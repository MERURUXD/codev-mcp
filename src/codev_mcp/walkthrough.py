"""Render a design walkthrough (设计记录) from execution records (D9).

The generator only formats what the runs recorded: numbers keep their units,
precision notes and source, nothing is recomputed or estimated, and anything
missing is written as missing. Records whose source is ``simulated`` are marked
as having no optical meaning. The text template is the ``ZH`` table below; the
record format does not depend on it.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .spec import read_spec

ZH = {
    "title": "# 设计记录：{title}",
    "generated": "> 由 `codev_mcp.walkthrough` 根据 {count} 个运行目录的执行记录生成（{when}）。数值均取自运行记录，保留单位、精度说明与来源；未记录的项写为“缺失”，不重算、不估算。设计思路等叙述请在各节之后补充，引用的数值应出自这些记录。",
    "simulated": "> **记录中含模拟来源（source=simulated）的步骤，其数值没有光学意义，不能作为设计结论。**",
    "s1": "## 1. 设计目标与指标",
    "s2": "## 2. 素材与准备",
    "s3": "## 3. 操作步骤",
    "s4": "## 4. 出图清单",
    "s5": "## 5. 实测数据",
    "s6": "## 6. 注意事项",
    "missing": "缺失",
    "pending": "待补",
    "status": {"succeeded": "成功", "failed": "失败", "complete": "完成", "pass": "通过", "fail": "未通过",
               "unknown": "未知", "satisfied": "满足", "violated": "未满足", "running": "未完成"},
    "actions": {"scale": "起点缩放", "glass_candidates": "相近玻璃候选", "edit": "类型化修改（换玻璃等）",
                "aut": "受控 AUT 候选", "aut_accept": "显式接受 AUT 候选", "export_seq": "导出可读 .seq",
                "evaluate": "评价出图与规格判定"},
}


def _fmt(value, digits: int = 9) -> str:
    if value is None:
        return ZH["missing"]
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _status(value) -> str:
    return ZH["status"].get(value, _fmt(value))


def _short(sha: str | None) -> str:
    return f"`{sha[:12]}`" if sha else ZH["missing"]


def load_runs(directories: list[Path]) -> list[dict]:
    """Steps in the order the directories are given, each with its bundle path."""
    steps = []
    for directory in directories:
        path = directory / "execution-record.json"
        if not path.is_file():
            raise ValueError(f"{directory} has no execution-record.json")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("kind") != "execution_record" or record.get("schema_version") != 1:
            raise ValueError(f"{path} is not an execution record of schema 1")
        for step in record["steps"]:
            steps.append({**step, "_directory": str(directory)})
    return steps


def _spec_section(spec: dict | None, spec_error: str | None) -> list[str]:
    lines = [ZH["s1"], ""]
    if spec is None:
        return lines + [f"设计规格：{ZH['missing']}" + (f"（{spec_error}）" if spec_error else "") + "。", ""]
    lines += [f"规格：{spec['name']}（单位 {spec['units']}）" + ("；**阈值为演示值，不是设计要求**" if spec.get("demonstration") else ""), ""]
    system = spec.get("system") or {}
    for key, label in (("aperture", "孔径"), ("fields", "视场"), ("wavelengths", "波长"), ("glass_catalogs", "玻璃目录")):
        entry = system.get(key)
        if entry is None:
            continue
        if entry.get("pending"):
            text = ZH["pending"]
        elif key == "aperture":
            text = f"{entry['kind'].upper()} {_fmt(entry['value'])}"
        elif key == "fields":
            text = "、".join(f"{_fmt(item['y_angle'])}°" + (f"（权重 {_fmt(item['weight'])}）" if item.get("weight") is not None else "")
                            for item in entry["values"])
        elif key == "wavelengths":
            text = "、".join(f"{_fmt(item['nm'])} nm" for item in entry["values"]) + f"；参考 {entry.get('reference')}"
        else:
            text = "、".join(entry["allowed"])
        lines.append(f"- {label}：{text}" + (f"（来源：{entry['source']}）" if entry.get("source") else ""))
    lines += ["", "| 编号 | 指标 | 条件 | 下限 | 上限 | 必需 | 来源 |", "| --- | --- | --- | ---: | ---: | --- | --- |"]
    for item in spec.get("requirements", []):
        condition = "；".join(f"{key} {item[key]}" for key in ("field", "direction", "frequency") if item.get(key) is not None) or "—"
        low = ZH["pending"] if item.get("pending") else _fmt(item["minimum"]) if item["minimum"] is not None else "—"
        high = ZH["pending"] if item.get("pending") else _fmt(item["maximum"]) if item["maximum"] is not None else "—"
        lines.append(f"| {item['id']} | {item['metric']}（{item['unit']}） | {condition} | {low} | {high} | "
                     f"{'是' if item['required'] else '否'} | {item.get('source') or ZH['missing']} |")
    for preset in spec.get("criteria", []):
        lines.append(f"- 判据预设：{preset['preset']}" + (f"（{preset['statistic']}）" if preset.get("statistic") else "")
                     + f"，{'必需' if preset['required'] else '非必需'}")
    return lines + [""]


def _materials(steps: list[dict]) -> list[str]:
    lines = [ZH["s2"], ""]
    first = next((step for step in steps if step["inputs"] and step["inputs"][0].get("role") == "lens"), None)
    if first:
        lens = first["inputs"][0]
        lines.append(f"- 起点镜头：`{lens['path']}`，SHA-256 {_short(lens.get('sha256'))}")
    lines += ["", "| 步骤 | 产物 | 路径 | SHA-256 |", "| ---: | --- | --- | --- |"]
    for index, step in enumerate(steps, 1):
        for output in step.get("outputs", []):
            lines.append(f"| {index} | {output.get('role', ZH['actions'].get(step['action'], step['action']))} | "
                         f"`{output.get('path')}` | {_short(output.get('sha256'))} |")
    lines += ["", "运行目录（均保留完整清单、原始输出和日志）：", ""]
    lines += [f"- 步骤 {index}：`{step['_directory']}`" for index, step in enumerate(steps, 1)]
    return lines + [""]


def _command_block(step: dict) -> list[str]:
    commands = step.get("native_commands") or []
    if not commands:
        return []
    lens = next((item for item in step.get("inputs", []) if item.get("role") == "lens"), None)
    note = step.get("command_note")
    block = ["", "可在 CODE V 命令行复现的命令" + (f"（{note}）" if note else "") + "：", "", "```"]
    if lens and step["action"] in {"scale", "edit"}:
        block.append(f"RES {lens['path']}")
    block += commands
    output = next(iter(step.get("outputs", [])), None)
    if output and step["action"] in {"scale", "edit"}:
        block.append(f"SAV {output['path']}")
    return block + ["```"]


def _first_order_table(pairs: dict | None) -> list[str]:
    if not pairs:
        return []
    lines = ["", "| 一阶参数 | 之前 | 之后 |", "| --- | ---: | ---: |"]
    lines += [f"| {key} | {_fmt(value.get('before'), 12)} | {_fmt(value.get('after'), 12)} |" for key, value in pairs.items()]
    return lines


def _aut_step(step: dict) -> list[str]:
    results = step.get("results") or {}
    lines = [f"- 目标达成（末阶段 ERR. F. ≤ TAR）：{_fmt(results.get('target_reached'))}；全部约束满足："
             f"{_fmt(results.get('all_constraints_satisfied'))}；变量界：{_fmt(results.get('explicit_bounds_satisfied'))}"]
    relations = results.get("relations") or {}
    if relations:
        lines.append(f"- 保留的求解／拾取：{relations.get('solves') or '无'}／{relations.get('pickups') or '无'}")
    commands = step.get("native_commands") or []
    for stage in results.get("stages") or []:
        lines += ["", f"#### 阶段 {stage['index']}：{stage['name']}（{_status(stage['status'])}）", ""]
        if stage.get("error"):
            lines.append(f"- 失败原因：{stage['error']}")
        if stage.get("completion"):
            lines.append(f"- 结束原因：{stage['completion']}；ERR. F. {_fmt(stage.get('initial_error'))} → "
                         f"{_fmt(stage.get('final_error'))}（第 {_fmt(stage.get('final_cycle'))} 循环）")
        for change in stage.get("lens_changes") or []:
            where = f"视场 {change['field']}" if change["target"] == "field" else f"波长 {change['wavelength']}"
            names = {"weight": "权重", "y_angle": "Y 视场角", "x_angle": "X 视场角", "vux": "VUX", "vlx": "VLX",
                     "vuy": "VUY", "vly": "VLY"}
            lines.append(f"- {names.get(change['parameter'], change['parameter'])}修改：{where} → {_fmt(change['value'])}")
        if stage.get("ramp_step"):
            lines.append(f"- 视场爬升第 {stage['ramp_step']} 步（从上一阶段的候选继续）")
        for note in stage.get("start_bounds") or []:
            lines.append(f"- 起始值在界上／界外：S{note['surface']} {note['parameter']} = {_fmt(note['value'], 12)}"
                         f"（{note['bound']} {_fmt(note['limit'])}，{'界外' if note['position'] == 'outside' else '正在界上'}），"
                         "CODE V 将其移入界内")
        if stage.get("variables"):
            lines += ["", "| 变量 | 之前 | 之后 | 下限 | 上限 | 界内 |", "| --- | ---: | ---: | ---: | ---: | --- |"]
            for item in stage["variables"]:
                before = "∞（平面）" if item.get("before_infinite") else _fmt(item.get("before"), 12)
                after = "∞（平面）" if item.get("after_infinite") else _fmt(item.get("after"), 12)
                lines.append(f"| S{item['surface']} {item['parameter']} | {before} | "
                             f"{after} | {_fmt(item.get('lower')) if item.get('lower') is not None else '—'} | "
                             f"{_fmt(item.get('upper')) if item.get('upper') is not None else '—'} | {_fmt(item.get('within_bounds'))} |")
        if stage.get("constraints"):
            lines += ["", "| 约束 | 关系 | 目标 | 达成值（打印） | 差值 | 判定 |", "| --- | --- | ---: | ---: | ---: | --- |"]
            for item in stage["constraints"]:
                printed = item.get("printed") or {}
                lines.append(f"| {item['label']} | {item['relation']} | {_fmt(item['target'])} | "
                             f"{printed.get('value', ZH['missing'])} | {printed.get('diff', ZH['missing'])} | "
                             f"{_status(item['status'])}{'（' + item['reason'] + '）' if item.get('reason') else ''} |")
        general = stage.get("general_constraints") or {}
        lines.append("")
        for item in general.get("service_checks") or []:
            limit = (f"（限值 {_fmt(item['limit'])}，容差 {_fmt(item.get('tolerance'), 3)}）"
                     if item.get("limit") is not None else "")
            lines.append(f"- 通用约束 {item['id']}（服务复核 {item['metric']}）：{_fmt(item.get('value'))} {item.get('unit') or ''}，"
                         f"{_status(item['status'])}{limit}")
        if general.get("frozen_violations"):
            lines.append(f"- CODE V 冻结厚度警告：{'、'.join(general['frozen_violations'])}")
        for item in stage.get("solve_coupled") or []:
            lines.append(f"- 求解联动：S{item['surface']} {item['parameter']}（{item['value']}） "
                         f"{_fmt(item.get('before'), 12)} → {_fmt(item.get('after'), 12)}")
        marker = f"! stage {stage['index']} "
        start = next((i for i, command in enumerate(commands) if command.startswith(marker)), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(commands)) if commands[i].startswith("! stage ")), len(commands))
            lines += ["", "本阶段实际发送的命令（`out <文件>` 只重定向输出，复现时可省略）：", "", "```"]
            lines += commands[start + 1:end] + ["```"]
    before, after = results.get("before_first_order") or {}, results.get("after_first_order") or {}
    if before or after:
        lines += _first_order_table({key: {"before": before.get(key), "after": after.get(key)} for key in (
            "effective_focal_length", "f_number", "overall_length", "image_distance")})
    for key, label in (("before_wavefront", "AUT 前"), ("after_wavefront", "AUT 后")):
        value = results.get(key) or {}
        if value:
            data = value.get("result") or {}
            lines.append(f"- {label} WAV 加权 RMS：{_fmt(data.get('weighted_rms_waves'))} waves（{_status(value.get('state'))}）")
    return lines


def _step_section(steps: list[dict]) -> list[str]:
    lines = [ZH["s3"], ""]
    for index, step in enumerate(steps, 1):
        title = ZH["actions"].get(step["action"], step["action"])
        lines += [f"### 3.{index} {title}（`{step['tool']}`，{_status(step['status'])}）", ""]
        if step.get("source") != "codev":
            lines.append(f"- **来源：{step.get('source')}，数值没有光学意义**")
        if step.get("error"):
            lines.append(f"- 失败原因：{step['error']}")
        params, results = step.get("parameters") or {}, step.get("results") or {}
        if step["action"] == "scale":
            check = results.get("efl_check") or {}
            lines.append(f"- 目标 EFL {_fmt(params.get('target_efl'))}，系数 {_fmt(params.get('factor'), 12)}；"
                         f"缩放后 EFL {_fmt(check.get('after'), 12)}，相对偏差 {_fmt(check.get('relative_deviation'), 3)}")
            for item in results.get("solve_coupled") or []:
                lines.append(f"- 求解联动：S{item['surface']} {item['control']} 像距 {_fmt(item.get('before'))} → {_fmt(item.get('after'))}")
            lines += _first_order_table(results.get("first_order"))
        elif step["action"] == "edit":
            lines.append(f"- 修改：{params.get('name')}" + (f"；理由：{params['reason']}" if params.get("reason") else ""))
            for warning in (results.get("transaction") or {}).get("warnings", []):
                lines.append(f"- 事务说明：{warning}")
            lines += _first_order_table(results.get("first_order"))
        elif step["action"] == "glass_candidates":
            reference = params.get("reference") or {}
            lines.append(f"- 参考：{reference.get('glass', '给定 nd／νd')}（nd {_fmt(reference.get('nd'))}，νd {_fmt(reference.get('vd'), 5)}）；"
                         f"目录：{'、'.join(params.get('catalogs') or [])}")
            lines += ["", "| 目录 | 玻璃 | 代码 | nd | νd（计算） | Δnd | Δνd | 距离 |", "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
            for catalog, rows in (results.get("candidates") or {}).items():
                for row in rows:
                    lines.append(f"| {catalog} | {row['name']} | {row['code']} | {_fmt(row['nd'])} | {_fmt(row['vd'], 5)} | "
                                 f"{_fmt(row['delta_nd'], 3)} | {_fmt(row['delta_vd'], 3)} | {_fmt(row['distance'], 3)} |")
            definitions = results.get("definitions") or {}
            if definitions.get("distance"):
                lines += ["", f"距离定义：{definitions['distance']}"]
        elif step["action"] == "aut":
            lines += _aut_step(step)
        elif step["action"] == "aut_accept":
            lines.append(f"- 接受为 revision {_fmt((results or {}).get('accepted_revision'))}")
        elif step["action"] == "evaluate":
            summary = (results.get("evaluation") or {}).get("status") or {}
            lines.append("- 规格判定总体：" + ("；".join(f"{stage} {_status(value)}" for stage, value in summary.items()) or ZH["missing"]))
            lines.append(f"- 报告：`{Path(step['bundle']) / results.get('report', 'report.md')}`")
        if step["action"] != "aut":
            lines += _command_block(step)
        lines.append("")
    return lines


def _figures(steps: list[dict]) -> list[str]:
    lines = [ZH["s4"], ""]
    evaluations = [step for step in steps if step["action"] == "evaluate"]
    if not evaluations:
        return lines + [f"出图：{ZH['missing']}（没有评价步骤）。", ""]
    for step in evaluations:
        figures = (step.get("results") or {}).get("figures")
        if not figures:
            lines.append(f"- `{step['bundle']}`：{ZH['missing']}")
            continue
        lines += ["| 镜头 | 分析 | 来源 | 图像 | 对应数据 |", "| --- | --- | --- | --- | --- |"]
        for figure in figures:
            origin = "CODE V 原生" if figure["origin"] == "codev_native" else "服务按数值重绘"
            data = figure.get("plot_file") or figure.get("numbers") or ZH["missing"]
            lines.append(f"| {figure['stage']} | {figure['name']} | {origin} | "
                         f"`{Path(step['bundle']) / figure['image']}` | `{data}` |")
    return lines + ["", "原生图由 CODE V 绘制，没有对应的结构化数值；服务重绘图由同目录快照中的数值生成。图像保留在运行目录，不复制、不改名。", ""]


def _measured(steps: list[dict]) -> list[str]:
    from .compare import stage_results

    lines = [ZH["s5"], ""]
    for step in [s for s in steps if s["action"] == "evaluate"]:
        bundle = Path(step["bundle"])
        try:
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lines.append(f"- `{bundle}` 的清单：{ZH['missing']}")
            continue
        stages = list(manifest.get("inputs") or {})
        results = {stage: stage_results(bundle, manifest, stage) for stage in stages}
        header = "| 量 | " + " | ".join(stages) + " |"
        rule = "| --- | " + " | ".join("---:" for _ in stages) + " |"
        first = {stage: results[stage].get("first_order") or {} for stage in stages}
        lines += ["### 一阶参数", "", header, rule]
        for key in ("effective_focal_length", "f_number", "back_focal_length", "overall_length", "image_distance"):
            lines.append(f"| {key} | " + " | ".join(_fmt(first[stage].get(key), 12) for stage in stages) + " |")
        note = next((value.get("precision_note") for value in first.values() if value.get("precision_note")), None)
        lines += ["", f"精度：{note or ZH['missing']}", ""]
        spots = sorted({name for stage in stages for name in results[stage] if name.startswith("spot-")})
        if spots:
            lines += ["### 点列（原生 SPO，半径）", "", "| 视场 | " + " | ".join(f"{s} RMS | {s} 最大" for s in stages) + " |",
                      "| --- | " + " | ".join("---: | ---:" for _ in stages) + " |"]
            for name in spots:
                cells = []
                for stage in stages:
                    spot = results[stage].get(name) or {}
                    cells += [_fmt(spot.get("rms_radius")), _fmt(spot.get("max_radius"))]
                lines.append(f"| {name.removeprefix('spot-')} | " + " | ".join(cells) + " |")
            lines.append("")
        mtfs = {stage: results[stage].get("mtf") for stage in stages if results[stage].get("mtf")}
        if mtfs:
            lines += ["### 衍射 MTF（cycles/mm；T/S）", "", "| 视场 | 频率 | " + " | ".join(f"{s} T | {s} S" for s in mtfs) + " |",
                      "| --- | ---: | " + " | ".join("---: | ---:" for _ in mtfs) + " |"]
            reference = next(iter(mtfs.values()))
            for position, curve in enumerate(reference["curves"]):
                for index, frequency in enumerate(reference["frequencies"]):
                    cells = []
                    for value in mtfs.values():
                        other = value["curves"][position] if position < len(value["curves"]) else {}
                        cells += [_fmt((other.get("tangential") or [None] * (index + 1))[index], 4),
                                  _fmt((other.get("sagittal") or [None] * (index + 1))[index], 4)]
                    lines.append(f"| {curve['field_number']} | {_fmt(frequency)} | " + " | ".join(cells) + " |")
            lines.append("")
        waves = {stage: results[stage].get("wavefront") for stage in stages if results[stage].get("wavefront")}
        if waves:
            lines += ["### WAV（当前保存焦面，RMS waves）", "", "| 视场 | " + " | ".join(f"{s} RMS | {s} Strehl" for s in waves) + " |",
                      "| --- | " + " | ".join("---: | ---:" for _ in waves) + " |"]
            reference = next(iter(waves.values()))
            for position, field in enumerate(reference["fields"]):
                cells = []
                for value in waves.values():
                    other = value["fields"][position] if position < len(value["fields"]) else {}
                    cells += [_fmt(other.get("rms_waves")), _fmt(other.get("strehl"))]
                lines.append(f"| {field['field_number']} | " + " | ".join(cells) + " |")
            lines.append("")
        try:
            evaluation = json.loads((bundle / "evaluation.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lines += [f"规格判定：{ZH['missing']}", ""]
            continue
        judged = list(evaluation["stages"])
        lines += ["### 规格判定", "", "总体：" + "；".join(f"{s} {_status(evaluation['stages'][s]['status'])}" for s in judged), "",
                  "| 条目 | 必需 | " + " | ".join(f"{s} 值 | {s} 判定" for s in judged) + " |",
                  "| --- | --- | " + " | ".join("---: | ---" for _ in judged) + " |"]
        base = evaluation["stages"][judged[0]]
        for group in ("conditions", "requirements", "criteria"):
            for position, item in enumerate(base[group]):
                cells = []
                for stage in judged:
                    other = evaluation["stages"][stage][group][position]
                    value = "—" if "expected" in other else (_fmt(other.get("value"), 7) + (f" {other['unit']}" if other.get("unit") else ""))
                    cells += [value, _status(other["status"]) + (f"（{other['reason']}）" if other.get("reason") else "")]
                lines.append(f"| {item['id']} | {'是' if item['required'] else '否'} | " + " | ".join(cells) + " |")
        lines.append("")
    aut_steps = [s for s in steps if s["action"] == "aut"]
    for step in aut_steps:
        lines += ["### 各阶段误差函数与约束达成（见 3 节对应步骤）", "", "| 阶段 | 结束原因 | 初始 ERR. F. | 最终 ERR. F. | 约束全部满足 |",
                  "| --- | --- | ---: | ---: | --- |"]
        for stage in (step.get("results") or {}).get("stages") or []:
            lines.append(f"| {stage['name']} | {stage.get('completion') or ZH['missing']} | {_fmt(stage.get('initial_error'))} | "
                         f"{_fmt(stage.get('final_error'))} | {_fmt(stage.get('constraints_satisfied'))} |")
        lines.append("")
    if not [s for s in steps if s["action"] == "evaluate"] and not aut_steps:
        lines += [f"实测数据：{ZH['missing']}。", ""]
    return lines


def _notes(steps: list[dict], spec: dict | None) -> list[str]:
    notes = []
    if any(step["action"] == "evaluate" for step in steps):
        notes.append("CODE V `OAL` 从第 1 面量到最后一个镜面，不含像距；后截距取列表打印的 BFL，AUT 中的 `IMD` 约束则是含离焦的像距。")
        notes.append("点列、MTF 与 WAV 都在镜头当前保存的像面上计算，服务不自动改焦；像距由 `PIM` 求解时即近轴像面，"
                     "不是最佳焦面，Maréchal 等衍射判据应结合这一点阅读。")
    paths = [item.get("path") or "" for step in steps for item in step.get("inputs", []) + step.get("outputs", [])]
    if any(path and not path.isascii() for path in paths):
        notes.append("部分路径含非 ASCII 字符：各 CLI 先把输入复制到服务自有 ASCII 目录再交给 CODE V，输出由 Python 写回原路径。")
    for index, step in enumerate(steps, 1):
        if step["status"] not in {"succeeded", "complete"}:
            error = step.get("error") or ZH["missing"]
            if len(error) > 200:
                error = error[:200] + f"…（全文见 3.{index}）"
            notes.append(f"步骤 {index}（{ZH['actions'].get(step['action'], step['action'])}）状态为 {_status(step['status'])}：{error}。")
        if step.get("source") != "codev":
            notes.append(f"步骤 {index} 的来源为 {step.get('source')}，数值没有光学意义。")
        if step["action"] == "evaluate":
            for item in (step.get("results") or {}).get("failed_analyses") or []:
                notes.append(f"步骤 {index} 的评价中 {item['stage']}/{item['name']} 没有得到结果（{item['kind']}：{item['error']}）；"
                             "其余分析照常运行，规格判定中对应的量记为未知。")
        if step["action"] == "edit":
            glasses = [item for item in (step.get("parameters") or {}).get("edits", []) if item.get("parameter") == "glass"]
            if glasses:
                notes.append(f"步骤 {index} 更换的玻璃：" + "、".join(f"S{item['surface']} → {item['value']}" for item in glasses)
                             + "；换玻璃后需重新优化，结果见后续 AUT 步骤。")
        if step["action"] == "aut":
            for stage in (step.get("results") or {}).get("stages") or []:
                bad = [f"{item['label']} {item['relation']} {_fmt(item['target'])}" for item in stage.get("constraints") or []
                       if item["status"] != "satisfied"]
                if bad:
                    notes.append(f"步骤 {index} 的 AUT 阶段 {stage['name']} 约束未全部满足：{'、'.join(bad)}（候选仍可接受，但不代表达标）。")
                frozen = (stage.get("general_constraints") or {}).get("frozen_violations")
                if frozen:
                    notes.append(f"步骤 {index} 的 AUT 阶段 {stage['name']} 有 CODE V 冻结厚度警告：{'、'.join(frozen)}。")
            later = steps[index:]
            if step["status"] == "complete" and not any(s["action"] == "aut_accept" and s["_directory"] == step["_directory"] for s in later):
                notes.append(f"步骤 {index} 的 AUT 候选未被接受，后续步骤不应把它当作已提交镜头。")
    known: set = set()
    for index, step in enumerate(steps, 1):
        lenses = [item for item in step.get("inputs", []) if item.get("role") in {"lens", "initial", "final", "candidate"}]
        if not known:
            known.update(item.get("sha256") for item in lenses)
        for item in lenses:
            if item.get("sha256") not in known:
                notes.append(f"步骤 {index} 的输入 `{item.get('path')}` 不是起点或前序步骤的输出（哈希不连续），请核对来源。")
        known.update(item.get("sha256") for item in step.get("outputs", []))
    if spec and any(item.get("pending") for item in spec.get("requirements", [])):
        notes.append("规格中有待补阈值，对应条目判定为未知，总体不会判为通过。")
    if spec and spec.get("demonstration"):
        notes.append("所用规格标记为演示：阈值是演示值，不是设计要求。")
    return [ZH["s6"], ""] + ([f"- {note}" for note in notes] or ["- 无自动识别的注意事项。"]) + [""]


def render(steps: list[dict], spec: dict | None, *, title: str, generated_at: str, spec_error: str | None = None) -> str:
    lines = [ZH["title"].format(title=title), "",
             ZH["generated"].format(count=len({step["_directory"] for step in steps}), when=generated_at), ""]
    if any(step.get("source") != "codev" for step in steps):
        lines += [ZH["simulated"], ""]
    lines += _spec_section(spec, spec_error) + _materials(steps) + _step_section(steps) + _figures(steps)
    lines += _measured(steps) + _notes(steps, spec)
    return "\n".join(_tidy(lines)).rstrip() + "\n"


def _tidy(lines: list[str]) -> list[str]:
    """One blank line between blocks and after every table; code blocks are kept as they are."""
    out: list[str] = []
    in_code = False
    for line in lines:
        if not in_code:
            if line == "" and (not out or out[-1] == ""):
                continue
            if out and out[-1].startswith("|") and line and not line.startswith("|"):
                out.append("")
        if line.startswith("```"):
            in_code = not in_code
        out.append(line)
    return out


def write_walkthrough(directories: list[Path], output: Path, *, spec_path: Path | None = None,
                      title: str = "设计记录", generated_at: str | None = None) -> Path:
    output = output.resolve()
    if output.suffix.lower() != ".md" or output.exists() or not output.parent.is_dir():
        raise ValueError(f"Output must be a new .md in an existing directory: {output}")
    steps = load_runs([directory.resolve() for directory in directories])
    spec, spec_error = None, None
    if spec_path is not None:
        try:
            spec, _ = read_spec(spec_path)
        except (OSError, ValueError) as exc:
            spec_error = f"{type(exc).__name__}: {exc}"
    text = render(steps, spec, title=title, spec_error=spec_error,
                  generated_at=generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="根据执行记录生成设计记录（walkthrough）Markdown")
    parser.add_argument("--run", type=Path, action="append", required=True, help="按执行顺序给出的运行目录，可重复")
    parser.add_argument("--spec", type=Path, help="设计规格 JSON")
    parser.add_argument("--output", type=Path, required=True, help="新的 .md 路径，可含中文；已存在则拒绝")
    parser.add_argument("--title", default="设计记录")
    args = parser.parse_args(argv)
    try:
        path = write_walkthrough(args.run, args.output, spec_path=args.spec, title=args.title)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
