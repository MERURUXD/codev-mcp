"""Arrange report material from finished runs (E7).

Reads what the other CLIs already wrote — a ``codev_mcp.compare`` bundle (single
lens or initial/final pair) and, optionally, ``codev_mcp.aut`` run directories —
and copies it into a new folder laid out the way reports are usually written:

* ``figures/init-*`` and ``figures/final-*``: the paired plots (native CODE V
  plots with their ``.PLT`` files, and the service redrawn spot and MTF pictures);
* ``lis/``: the native lens listing before and after; ``wav/`` and ``first-order/``:
  the native WAV and first order text; ``raw/``: the other native outputs;
* ``macro/setup-*.seq``: the equivalent commands of typed edits, scalings and sequence
  imports that preceded the optimisation (``--setup``);
* ``macro/``: the optimisation as the service sent it (verbatim) and an
  *equivalent hand-written macro* that keeps only the variables, constraints,
  error function settings and ``GO`` of each stage, with the differences listed;
* ``runs/``: the AUT output files and a per-stage summary;
* ``index.md`` (资料索引) and ``manifest.json`` with the source path and SHA-256 of
  every file, and a list of what could not be produced and why.

Nothing is recomputed and no CODE V command is sent. The target folder must not
exist. An initial lens that could not be fully traced simply has fewer initial
figures; each missing one is listed with the reason the run recorded.

    python -m codev_mcp.deliver --comparison BUNDLE [--aut AUT_RUN ...] [--setup RUN ...] [--seq FILE ...] \
        --output-dir NEW
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PREFIX = {"initial": "init", "final": "final", "lens": "final"}
STAGE_TEXT = {"initial": "初始", "final": "最终", "lens": "镜头"}
#: Analyses whose raw native text gets its own folder; the rest goes to raw/.
TEXT_FOLDER = {"wavefront": "wav", "first_order": "first-order"}
#: Commands of the zero-cycle variable check that ``aut`` runs before the real run.
ZERO_CYCLE = ["aut", "err cdv", "mxc 0", "vli y"]
OMITTED = ("零循环变量表核对（`aut; err cdv; mxc 0; vli y; go`）", "墙钟辅助 `tim` 与变量表打印 `vli y`",
           "输出重定向（`out <文件>` … `out t`）", "运行后对控制码的恢复（`ccy/thc … 100/0`）",
           "中间候选保存（`sav <文件>`）")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Delivery:
    """Copies files into the new folder and remembers where each one came from."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: list[dict] = []
        self.unavailable: list[dict] = []
        self.notes: list[str] = []

    def copy(self, source: Path, relative: str, *, kind: str, description: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"Refusing to overwrite {relative}")
        shutil.copyfile(source, target)
        if _sha(target) != _sha(source):
            raise ValueError(f"Copy of {source} does not match its source")
        self.files.append({"path": relative, "kind": kind, "description": description, "source": str(source),
                           "sha256": _sha(target), "bytes": target.stat().st_size})
        return target

    def write(self, relative: str, text: str, *, kind: str, description: str, source: str = "generated") -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"Refusing to overwrite {relative}")
        target.write_text(text, encoding="utf-8", newline="\n")
        self.files.append({"path": relative, "kind": kind, "description": description, "source": source,
                           "sha256": _sha(target), "bytes": target.stat().st_size})
        return target


# ----------------------------------------------------------------- macros


def split_stages(commands: list[str]) -> list[tuple[str, list[str]]]:
    """The recorded AUT command list as (stage header, commands) pairs."""
    stages: list[tuple[str, list[str]]] = []
    for command in commands:
        if command.startswith("! stage "):
            stages.append((command[2:], []))
        elif stages:
            stages[-1][1].append(command)
        else:
            raise ValueError("The recorded AUT commands do not start with a stage marker")
    return stages


def equivalent_stage(commands: list[str]) -> list[str]:
    """One stage reduced to what a person would type: setup, variables, AUT settings, constraints, GO.

    The recorded block is ``[edits, FRZ and variables] [zero-cycle check] GO
    [AUT settings, bounds, constraints] OUT file GO OUT T [restore, SET VIG] SAV file``.
    The pieces are located by those fixed landmarks and anything that does not fit
    raises instead of being guessed.
    """
    try:
        first_go = commands.index("go")
    except ValueError:
        raise ValueError("The stage has no zero-cycle GO") from None
    if commands[first_go - len(ZERO_CYCLE):first_go] != ZERO_CYCLE:
        raise ValueError("The stage does not contain the recorded zero-cycle check")
    head = commands[:first_go - len(ZERO_CYCLE)]
    rest = commands[first_go + 1:]
    redirect = next((i for i, command in enumerate(rest) if command.startswith("out ") and command != "out t"), None)
    if redirect is None:
        raise ValueError("The stage has no output redirection")
    settings = [command for command in rest[:redirect] if not command.startswith("tim ") and command != "vli y"]
    if not settings or settings[0] != "aut":
        raise ValueError("The stage has no AUT settings block")
    # SET VIG is part of the design step (it changes the vignetting factors), so it stays.
    after = ["set vig"] if "set vig" in rest[redirect:] else []
    return head + settings + ["go"] + after


