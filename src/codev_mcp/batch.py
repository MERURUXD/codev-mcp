"""Serial, public-MCP batch comparisons against one reference lens."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
from datetime import datetime
from pathlib import Path

from .compare import (AnalysisConfig, KINDS, ROOT, StdioClient, digest, now,
                      run_comparison, validate_config, write_json)


def input_id(path: Path) -> str:
    """Stable identifier for a resolved input path, independent of run order."""
    resolved = path.resolve()
    label = "".join(c.lower() if c.isascii() and c.isalnum() else "-" for c in resolved.stem)
    label = "-".join(part for part in label.split("-") if part)[:32] or "lens"
    return f"{label}-{hashlib.sha256(str(resolved).casefold().encode()).hexdigest()[:12]}"


def pair_metrics(bundle: Path, manifest: dict) -> list[dict]:
    entries = {(entry["stage"], entry["name"]): entry for entry in manifest["analyses"]
               if entry["status"] == "succeeded"}
    metrics = []
    for (stage, name), first in entries.items():
        if stage != "initial" or ("final", name) not in entries:
            continue
        last = entries[("final", name)]
        if first["request"] != last["request"] or first["actual_settings"] != last["actual_settings"]:
            continue
        kind = first["request"]["kind"]
        a = json.loads((bundle / first["snapshot"]).read_text(encoding="utf-8"))[kind]
        b = json.loads((bundle / last["snapshot"]).read_text(encoding="utf-8"))[kind]
        candidates = []
        if kind == "first_order":
            candidates = [(key, a.get(key), b.get(key), None) for key in (
                "effective_focal_length", "f_number", "overall_length", "image_distance")]
        elif kind == "spot_diagram":
            candidates = [(f"field-{a['field_number']}.rms_radius", a["rms_radius"], b["rms_radius"], None),
                          (f"field-{a['field_number']}.max_radius", a["max_radius"], b["max_radius"], None)]
        elif kind == "mtf":
            for ac, bc in zip(a["curves"], b["curves"]):
                if ac["field_number"] != bc["field_number"]:
                    continue
                for i, frequency in enumerate(a["frequencies"]):
                    for direction in ("tangential", "sagittal"):
                        candidates.append((f"field-{ac['field_number']}.{direction}",
                                           ac[direction][i], bc[direction][i], frequency))
        elif kind == "wavefront":
            for af, bf in zip(a["fields"], b["fields"]):
                if af["field_number"] == bf["field_number"]:
                    for key in ("rms_waves", "strehl"):
                        candidates.append((f"field-{af['field_number']}.{key}", af[key], bf[key], None))
            if not manifest.get("field_weight_differences"):
                candidates.extend((key, a[key], b[key], None) for key in ("weighted_rms_waves", "weighted_strehl"))
        for metric, initial, final, frequency in candidates:
            if isinstance(initial, (int, float)) and isinstance(final, (int, float)):
                if kind == "first_order":
                    unit = "1" if metric == "f_number" else a["units"]
                elif kind == "spot_diagram":
                    unit = a["units"]
                elif kind == "wavefront":
                    unit = "1" if metric.endswith("strehl") else "waves"
                else:
                    unit = "1"
                metrics.append({"analysis": name, "zoom_position": a.get("zoom_position", 1),
                                "metric": metric, "initial": initial, "final": final,
                                "difference": final - initial, "unit": unit,
                                "frequency": frequency,
                                "frequency_unit": a["frequency_unit"] if frequency is not None else None,
                                "settings": first["actual_settings"]})
    return metrics


def run_batch(paths: list[Path], output_dir: Path, backend="com", timeout=300.0,
              *, config: AnalysisConfig | None = None, client_factory=StdioClient,
              allow_field_weight_difference=False) -> tuple[Path, dict]:
    config = config or AnalysisConfig()
    validate_config(config)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if config.zoom_positions is not None and any(k in config.kinds for k in ("wavefront", "native_plot")):
        raise ValueError("Multi-zoom batch supports first_order, spot_diagram and mtf only")
    if config.fields is not None and any(k in config.kinds for k in ("wavefront", "native_plot")):
        raise ValueError("Field selection cannot be applied to WAV or native plots")
    if len(paths) < 2:
        raise ValueError("Batch needs a reference lens and at least one comparison lens")
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Batch input paths must be distinct")
    for path in resolved:
        if not path.is_file() or path.suffix.lower() != ".len":
            raise ValueError(f"Expected existing .len: {path}")
    batch = output_dir.resolve() / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    batch.mkdir(parents=True, exist_ok=False)
    inputs = [{"id": input_id(path), "path": str(path), "sha256": digest(path)} for path in resolved]
    report = {"schema_version": 1, "created_at": now(), "status": "running", "backend": backend,
              "reference_id": inputs[0]["id"], "inputs": inputs, "comparisons": []}
    write_json(batch / "batch.json", report)
    interrupted = False
    for item in inputs[1:]:
        bundle = None
        try:
            bundle, manifest = run_comparison(resolved[0], Path(item["path"]), batch / "pairs", backend,
                                              timeout, client_factory=client_factory,
                                              allow_field_weight_difference=allow_field_weight_difference,
                                              config=config)
            comparison = {"lens_id": item["id"], "status": manifest["status"],
                          "bundle": bundle.relative_to(batch).as_posix(),
                          "error": manifest.get("error"), "metrics": []}
            if manifest.get("interrupted") or str(manifest.get("error") or "").startswith("KeyboardInterrupt:"):
                comparison["status"] = "interrupted"
                interrupted = True
            if comparison["status"] == "succeeded":
                comparison["metrics"] = pair_metrics(bundle, manifest)
        except KeyboardInterrupt as exc:
            interrupted = True
            comparison = {"lens_id": item["id"], "status": "interrupted",
                          "bundle": bundle.relative_to(batch).as_posix() if bundle else None,
                          "error": f"KeyboardInterrupt: {exc}", "metrics": []}
        except Exception as exc:
            comparison = {"lens_id": item["id"], "status": "failed",
                          "bundle": bundle.relative_to(batch).as_posix() if bundle else None,
                          "error": f"{type(exc).__name__}: {exc}", "metrics": []}
        report["comparisons"].append(comparison)
        write_json(batch / "batch.json", report)
        if interrupted:
            break
    report["input_errors"] = []
    for item in inputs:
        try:
            item["unchanged"] = digest(Path(item["path"])) == item["sha256"]
            if not item["unchanged"]:
                item["verification_error"] = "Input SHA-256 changed during the batch"
        except OSError as exc:
            item["unchanged"] = False
            item["verification_error"] = f"{type(exc).__name__}: {exc}"
        if not item["unchanged"]:
            report["input_errors"].append({"lens_id": item["id"], "error": item["verification_error"]})
    report["status"] = ("interrupted" if interrupted else
                        "succeeded" if all(c["status"] == "succeeded" for c in report["comparisons"])
                        and all(item["unchanged"] for item in inputs) else "failed")
    report["finished_at"] = now()
    write_json(batch / "batch.json", report)
    lines = ["# 批量镜头对比", "", f"状态：{report['status']}；参考镜头：{report['reference_id']}", "",
             "每个镜头与参考镜头分别运行独立的初始／最终对比。只列同请求、同实际设置的数值差异；无跨条件排名或总评分。", ""]
    if interrupted:
        lines += ["**用户中断：后续镜头未启动；当前对的会话清理状态见对应对比包。**", ""]
    if report["input_errors"]:
        lines += ["## 输入复核失败", ""]
        for error in report["input_errors"]:
            lines.append(f"- {error['lens_id']}：{error['error']}")
        lines.append("")
    if backend == "simulated":
        lines += ["**模拟后端按文件名选样本，不解析输入镜头，也不产生光学结论。**", ""]
    for item in report["comparisons"]:
        detail = f"[详细对比]({item['bundle']}/comparison.md)" if item["bundle"] else "无对比包"
        lines += [f"## {item['lens_id']}", "", f"状态：{item['status']}；{detail}", ""]
        if item["error"]:
            lines += [f"失败或不兼容：{item['error']}", ""]
        if item["metrics"]:
            lines += ["| 变焦 | 分析 | 指标 | 频率（cycles/mm） | 单位 | 参考 | 当前 | 差值 |",
                      "| ---: | --- | --- | ---: | --- | ---: | ---: | ---: |"]
            for metric in item["metrics"]:
                frequency = "—" if metric["frequency"] is None else f"{metric['frequency']:g}"
                unit = "无量纲" if metric["unit"] == "1" else metric["unit"]
                lines.append(f"| {metric['zoom_position']} | {metric['analysis']} | {metric['metric']} | "
                             f"{frequency} | {unit} | "
                             f"{metric['initial']:.9g} | {metric['final']:.9g} | {metric['difference']:.9g} |")
            lines.append("")
    (batch / "batch.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return batch, report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="串行批量镜头分析，第一份为参考镜头")
    parser.add_argument("--lens", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codev-run" / "batches")
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--allow-field-weight-difference", action="store_true")
    parser.add_argument("--analyses")
    parser.add_argument("--fields")
    parser.add_argument("--mtf-frequencies")
    parser.add_argument("--spot-grid", type=int, default=7)
    parser.add_argument("--zoom-positions")
    args = parser.parse_args(argv)
    try:
        config = AnalysisConfig(
            kinds=tuple(args.analyses.split(",")) if args.analyses else
                  (("first_order", "spot_diagram", "mtf") if args.zoom_positions else KINDS),
            fields=tuple(int(n) for n in args.fields.split(",")) if args.fields else None,
            frequencies=tuple(float(n) for n in args.mtf_frequencies.split(","))
                        if args.mtf_frequencies else AnalysisConfig().frequencies,
            spot_grid=args.spot_grid,
            zoom_positions=tuple(int(n) for n in args.zoom_positions.split(","))
                           if args.zoom_positions else None)
        batch, report = run_batch(args.lens, args.output_dir, args.backend, args.timeout, config=config,
                                  allow_field_weight_difference=args.allow_field_weight_difference)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{report['status']}: {batch}", flush=True)
    return 0 if report["status"] == "succeeded" else 130 if report["status"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
