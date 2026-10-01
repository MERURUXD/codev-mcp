# 安装与故障排查

本文档面向当前服务，说明如何在本机安装、配置、运行 CODE V MCP 服务，以及遇到问题时的排查顺序。

## 1. 环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows，仅本机 stdio 连接 |
| CODE V | 10.2，已注册 `CodeV.Command.102`，许可证可用 |
| Python | 3.10 及以上，推荐 64 位 |
| 依赖 | 锁定安装见 `requirements.lock.txt`；声明依赖见 `pyproject.toml` |

64 位 Python 可以驱动 32 位的 COM 服务，不需要 32 位工作进程。

## 2. 安装

以下命令在下载或克隆后的仓库根目录执行，`python` 应指向 Python 3.10+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m codev_mcp --backend simulated
```

锁定文件记录发布候选依赖（Python 3.10 的 rpds-py 与 websockets 使用兼容分支），可编辑安装便于从当前源码运行。若不安装本项目包，仍需先安装依赖，再设置 `$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'`。

直接安装声明依赖时，项目约束为 MCP 1.x 与 Pydantic 2.x；MCP 2.x 的服务 API 已变化，当前实现尚未迁移。

真实后端改用 `--backend com`。先确认 CODE V 安装、COM 注册和许可证可用。

## 3. 运行

```text
codev-mcp [--backend {simulated,com}] [--working-directory DIR] [--python EXE] [--timeout SECONDS]
```

| 选项 | 说明 |
| --- | --- |
| `--backend` | 默认 `simulated`；`com` 驱动真实 CODE V；`simulated` 用于自动化测试，结果一律标记为 simulated |
| `--working-directory` | 服务自有工作目录，CODE V 会话在其中运行，恢复点与结果图片也写在这里（源码／可编辑安装默认 `<仓库>/.codev-run`；其他安装方式建议显式指定可写的短路径） |
| `--python` | 运行工作进程的解释器，默认与当前进程相同 |
| `--timeout` | 单次工具调用的超时秒数，超时会终止卡住的工作进程 |

对应环境变量：`CODEV_MCP_BACKEND`、`CODEV_MCP_WORKDIR`、`CODEV_MCP_PYTHON`、`CODEV_MCP_TIMEOUT`。

stdout 只用于 MCP 协议，日志写 stderr。

## 4. MCP 客户端配置示例

在项目根目录完成安装后，用 PowerShell 生成配置所需绝对路径，避免依赖维护者机器或 `PYTHONPATH`：

```powershell
$pythonPath = (Resolve-Path .venv/Scripts/python.exe).Path
$workPath = Join-Path (Get-Location).Path '.codev-run'
$config = @{mcpServers = @{codev = @{command = $pythonPath; args = @('-m', 'codev_mcp', '--backend', 'simulated', '--working-directory', $workPath)}}}
$config | ConvertTo-Json -Depth 6
```

把生成的 JSON 加入客户端配置。真实后端把 `simulated` 改成 `com`；需要 Windows、CODE V 10.2 的 COM 注册和可用许可证。
真实镜头由用户自行提供绝对 `.len` 路径给 `open_lens`，不需要厂商样例。源码目录之外使用 wheel 安装时也可使用相同配置；工作目录必须可写。

服务的 `serverInfo.version` 来自 MCP SDK；服务版本见 `get_status.service_version`。

## 5. 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe examples/simulated_quickstart.py --output .codev-run/demo
```

模拟流程用于检查安装与 MCP 连接，不用于评价真实光学性能。

## 6. 故障排查

按以下顺序判断问题出在哪一层。

### 6.1 客户端连不上、工具列表为空

1. 手动运行 `python -m codev_mcp --backend simulated`，确认进程能启动且不输出到 stdout（stdout 被协议占用）。
2. 检查客户端配置是否使用安装了本项目的解释器，并确认绝对路径存在。
3. `get_status` 的 `warnings` 会说明工作进程是否启动失败，以及为什么。

### 6.2 工具返回 not_ready

含义是后台工作进程不可用。`get_status` 的 `warnings` 与错误 `details.stderr` 里有工作进程 stderr 的最后若干行。常见原因：依赖缺失、解释器路径写错、单次调用超时后工作进程被终止（此时重启服务即可）。

### 6.3 会话启动很慢或一直不返回

- 启动阶段有 120 秒上限，超过会返回错误而不是无限等待。
- 首次启动可能因许可证初始化而耗时较长；若超过启动上限，查看返回的诊断。
- 若长时间不返回，检查是否弹出 CODE V 的对话框（见 6.4）。

### 6.4 弹出「应用程序错误」对话框，之后所有调用卡住

这是 CODE V 引擎自身的崩溃（例如 `0x40000015` 或 `c0000005`），而且是**随机**发生的：同一台机器、同一套命令，前一次成功、后一次就崩。崩溃后引擎进程留在进程表里但已经没有线程，停在「应用程序错误」对话框上等点击；COM 服务器仍然健在，所以之后的调用不报错、直接永久阻塞。

服务侧针对这类故障提供以下防护；不能保证所有崩溃都能恢复：

1. **启动阶段看门狗**：`StartCodeV` 运行期间会有线程盯着新出现的引擎进程；一旦发现「没有线程」的引擎就立刻终止它，对话框随之消失（不需要你去点「确定」），`StartCodeV` 随即返回。
2. **启动重试**：会话启动最多尝试 4 次，每次只清理已确认属于本服务的进程后再重试；持续失败时返回诊断。
3. **运行中自愈**：引擎在运行中死掉时，下一次调用会立刻识别（不再等满超时），丢弃死会话、重新建会话，并从最近成功检查点恢复镜头、核对状态；恢复无法确认时停止镜头操作。

如需人工介入，按顺序做：

1. 关闭该对话框。
2. 根据工作目录的 `codev-mcp-session.json` 核对 PID 与会话归属，只处理确认由本服务创建的残留进程；不要按进程名批量结束进程。无法确认归属时先保留日志。
3. 重启 MCP 服务。

其他已有防护：启动后若没有引擎进程会立即返回结构化错误；每次调用前检查已记录的引擎进程是否存活（并且检查它是否还有线程）；启动前清理工作目录里的残留 `codev*.rec`（残留恢复文件会让 `StartCodeV` 永久阻塞）；客户端调用超时会终止卡住的工作进程。

怎么确认自愈发生过：`get_status` 的 `details.session_restarts` 是会话重建次数，大于 0 就说明引擎中途死过并被自动重建。重建后正在运行的分析标记为 `failed`，需要重新提交；已有结果文件保留，历史结果通过 `history_only` 标记。镜头从检查点恢复并核对，详情见 6.5。

### 6.5 引擎中途退出与成功检查点

服务承诺：`update_lens` 报告成功之前，修改已经写入独立检查点并通过“保存 → 重新载入 → 逐项比对”。因此引擎中途退出后，服务会从最近成功版本恢复，而不是回到原始镜头文件。

检查点位置与识别方式：

```text
<工作目录>/checkpoints/<后端实例 UUID>/<镜头 UUID>/
    revision-000000.len / .json     # 每次成功提交一份，从不覆盖
    current.json                    # 唯一允许替换的“成功版本指针”
    restore-points/<rp-NNNN>.len    # 每批修改前的恢复点
    transactions/<tx-NNNNNN>.json   # 事务记录（状态、失败原因）
