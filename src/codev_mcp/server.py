"""MCP server entry point.

The server is a thin adapter around the worker: it validates tool arguments with
the public models, forwards them over the worker protocol and converts the
response back into the models. Errors keep their structured kind by travelling
inside the tool error message as JSON, because MCP only carries text for a
failed call.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from . import __version__
from .backend import BACKEND_NAMES, default_working_directory
from .client import DEFAULT_TIMEOUT_SECONDS, WorkerClient
from .errors import CodeVError, NotReadyError
from .models import (
    AnalysisRequest,
    AnalysisSnapshot,
    CancelResult,
    CapabilityInfo,
    CreateLensRequest,
    FirstOrderResult,
    ImagePayload,
    LensData,
    MtfResult,
    SaveResult,
    Source,
    SpotDiagramResult,
    StatusInfo,
    StructureRequest,
    StructureResult,
    TaskInfo,
    UpdateRequest,
    UpdateResult,
)

INSTRUCTIONS = (
    "通过 COM 接口驱动本机 CODE V 10.2：打开镜头、新建受限球面镜头、读取与修改参数、"
    "受限结构编辑、运行一阶参数/"
    "点列图/MTF 分析、导出 CODE V 原生绘图、另存镜头。所有调用串行执行，服务自己"
    "创建无界面会话，不接触"
    "用户已打开的 CODE V 图形界面。返回值都带 source 字段：simulated 表示模拟数据，"
    "codev 表示真实计算结果。工具调用失败时，错误消息是一段 JSON，包含 kind、"
    "message、details 与 hint 字段，kind 取值为 parameter、not_ready、"
    "computation_failed、session_invalid、unsupported、not_found、internal。"
    "错误消息形如 Error executing tool <name>: {json}，可调用 "
    "codev_mcp.errors.parse_tool_error_message 取回结构化字段。"
)


#: An error's raw output can be a whole CODE V text buffer (about 2 MB); an MCP error message that
#: large is truncated or rejected by clients and fills the model's context, so its ends are kept.
MAX_ERROR_RAW_OUTPUT = 8000


def _bounded_raw_output(raw_output: str | None) -> str | None:
    if raw_output is None or len(raw_output) <= MAX_ERROR_RAW_OUTPUT:
        return raw_output
    half = MAX_ERROR_RAW_OUTPUT // 2
    omitted = len(raw_output) - 2 * half
    return f"{raw_output[:half]}\n... [{omitted} characters omitted] ...\n{raw_output[-half:]}"


def _tool_error(exc: CodeVError) -> ToolError:
    payload = {
        "kind": exc.kind.value,
        "message": exc.message,
        "details": exc.details,
        "raw_output": _bounded_raw_output(exc.raw_output),
        "hint": exc.hint,
    }
    if exc.raw_output is not None and len(exc.raw_output) > MAX_ERROR_RAW_OUTPUT:
        payload["raw_output_characters"] = len(exc.raw_output)
    return ToolError(json.dumps(payload, ensure_ascii=False))


class ServiceState:
    """Owns the worker client and remembers why it is unavailable."""

    def __init__(
        self,
        backend: str,
        *,
        working_directory: str | None,
        python_executable: str | None,
        timeout: float,
    ) -> None:
        self.backend = backend
        self.working_directory = working_directory
        self.python_executable = python_executable
        self.timeout = timeout
        self.client: WorkerClient | None = None
        self.startup_error: str | None = None
        #: How many times a worker that died was replaced, and why the last one was.
        self.restarts = 0
        self.last_restart_reason: str | None = None
        #: Tool calls run in threads, so two of them could otherwise replace one dead worker twice.
        self._lock = threading.Lock()

    def start(self) -> None:
        client = WorkerClient(
            self.backend,
            working_directory=self.working_directory,
            python_executable=self.python_executable,
            timeout=self.timeout,
        )
        try:
            client.start()
        except Exception as exc:  # noqa: BLE001 - reported through get_status
            self.startup_error = f"{type(exc).__name__}: {exc}"
            self.client = None
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self.client = client
        self.startup_error = None

    def require(self) -> WorkerClient:
        with self._lock:
            return self._require()

    def _require(self) -> WorkerClient:
        client = self.client
        if client is not None and not client.alive:
            # A worker that timed out (and was killed) or crashed is replaced once; the new worker stops
            # the CODE V processes the old one recorded. A replacement that cannot start leaves no client,
            # so a broken setup is reported instead of being restarted on every call.
            reason = client.last_error.message if client.last_error is not None else "the worker exited"
            try:
                client.close()
            except Exception:  # noqa: BLE001 - the process is already gone
                pass
            self.client = None
            self.restarts += 1
            self.last_restart_reason = reason
            self.start()
        if self.client is None or not self.client.alive:
            raise NotReadyError(
                self.startup_error or "后台工作进程不可用。",
                details={"backend": self.backend},
                hint="检查服务日志或重新启动服务。",
            )
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


def _strip_images(payload: dict) -> tuple[dict, list[ImagePayload]]:
    """Remove base64 image bodies from a JSON payload and collect them instead."""
    images: list[ImagePayload] = []

    def walk(node):
        if isinstance(node, dict):
            if {"path", "media_type", "base64_data"} <= set(node):
                image = ImagePayload.model_validate(node)
                images.append(image)
                trimmed = dict(node)
                trimmed["base64_data"] = "<returned as MCP image content>"
                return trimmed
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(payload), images


def build_server(
    backend: str = "simulated",
    *,
    working_directory: str | None = None,
    python_executable: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    start_worker: bool = True,
) -> FastMCP:
    """Create the MCP server and optionally start its worker immediately."""
    state = ServiceState(
        backend,
        working_directory=working_directory,
        python_executable=python_executable,
        timeout=timeout,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        if start_worker:
            await anyio.to_thread.run_sync(state.start)
        try:
            yield {}
        finally:
            await anyio.to_thread.run_sync(state.close)

    mcp = FastMCP("codev-mcp", instructions=INSTRUCTIONS, lifespan=lifespan)

    def call_sync(method: str, params: dict | None = None):
        try:
            return state.require().call(method, params)
        except CodeVError as exc:
            raise _tool_error(exc) from exc

    async def call(method: str, params: dict | None = None):
        # A worker call can take minutes (a CODE V start, a long MTF); in a thread it leaves the event
        # loop free to answer pings and other requests. WorkerClient serialises the calls themselves.
        return await anyio.to_thread.run_sync(call_sync, method, params)

    @mcp.tool()
    async def get_status() -> StatusInfo:
        """返回后端类型、CODE V 版本、会话与镜头状态、当前分析任务与支持能力。"""
        restart_note = (
            [f"后台工作进程已重建 {state.restarts} 次，上次原因：{state.last_restart_reason}；"
             "重建后不会自动恢复之前打开的镜头。"]
            if state.restarts else []
        )
        if state.client is None or not state.client.alive:
            reason = state.startup_error or "尚未启动。"
            if state.client is not None:
                reason = "已退出，下一次工具调用会重建工作进程。"
            return StatusInfo(
                backend=state.backend,
                source=Source.CODEV if state.backend == "com" else Source.SIMULATED,
                service_version=__version__,
                ready=False,
                session_open=False,
                warnings=["后台工作进程未运行：" + reason, *restart_note],
                capabilities=[],
            )
        status = StatusInfo.model_validate(await call("get_status"))
        status.warnings.extend(restart_note)
        return status

    @mcp.tool()
    async def open_lens(path: str) -> LensData:
        """打开一个 .len 镜头文件的工作副本，并返回其表面、材料、单位、波长与视场。"""
        return LensData.model_validate(await call("open_lens", {"path": path}))

    @mcp.tool()
    async def create_lens(request: CreateLensRequest) -> LensData:
        """在空会话中按受限球面规格新建单变焦镜头，并发布核对过的恢复点。

        image_solve="pim" 给最后一个普通面的厚度加 CODE V 近轴像面求解（PIM），
        请求中的厚度只是初值；求解厚度之后由 CODE V 推导，不能再直接编辑。
        """
        return LensData.model_validate(
            await call("create_lens", {"request": request.model_dump(mode="json")})
        )

    @mcp.tool()
    async def get_lens(zoom_position: int | None = None) -> LensData:
        """读取当前镜头的表面、材料、单位、孔径、波长、视场与变焦位置。

        视场含渐晕因子 vux/vlx/vuy/vly（CODE V VUX/VLX/VUY/VLY），可经 update_lens 修改。
        zoom_position 用于多变焦镜头，只影响按变焦位置取值的数据。
        """
        params = {} if zoom_position is None else {"zoom_position": zoom_position}
        return LensData.model_validate(await call("get_lens", params))

    @mcp.tool()
    async def update_lens(request: UpdateRequest) -> UpdateResult:
        """批量修改已有表面、视场、波长及受限孔径参数。

        一批修改共享一个恢复点；只要有一条被拒绝，整批都不会生效。
        由求解或拾取控制的参数会被拒绝，多变焦镜头必须给出 zoom_position。
        视场可改 y_angle、x_angle、weight 与渐晕因子 vux/vlx/vuy/vly（-0.99～0.99）。
        field_set 整体替换视场集合（1～10 个角度视场，含个数、角度、权重与渐晕因子），
        单独成一次事务、不能与 edits 同批；省略的权重与渐晕因子沿用同编号视场的值，
        新增视场权重为 1、渐晕为 0，像面求解（如 PIM）保持不变。仅支持单变焦、角度定义的视场。
        孔径首版仅支持单变焦：target=aperture、parameter=value 保持现有
        EPD/FNO/NA/NAO 类型只改数值；表面 clear_aperture_radius 只改已有的
        单一、居中、圆形显式通光孔径半径。自动孔径、遮拦和复合定义只读。
        """
        return UpdateResult.model_validate(await call("update_lens", {"request": request.model_dump(mode="json")}))

    @mcp.tool()
    async def edit_lens_structure(request: StructureRequest) -> StructureResult:
        """对本服务新建的简单镜头批量插入或删除普通球面、设置光阑，回读并核对恢复。"""
        return StructureResult.model_validate(
            await call("edit_lens_structure", {"request": request.model_dump(
                mode="json", exclude_unset=True,
            )})
        )

    @mcp.tool()
    async def run_analysis(request: AnalysisRequest) -> TaskInfo:
        """提交一阶参数、点列图、MTF、当前焦面 WAV 或原生绘图，返回任务状态。

        MTF 必须由调用方给出频率网格；原生绘图用 options.plot_type 选择类型，
        由 CODE V 自己绘制并导出为 PNG。WAV 只支持单变焦镜头的全部视场和波长，
        固定 NRD=20，不改变焦面。点列图与原生绘图异步执行，需要轮询
        get_analysis；一阶参数、MTF 和 WAV 同步完成。同一时刻只允许一个分析任务。
        """
        # Only the fields the caller really set are forwarded, so the backend can
        # still tell a supplied setting from a model default: a native plot
        # refuses an explicitly supplied selection setting.
        return TaskInfo.model_validate(
            await call(
                "run_analysis",
                {"request": request.model_dump(mode="json", exclude_unset=True)},
            )
        )

    @mcp.tool()
    async def get_analysis() -> list[mcp_types.ContentBlock]:
        """获取当前分析任务的状态、数值结果与图像。

        结果以文本 JSON 返回，图像同时作为 MCP 图像内容返回，并在结果文件中保留副本。
        """
        payload = await call("get_analysis")
        snapshot = AnalysisSnapshot.model_validate(payload)
        trimmed, images = _strip_images(snapshot.model_dump(mode="json"))
        blocks: list[mcp_types.ContentBlock] = [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(trimmed, ensure_ascii=False, indent=2),
            )
        ]
        for image in images:
            blocks.append(
                mcp_types.ImageContent(
                    type="image", data=image.base64_data, mimeType=image.media_type
                )
            )
        return blocks

    @mcp.tool()
    async def cancel_analysis() -> CancelResult:
        """请求取消当前分析任务，并区分“已请求取消”与“确认停止”。

        只对异步任务（点列图与原生绘图导出）有意义；取消原生绘图时还会关闭
        CODE V 已经打开的绘图文件。
        """
        payload = await call("cancel_analysis") or {}
        if isinstance(payload, dict) and "task" in payload and "requested" in payload:
            return CancelResult.model_validate(payload)
        task = TaskInfo.model_validate(payload) if payload else None
        return CancelResult(
            source=Source.SIMULATED if state.backend != "com" else Source.CODEV,
            requested=task is not None,
            still_running=bool(task and task.state.value in {"queued", "running"}),
            task=task,
        )

    @mcp.tool()
    async def save_lens_as(path: str) -> SaveResult:
        """把当前工作副本另存为新文件；拒绝覆盖源文件或已存在的文件。"""
        return SaveResult.model_validate(await call("save_lens_as", {"path": path}))

    @mcp.tool()
    async def close_session() -> StatusInfo:
        """释放本服务创建的 CODE V 会话，并返回释放后的状态。"""
        return StatusInfo.model_validate(await call("close_session"))

    return mcp