def macro_texts(commands: list[str], *, source: str) -> tuple[str, str, list[str]]:
    """(verbatim text, equivalent text, per-stage problems) from a recorded command list."""
    stages = split_stages(commands)
    verbatim = ["! Optimisation exactly as the service sent it (codev_mcp.aut).",
                "! The 'out' lines are local paths; drop them (and the OUT T after GO) when replaying.",
                f"! Source: {source}", ""]
    equivalent = ["! Equivalent hand-written macro: variables, constraints, error function settings and GO.",
                  f"! Source: {source}",
                  "! Not included (see the index): " + "; ".join(OMITTED) + ".",
                  "! Each stage starts from the previous stage's result; run the stages in order in one session.", ""]
    problems = []
    for header, block in stages:
        verbatim += [f"! {header}"] + block + [""]
        equivalent.append(f"! {header}")
        try:
            equivalent += equivalent_stage(block)
        except ValueError as exc:
            problems.append(f"{header}: {exc}")
            equivalent.append(f"! (this stage could not be reduced: {exc})")
        equivalent.append("")
    return "\n".join(verbatim), "\n".join(equivalent), problems


# ------------------------------------------------------------------ setup runs

SETUP_ACTIONS = {"edit": "类型化修改", "scale": "起点缩放", "import_seq": "导入 .seq"}


def setup_material(delivery: Delivery, run: Path, number: int) -> None:
    """The equivalent native commands of one edit, scale or import run, as a macro file."""
    record_path = run / "execution-record.json"
    if not record_path.is_file():
        raise ValueError(f"No execution-record.json in {run}")
    steps = [item for item in json.loads(record_path.read_text(encoding="utf-8"))["steps"]
             if item.get("action") in SETUP_ACTIONS]
    if not steps:
        raise ValueError(f"{record_path} has no edit, scale or import step")
    for index, step in enumerate(steps, 1):
        lines = [f"! {SETUP_ACTIONS[step['action']]}：服务发送的等效命令（{step.get('command_note') or ''}）",
                 f"! Source: {record_path}", ""] + list(step.get("native_commands") or [])
        suffix = f"{number}" if len(steps) == 1 else f"{number}-{index}"
        delivery.write(f"macro/setup-{suffix}-{step['action']}.seq", "\n".join(lines) + "\n", kind="macro",
                       description=f"{SETUP_ACTIONS[step['action']]}（{step['action']}）的等效命令，放在优化宏之前", source=str(record_path))


# ------------------------------------------------------------------ AUT run


def aut_material(delivery: Delivery, run: Path, number: int, *, label: str) -> None:
    record_path = run / "execution-record.json"
    if not record_path.is_file():
        raise ValueError(f"No execution-record.json in {run}")
    steps = json.loads(record_path.read_text(encoding="utf-8"))["steps"]
    step = next((item for item in steps if item.get("action") == "aut"), None)
    if step is None:
        raise ValueError(f"{record_path} has no AUT step")
    suffix = "" if number == 1 else f"-{number}"
    verbatim, equivalent, problems = macro_texts(step["native_commands"], source=str(record_path))
    delivery.write(f"macro/optimize-verbatim{suffix}.seq", verbatim, kind="macro",
                   description=f"{label}：服务实际发送的命令块（含重定向与保存，原样）", source=str(record_path))
    delivery.write(f"macro/optimize-equivalent{suffix}.seq", equivalent, kind="macro",
                   description=f"{label}：等效手写宏，只保留变量、约束、误差函数设置与 GO；省略的内容见文件头",
                   source=str(record_path))
    for problem in problems:
        delivery.notes.append(f"{label} 精简宏：{problem}")
    results = step.get("results") or {}
    rows = ["| 阶段 | 状态 | 结束原因 | ERR. F. 初始 → 最终 | 约束全部满足 | 变量界满足 |", "| --- | --- | --- | --- | --- | --- |"]
    for stage in results.get("stages") or []:
        rows.append(f"| {stage['index']} {stage['name']} | {stage['status']} | {stage.get('completion', '—')} | "
                    f"{stage.get('initial_error', '—')} → {stage.get('final_error', '—')} | "
                    f"{stage.get('constraints_satisfied', '—')} | {stage.get('bounds_satisfied', '—')} |")
    delivery.write(f"runs/aut-summary{suffix}.md", "\n".join([f"# AUT 运行摘要（{label}）", "", *rows, "",
                   f"来源：`{record_path}`。数值取自运行记录，未重算。", ""]), kind="run",
                   description=f"{label}：各阶段 ERR. F. 与判定摘要", source=str(record_path))
    for output in sorted(run.glob("stage-*-aut-output.lis")):
        delivery.copy(output, f"runs/{output.stem}{suffix}.lis", kind="run",
                      description=f"{label}：AUT 原始输出（`OUT` 重定向文件）")