```

- 唯一权威是 `current.json`（内含格式版本、镜头标识、版本号、文件大小与 SHA-256、源文件路径与哈希）。**不要**按文件修改时间或文件名猜“最新版本”。
- `get_status.details` 给出 `lens_state`（`empty`/`ready`/`updating`/`recovering`/`invalid`）、`lens_id`、`committed_revision`、`checkpoint_path`、`recovery_count`、`last_recovery`。
- 想显式打开某个已提交版本：读取 `current.json` 的 `lens_file`，再对该文件调用 `open_lens`（它会作为新的工作副本另建一份记录，不会改动历史版本）。
- 恢复无法确认（文件缺失、哈希不符、回读不一致）时 `lens_state` 变为 `invalid`，所有依赖可信镜头的操作（读取、修改、分析、另存）都被拒绝，直到重启服务；服务不会回退到更老版本或原始镜头文件。
- 工作进程或整个服务退出后会保留检查点、事务记录、日志与结果文件，但**不会自动续接**：重启服务后需要重新 `open_lens`。

### 6.6 长时间会话中途失败

长时间运行的 CODE V 会话可能中途退出，调用开销随分析设置变化。建议分段进行，必要时重新建立会话；见[长会话稳定性](capabilities.md#3-限制与注意事项)。

成功 `open_lens` 已建立 revision 0 检查点，后续成功 `update_lens` 会推进版本；只要当前检查点有效，下一次调用会丢弃死会话并自动从最近成功版本恢复镜头（见 6.5）。如果还没有任何提交过的检查点，就没有可恢复的版本（服务不会退回原始镜头文件），请重启服务后重新 `open_lens`。

### 6.7 提示会话已失效（session_invalid）

出现在两类情况：回滚失败无法确认、`StopCommand` 未被确认。此时不要继续依赖当前镜头状态；恢复无法确认时，读取、修改、分析与另存均会被拒绝。可用 `get_status` 查看诊断，然后重启服务并重新打开镜头。

### 6.8 提示参数错误，但参数看着没问题

按错误 `details` 判断：

- 由求解或拾取控制的参数会被拒绝（例如由 `PIM` 控制的像距）。
- 多变焦镜头的修改必须给出 `zoom_position`；若该参数本身没有变焦，服务会改共享值并在 `warnings` 里说明。
- 孔径编辑只支持单变焦镜头。系统孔径使用 `target="aperture"`、`parameter="value"`，保持现有 EPD/FNO/NA/NAO 类型；表面孔径使用 `parameter="clear_aperture_radius"`，只修改已有、唯一、居中、圆形的显式通光孔径半径。旧名称 `semi_aperture`、`clear_aperture`、自动孔径、遮拦和复合定义均拒绝。
- 玻璃名只允许字母、数字、点、加号、减号、下划线；路径必须是绝对路径且以 `.len` 结尾。

求解探测本身失败（引擎中途退出等）时，编辑仍会尝试，结果里的 `warnings` 会说明“求解探测未能读取”，此时由回读校验兜底。

### 6.9 提示文件冲突

另存拒绝覆盖源文件与已存在的目标文件，请换一个新文件名。

### 6.10 残留进程与许可证席位

每次启动会话时，服务把本次会话创建的 CODE V 进程记录到工作目录的 `codev-mcp-session.json`；下一次启动会先清理这些已确认属于本服务的进程。正常关闭（`close_session` 或客户端断开）会自动停止会话。用户自己启动的 CODE V 图形界面不会被服务接触或结束。

多个会话同时运行时（`schemes --jobs`）：会话启动由机器级互斥量 `Local\codev-mcp-session-start` 排队，最长等待 600 秒；`cvcomsvr.exe` 是所有同时运行的会话共享的 COM 服务器，先启动的会话虽把它记入自己的记录，但只要还有别的会话在用，停止和清理都不会结束它。