# ----------------------------------------------------------- comparison bundle


def comparison_material(delivery: Delivery, bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    stages = list(manifest.get("inputs", {}))
    for entry in manifest.get("analyses", []):
        prefix = PREFIX.get(entry["stage"], entry["stage"])
        name = entry["name"]
        label = f"{STAGE_TEXT.get(entry['stage'], entry['stage'])}镜头 {name}"
        if entry.get("status") != "succeeded":
            delivery.unavailable.append({"item": f"{prefix}-{name}", "reason": entry.get("error") or entry.get("status")})
            continue
        files = {Path(item["path"]).name: item for item in entry.get("artifacts", [])}
        native = entry["request"]["kind"] == "native_plot"
        if "image.png" in files:
            delivery.copy(bundle / files["image.png"]["path"], f"figures/{prefix}-{name}.png", kind="figure",
                          description=f"{label}（{'CODE V 原生绘图' if native else '服务按数值重绘'}）")
        if "native.PLT" in files:
            delivery.copy(bundle / files["native.PLT"]["path"], f"figures/{prefix}-{name}.PLT", kind="figure",
                          description=f"{label} 的 CODE V 中性绘图文件")
        if "raw-output.txt" in files:
            folder = TEXT_FOLDER.get(entry["request"]["kind"])
            if folder:
                delivery.copy(bundle / files["raw-output.txt"]["path"], f"{folder}/{prefix}-{name}.txt", kind="text",
                              description=f"{label} 的 CODE V 原生文本输出")
            elif not native:
                delivery.copy(bundle / files["raw-output.txt"]["path"], f"raw/{prefix}-{name}.txt", kind="text",
                              description=f"{label} 的 CODE V 原生文本输出")
    for failed in manifest.get("failed_analyses") or []:
        item = f"{PREFIX.get(failed['stage'], failed['stage'])}-{failed['name']}"
        if not any(entry["item"] == item for entry in delivery.unavailable):
            delivery.unavailable.append({"item": item, "reason": failed.get("error")})
    for stage in stages:
        listing = (manifest["inputs"][stage].get("listing") or {}).get("path")
        if listing:
            delivery.copy(bundle / listing, f"lis/{PREFIX.get(stage, stage)}-lis.txt", kind="listing",
                          description=f"{STAGE_TEXT.get(stage, stage)}镜头的原生 LIS 文本（get_lens 读取时的只读输出）")
        else:
            delivery.unavailable.append({"item": f"{PREFIX.get(stage, stage)}-lis", "reason": "运行未导出 LIS"})
    for name, relative, description in (("evaluation.json", "evaluation/evaluation.json", "规格逐项判定"),
                                        ("comparison.md", "evaluation/comparison.md", "对比报告"),
                                        ("report.md", "evaluation/report.md", "单镜头评价报告"),
                                        ("design-spec.json", "evaluation/design-spec.json", "评价时使用的设计规格副本")):
        if (bundle / name).is_file():
            delivery.copy(bundle / name, relative, kind="evaluation", description=description)
    missing_init = [item for item in delivery.unavailable if item["item"].startswith("init-")]
    if missing_init and stages and "initial" in stages:
        delivery.notes.append(
            f"初始镜头有 {len(missing_init)} 项没有生成（例如大视场无渐晕起点无法完整追迹）；"
            "初始图只包含实际生成的部分，原因逐项列在“未能生成的项”中。")
    return manifest


# ------------------------------------------------------------------- index


def render_index(title: str, delivery: Delivery, manifest: dict | None) -> str:
    groups = {"figure": "图（初始 `init-*` ／最终 `final-*` 成对）", "listing": "原生 LIS", "text": "原生文本输出（WAV、一阶等）",
              "macro": "优化宏", "run": "运行输出", "evaluation": "规格判定与报告", "lens": "镜头与序列文件"}
    lines = [f"# 资料索引：{title}", "",
             f"> 由 `codev_mcp.deliver` 于 {_now()} 整理。文件原样复制（SHA-256 已核对），来源路径列在 `manifest.json`；"
             "数值未重算，未向 CODE V 发送任何命令。", ""]
    for kind, heading in groups.items():
        rows = [item for item in delivery.files if item["kind"] == kind]
        if not rows:
            continue
        lines += [f"## {heading}", "", "| 文件 | 说明 | 大小 | SHA-256（前 12 位） |", "| --- | --- | ---: | --- |"]
        lines += [f"| [{item['path']}]({item['path']}) | {item['description']} | {item['bytes']} | `{item['sha256'][:12]}` |"
                  for item in rows]
        lines.append("")
    if any(item["kind"] == "macro" for item in delivery.files):
        lines += ["## 关于精简宏", "",
                  "`macro/optimize-equivalent*.seq` 由记录的命令块按固定位置标记裁剪而成，只保留每个阶段的视场／权重修改、"
                  "`FRZ` 与变量开放、`AUT` 的误差函数设置、变量界、约束和 `GO`。与原样命令块 `optimize-verbatim*.seq` 的差别：", ""]
        lines += [f"- 省略 {item}" for item in OMITTED]
        lines += ["", "因此手写宏运行后，变量的开放标记（`CCY/THC 0`）留在镜头里而不像服务那样恢复，不会写出候选文件，"
                  "也不会核对变量表；优化命令本身与服务发送的相同。", ""]
    if delivery.unavailable:
        lines += ["## 未能生成的项", "", "| 项 | 原因（运行记录） |", "| --- | --- |"]
        lines += [f"| {item['item']} | {str(item['reason']).replace('|', '/')} |" for item in delivery.unavailable]
        lines.append("")
    if delivery.notes:
        lines += ["## 说明", ""] + [f"- {note}" for note in delivery.notes] + [""]
    return "\n".join(lines)


def deliver(comparison: Path | None, aut_runs: list[Path], seqs: list[Path], output_dir: Path, *,
            title: str = "交付资料", setup_runs: list[Path] | None = None) -> dict:
    setup_runs = setup_runs or []
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError(f"Output folder already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise ValueError(f"The parent of the output folder must exist: {output_dir.parent}")
    if comparison is None and not aut_runs and not seqs and not setup_runs:
        raise ValueError("Give at least one of --comparison, --aut, --setup or --seq")
    for run in setup_runs:
        if not (run / "execution-record.json").is_file():
            raise ValueError(f"Not a run directory (no execution-record.json): {run}")
    if comparison is not None and not (comparison / "manifest.json").is_file():
        raise ValueError(f"Not a comparison bundle (no manifest.json): {comparison}")
    for run in aut_runs:
        if not (run / "execution-record.json").is_file():
            raise ValueError(f"Not an AUT run directory (no execution-record.json): {run}")
    for seq in seqs:
        if not seq.is_file() or seq.suffix.lower() not in {".seq", ".len"}:
            raise ValueError(f"Expected an existing .seq or .len file: {seq}")
    output_dir.mkdir()
    delivery = Delivery(output_dir)
    manifest = None
    try:
        if comparison is not None:
            manifest = comparison_material(delivery, comparison.resolve())
        for number, run in enumerate(setup_runs, 1):
            setup_material(delivery, run.resolve(), number)
        for number, run in enumerate(aut_runs, 1):
            aut_material(delivery, run.resolve(), number, label=f"AUT 运行 {number}" if len(aut_runs) > 1 else "AUT 运行")
        for seq in seqs:
            delivery.copy(seq.resolve(), f"lens/{seq.name}", kind="lens", description="镜头或 .seq 文件（按调用方给定）")
        delivery.write("index.md", render_index(title, delivery, manifest), kind="index", description="资料索引")
        result = {"schema_version": 1, "kind": "deliverable", "created_at": _now(), "title": title,
                  "comparison": str(comparison.resolve()) if comparison else None,
                  "aut_runs": [str(run.resolve()) for run in aut_runs], "files": delivery.files,
                  "unavailable": delivery.unavailable, "notes": delivery.notes}
        (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8", newline="\n")
    except BaseException:
        # A half written folder would look like a deliverable; remove only what this call created.
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把已完成的评价包与 AUT 运行整理成报告素材（init-/final- 成对图、LIS、WAV、宏、资料索引）")
    parser.add_argument("--comparison", type=Path, help="codev_mcp.compare 的运行包（单镜头或初始／最终）")
    parser.add_argument("--aut", type=Path, action="append", default=[], help="codev_mcp.aut 的运行目录，可重复")
    parser.add_argument("--setup", type=Path, action="append", default=[],
                        help="优化之前的类型化修改／缩放／.seq 导入运行目录（取其执行记录里的等效命令），可重复")
    parser.add_argument("--seq", type=Path, action="append", default=[], help="要一并放入的 .seq／.len 文件，可重复")
    parser.add_argument("--output-dir", type=Path, required=True, help="新的输出目录（已存在则拒绝）")
    parser.add_argument("--title", default="交付资料")
    args = parser.parse_args(argv)
    try:
        result = deliver(args.comparison, args.aut, args.seq, args.output_dir, title=args.title,
                         setup_runs=args.setup)
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{len(result['files'])} 个文件 -> {args.output_dir.resolve()}；未能生成 {len(result['unavailable'])} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
