"""Reproducible initial/final analysis bundles through public MCP tools only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .evaluation import evaluate_spec
from .models import VIGNETTING_FACTORS, AnalysisSnapshot, LensData
from .record import execution_step, write_record
from .spec import check_lens, read_spec
from .stdio_client import StdioClient

# Same tolerances as the backend's parameter readback, without importing COM.
ABS_TOL = 1e-9
REL_TOL = 1e-7
FREQUENCIES = list(range(0, 101, 10))
PLOTS = ("layout", "spot", "mtf", "ray_aberration", "field_aberration")
ROOT = Path(__file__).resolve().parents[2]
KINDS = ("first_order", "spot_diagram", "mtf", "wavefront", "native_plot")
#: Error kinds of a task that failed on its own account: the analysis could not
#: be computed, but the session and the lens are intact, so the remaining
#: analyses still run. Anything else (a lost session, an internal error) stops.
RECOVERABLE_ERROR_KINDS = ("computation_failed", "unsupported", "parameter")


class AnalysisFailed(ValueError):
    """One analysis failed without harming the session; the run continues."""

    def __init__(self, message: str, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class AnalysisConfig:
    kinds: tuple[str, ...] = KINDS
    fields: tuple[int, ...] | None = None
    frequencies: tuple[float, ...] = tuple(FREQUENCIES)
    spot_grid: int = 7
    zoom_positions: tuple[int, ...] | None = None


def validate_config(config: AnalysisConfig) -> None:
    if not config.kinds or len(set(config.kinds)) != len(config.kinds) or any(k not in KINDS for k in config.kinds):
        raise ValueError("Analysis kinds must be a nonempty, unique subset of " + ", ".join(KINDS))
    if config.fields is not None and (not config.fields or len(set(config.fields)) != len(config.fields)
                                      or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in config.fields)):
        raise ValueError("fields must be unique positive integers")
    if config.zoom_positions is not None and (not config.zoom_positions or len(set(config.zoom_positions)) != len(config.zoom_positions)
                                              or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in config.zoom_positions)):
        raise ValueError("zoom positions must be unique positive integers")
    if isinstance(config.spot_grid, bool) or not isinstance(config.spot_grid, int) or not 2 <= config.spot_grid <= 101:
        raise ValueError("spot grid must be an integer from 2 to 101")
    if (not config.frequencies or len(config.frequencies) > 101
            or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0
                   for n in config.frequencies)
            or list(config.frequencies) != sorted(set(config.frequencies))):
        raise ValueError("MTF frequencies must be unique, finite, nonnegative and ascending (at most 101)")


def selected_positions(lens: dict, config: AnalysisConfig) -> list[int]:
    positions = list(config.zoom_positions or range(1, lens["zoom_positions"] + 1))
    if any(n > lens["zoom_positions"] for n in positions):
        raise ValueError("Requested zoom position exceeds lens zoom count")
    return positions


def selected_fields(lens: dict, config: AnalysisConfig) -> list[int]:
    available = [f["number"] for f in lens["fields"]]
    selected = list(config.fields or available)
    if any(n not in available for n in selected):
        raise ValueError("Requested field does not exist in lens")
    return selected


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def optical_state(lens: dict) -> dict:
    validated = LensData.model_validate(lens).model_dump(mode="json")
    return {k: v for k, v in validated.items()
            if k not in {"source", "path", "title", "raw_listing", "warnings"}}


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def assert_equivalent(left, right, path="state") -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            raise ValueError(f"{path}: keys changed")
        for key in left:
            assert_equivalent(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            raise ValueError(f"{path}: length changed")
        for i, (a, b) in enumerate(zip(left, right)):
            assert_equivalent(a, b, f"{path}[{i}]")
    elif (isinstance(left, (float, int)) and not isinstance(left, bool)
          and isinstance(right, (float, int)) and not isinstance(right, bool)):
        if not math.isclose(left, right, abs_tol=ABS_TOL, rel_tol=REL_TOL):
            raise ValueError(f"{path}: {left!r} != {right!r}")
    elif type(left) is not type(right) or left != right:
        raise ValueError(f"{path}: {left!r} != {right!r}")


def check_compatible(initial: dict, final: dict, allow_field_weight_difference=False,
                     *, multi_zoom=False) -> list[dict]:
    for lens in (initial, final):
        if (not multi_zoom and (lens["zoom_positions"] != 1 or lens["zoom_position"] != 1)):
            raise ValueError("Only single-zoom lenses are supported")
        if not lens["fields"] or not lens["wavelengths"]:
            raise ValueError("Fields and wavelengths must be available")
    if initial["zoom_positions"] != final["zoom_positions"]:
        raise ValueError("Zoom position counts differ")
    if initial["zoom_position"] != final["zoom_position"]:
        raise ValueError("Zoom positions differ")
    for key in ("units", "dimension_code", "wavelengths"):
        assert_equivalent(initial[key], final[key], f"compatibility.{key}")
    differences = []
    if len(initial["fields"]) != len(final["fields"]):
        raise ValueError("Field count differs")
    for a, b in zip(initial["fields"], final["fields"]):
        # Weights and vignetting factors may legitimately differ; both are reported.
        assert_equivalent({k: v for k, v in a.items() if k not in {"weight", *VIGNETTING_FACTORS}},
                          {k: v for k, v in b.items() if k not in {"weight", *VIGNETTING_FACTORS}},
                          "compatibility.field")
        try:
            assert_equivalent(a["weight"], b["weight"], "compatibility.field.weight")
        except ValueError:
            if not allow_field_weight_difference:
                raise ValueError("Field weights differ; use --allow-field-weight-difference for per-field "
                                 "comparison without aggregate scores") from None
            differences.append({"field": a["number"], "initial": a["weight"], "final": b["weight"]})
    return differences


def vignetting_differences(initial: dict, final: dict) -> list[dict]:
    """Per-field VUX/VLX/VUY/VLY that differ; allowed, but never silent."""
    differences = []
    for a, b in zip(initial["fields"], final["fields"]):
        for name in VIGNETTING_FACTORS:
            left, right = a.get(name), b.get(name)
            try:
                assert_equivalent(left, right)
            except ValueError:
                differences.append({"field": a["number"], "factor": name, "initial": left, "final": right})
    return differences


def analysis_requests(lens: dict, config: AnalysisConfig | None = None,
                      zoom_position: int = 1) -> list[tuple[str, dict]]:
    config = config or AnalysisConfig()
    fields = selected_fields(lens, config)
    selected = {"zoom_position": zoom_position,
                "field_numbers": fields,
                "wavelength_numbers": [w["number"] for w in lens["wavelengths"]]}
    requests = []
    for kind in config.kinds:
        if kind == "first_order":
            requests.append(("first_order", {"kind": kind, "options": selected.copy()}))
        elif kind == "spot_diagram":
            for field in fields:
                requests.append((f"spot-{field}", {"kind": kind, "options": dict(
                    selected, field_numbers=[field], ray_grid=config.spot_grid)}))
        elif kind == "mtf":
            requests.append(("mtf", {"kind": kind, "options": dict(
                selected, frequencies=list(config.frequencies), azimuth=0.0, mtf_type="diffraction")}))
        elif kind == "wavefront":
            if config.fields is not None:
                raise ValueError("WAV requires all fields; omit --fields or exclude wavefront")
            requests.append(("wavefront", {"kind": kind, "options": selected.copy()}))
        elif kind == "native_plot":
            if config.fields is not None:
                raise ValueError("Native plots cannot apply a field selection")
            requests.extend((f"native-{plot}", {"kind": kind, "options": {"plot_type": plot}})
                            for plot in PLOTS)
    return requests


def finite_number(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"Missing/non-finite number: {name}")


#: Largest accepted-pupil-ratio difference (fraction of the on-axis ray count) at which two WAV
#: results still count as evaluated on the same pupil; 3 percentage points, chosen with the F9 probe.
PUPIL_FRACTION_TOLERANCE = 0.03


def pupil_fractions(wavefront: dict) -> list[float] | None:
    """Rays traced per field divided by field 1's: the share of the fixed WAV pupil grid that passed.

    CODE V's WAV grid is fixed in exit space; the stop and default apertures block the part of it
    that an off-axis beam cannot pass, and those rays are neither failures nor part of the RMS
    None if the counts are missing or field 1 has none.
    """
    counts = [field.get("rays_traced") for field in wavefront.get("fields") or []]
    if not counts or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in counts):
        return None
    return [count / counts[0] for count in counts]


def equal_ray_weighted_rms(wavefront: dict, weights: list[float] | None = None) -> float | None:
    """Composite RMS without CODE V's weighting of each field's variance by its ray count.

    ``weights`` are the field weights (all 1 when omitted); the per-field RMS values are the printed
    ones, so this carries their rounding.
    """
    values = [field.get("rms_waves") for field in wavefront.get("fields") or []]
    if not values or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        return None
    weights = weights if weights is not None and len(weights) == len(values) else [1.0] * len(values)
    total = sum(weights)
    if total <= 0:
        return None
    return math.sqrt(sum(w * v * v for w, v in zip(weights, values)) / total)


def pupil_fraction_spread(left: list[float], right: list[float]) -> float | None:
    """Largest per-field difference of two accepted-pupil ratios; None if the field sets differ."""
    if len(left) != len(right):
        return None
    return max(abs(a - b) for a, b in zip(left, right))


def validate_result(snapshot: dict, task_id: str, request: dict, source: str, lens: dict) -> dict:
    # Parse the public contract as well as checking required successful payloads.
    AnalysisSnapshot.model_validate(snapshot)
    task = snapshot.get("task") or {}
    kind = request["kind"]
    if task.get("task_id") != task_id or task.get("kind") != kind:
        raise ValueError("Analysis task/request mismatch")
    if snapshot.get("source") != source or task.get("source") != source:
        raise ValueError("Analysis source mismatch")
    if task.get("state") != "succeeded" or task.get("error"):
        error = task.get("error") or {}
        if task.get("state") == "failed" and error.get("kind") in RECOVERABLE_ERROR_KINDS:
            raise AnalysisFailed(f"{error.get('kind')}: {error.get('message')}", error.get("kind"))
        raise ValueError(f"Analysis did not succeed: {task}")
    if task.get("history_only"):
        raise ValueError("Historical results cannot complete a comparison")
    if task.get("output_truncated"):
        raise AnalysisFailed("The analysis output was truncated, so the result is incomplete.", "truncated")
    result = snapshot.get(kind)
    if not isinstance(result, dict) or result.get("source") != source:
        raise ValueError(f"Missing {kind} result or wrong source")
    if not (result.get("raw_output") or task.get("raw_output")):
        raise ValueError("Missing raw analysis output")
    settings = task.get("settings") or {}
    for key, value in request["options"].items():
        assert_equivalent(value, settings.get(key), f"settings.{key}")
    expected_fields = request["options"].get("field_numbers", [f["number"] for f in lens["fields"]])
    expected_waves = [w["number"] for w in lens["wavelengths"]]
    assert_equivalent(expected_fields, settings.get("field_numbers"), "settings.fields")
    assert_equivalent(expected_waves, settings.get("wavelength_numbers"), "settings.wavelengths")
    expected_zoom = request["options"].get("zoom_position", 1)
    if settings.get("zoom_position") != expected_zoom or result.get("zoom_position") != expected_zoom:
        raise ValueError("Result zoom mismatch")
    if kind == "first_order":
        for name in ("effective_focal_length", "f_number", "overall_length", "image_distance"):
            finite_number(result.get(name), name)
        if result.get("units") != lens["units"] or not result.get("precision_note"):
            raise ValueError("Missing first-order units or precision")
    elif kind == "spot_diagram":
        if result.get("field_number") != expected_fields[0] or not result.get("size_is_radius"):
            raise ValueError("Spot field or radius convention mismatch")
        assert_equivalent(expected_waves, result.get("wavelength_numbers"), "spot.wavelengths")
        if result.get("units") != lens["units"]:
            raise ValueError("Spot units mismatch")
        for name in ("rms_radius", "max_radius", "plot_sample_count", "statistics_sample_count"):
            finite_number(result.get(name), name)
            if result[name] < 0 or (name.endswith("count") and result[name] == 0):
                raise ValueError(f"Invalid spot {name}")
    elif kind == "mtf":
        frequencies = request["options"]["frequencies"]
        assert_equivalent(frequencies, result.get("frequencies"), "mtf.frequencies")
        if result.get("frequency_unit") != "cycles/mm" or result.get("mtf_type") != "diffraction":
            raise ValueError("MTF units/type mismatch")
        assert_equivalent(0.0, result.get("azimuth"), "mtf.azimuth")
        curves = result.get("curves", [])
        if [c.get("field_number") for c in curves] != expected_fields:
            raise ValueError("MTF fields missing or mismatched")
        for curve in curves:
            assert_equivalent(expected_waves, curve.get("wavelength_numbers"), "mtf.wavelengths")
            for key in ("tangential", "sagittal", "analytic_limit"):
                if len(curve.get(key, [])) != len(frequencies):
                    raise ValueError(f"Incomplete MTF {key}")
                for value in curve[key]:
                    finite_number(value, key)
    elif kind == "wavefront":
        if result.get("focus") != "nominal" or result.get("nrd") != 20 or settings.get("wavefront_nrd") != 20:
            raise ValueError("WAV focus or sampling mismatch")
        assert_equivalent(expected_waves, result.get("wavelength_numbers"), "wavefront.wavelengths")
        reference = [w for w in lens["wavelengths"] if w["is_reference"]]
        if len(reference) != 1 or result.get("reference_wavelength_number") != reference[0]["number"]:
            raise ValueError("WAV reference wavelength mismatch")
        assert_equivalent(reference[0]["micrometers"] * 1000,
                          result.get("reference_wavelength_nm"), "wavefront.reference_nm")
        if result.get("rms_equivalent_wavelength_nm") is not None:
            finite_number(result["rms_equivalent_wavelength_nm"], "wavefront.equivalent_nm")
        fields = result.get("fields", [])
        if [field.get("field_number") for field in fields] != expected_fields:
            raise ValueError("WAV fields missing or mismatched")
        for field in fields:
            for key in ("rms_waves", "strehl", "rays_traced"):
                finite_number(field.get(key), "wavefront." + key)
            if field["rms_waves"] < 0 or not 0 <= field["strehl"] <= 1 or field["rays_traced"] <= 0:
                raise ValueError("Invalid WAV field values")
        for key in ("weighted_rms_waves", "weighted_strehl"):
            finite_number(result.get(key), "wavefront." + key)
        if result["weighted_rms_waves"] < 0 or not 0 <= result["weighted_strehl"] <= 1:
            raise ValueError("Invalid WAV composite values")
    elif result.get("plot_type") != request["options"]["plot_type"]:
        raise ValueError("Native plot type mismatch")
    return result


def collect_artifacts(result: dict, images: list, work: Path, destination: Path) -> list[Path]:
    paths = []
    image = result.get("image")
    if image is None:
        if images:
            raise ValueError("Unexpected MCP image")
        return paths
    if len(images) != 1 or images[0].get("mimeType") != image["media_type"]:
        raise ValueError("MCP image count/type mismatch")

    def local_file(value: str) -> Path:
        path = Path(value).resolve()
        if not path.is_relative_to(work.resolve()) or not path.is_file():
            raise ValueError("Result file is missing or outside the session directory")
        return path

    source = local_file(image["path"])
    image_bytes = base64.b64decode(images[0]["data"], validate=True)
    if not image_bytes or source.read_bytes() != image_bytes:
        raise ValueError("MCP image bytes differ from local file")
    target = destination / "image.png"
    target.write_bytes(image_bytes)
    paths.append(target)
    if "plot_file_path" in result and (result.get("plot_file_path") or result.get("source") == "codev"):
        if not result.get("plot_file_path"):
            raise ValueError("Missing native plot file")
        plot = local_file(result["plot_file_path"])
        if plot.stat().st_size <= 0 or plot.stat().st_size != result.get("plot_file_bytes"):
            raise ValueError("Missing/incorrect native plot size")
        target = destination / "native.PLT"
        shutil.copyfile(plot, target)
        paths.append(target)
    return paths


def poll_result(client, request: dict, timeout: float, destination: Path) -> tuple[dict, list, str]:
    deadline = time.monotonic() + timeout
    task, _ = client.call("run_analysis", {"request": request})
    write_json(destination / "submitted-task.json", task)
    task_id = task["task_id"]
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancellation = {"requested": False}
            if request["kind"] in {"spot_diagram", "native_plot"} and not client.broken:
                try:
                    cancellation = client.call("cancel_analysis", timeout=30)[0]
                except Exception as exc:
                    cancellation["error"] = str(exc)
            write_json(destination / "cancellation.json", cancellation)
            raise TimeoutError("Analysis deadline exceeded; see cancellation and cleanup records")
        snapshot, images = client.call("get_analysis", timeout=remaining)
        write_json(destination / "snapshot.json", snapshot)
        polled = snapshot.get("task") or {}
        if polled.get("task_id") != task_id:
            raise ValueError("Polled task id mismatch")
        if polled.get("state") not in {"queued", "running"}:
            return snapshot, images, task_id
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def provenance(status: dict) -> dict:
    details = status.get("details") or {}
    return {"codev_version": status.get("codev_version"),
            "lens_id": details.get("lens_id"), "revision": details.get("committed_revision"),
            "recovery_count": details.get("recovery_count")}


def git_context() -> dict:
    def git(*args):
        run = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return run.stdout.strip() if run.returncode == 0 else None
    try:
        status = git("status", "--porcelain")
        return {"commit": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}


def stage_results(bundle: Path, manifest: dict, stage: str) -> dict:
    """Successful single-zoom results of one stage, keyed by analysis name."""
    results = {}
    for entry in manifest["analyses"]:
        if entry["stage"] == stage and entry["status"] == "succeeded" and entry.get("snapshot"):
            snapshot = json.loads((bundle / entry["snapshot"]).read_text(encoding="utf-8"))
            results[entry["name"]] = snapshot[entry["request"]["kind"]]
    return results


def export_listing(lens: dict, destination: Path, bundle: Path, source: str) -> dict:
    """Write the native LIS text the lens was read from; nothing is sent to CODE V."""
    listing = lens.get("raw_listing")
    if not listing:
        if source == "codev":
            raise ValueError("Native LIS listing is missing from the lens read")
        return {"path": None, "reason": "backend returned no native listing"}
    path = destination / "lis.txt"
    path.write_text(listing, encoding="utf-8")
    return {"path": path.relative_to(bundle).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size,
            "note": "verbatim output of the read-only LIS command used by get_lens"}


def figure_index(manifest: dict) -> list[dict]:
    """Map every image to the numbers, raw text or plot file it belongs to."""
    figures = []
    for entry in manifest["analyses"]:
        paths = {Path(a["path"]).name: a for a in entry.get("artifacts", [])}
        image = paths.get("image.png")
        if image is None:
            continue
        kind = entry["request"]["kind"]
        native = kind == "native_plot"
        figures.append({
            "stage": entry["stage"], "name": entry["name"], "kind": kind,
            "plot_type": entry["request"]["options"].get("plot_type"), "status": entry["status"],
            "origin": "codev_native" if native else "service_redrawn_from_numbers",
            "image": image["path"], "image_sha256": image["sha256"],
            "plot_file": (paths.get("native.PLT") or {}).get("path"),
            "numbers": None if native else entry.get("snapshot"),
            "raw_output": (paths.get("raw-output.txt") or {}).get("path")})
    return figures


def write_evaluation(bundle: Path, manifest: dict, spec: dict) -> dict:
    stages = {}
    for stage in manifest["inputs"]:
        lens = manifest["preflight"].get(stage)
        if lens is not None:
            stages[stage] = evaluate_spec(spec, stage_results(bundle, manifest, stage), lens, manifest["source"])
    record = {"schema_version": 1, "design_spec": manifest["design_spec"],
              "run_status": manifest["status"], "stages": stages}
    write_json(bundle / "evaluation.json", record)
    return {"path": "evaluation.json", "sha256": digest(bundle / "evaluation.json"),
            "status": {stage: value["status"] for stage, value in stages.items()}}


STATUS_TEXT = {"pass": "通过", "fail": "未通过", "unknown": "未知"}


def _bounds(item: dict) -> str:
    if item.get("pending"):
        return "待补"
    if "expected" in item:
        return "与规格一致"
    low, high = item.get("minimum"), item.get("maximum")
    if low is not None and high is not None:
        return f"{low:.9g} ～ {high:.9g}"
    return f"≥ {low:.9g}" if low is not None else f"≤ {high:.9g}" if high is not None else "—"


def _value(item: dict) -> str:
    value = item.get("value")
    if "expected" in item:
        return "见 evaluation.json"
    return "—" if not isinstance(value, (int, float)) else f"{value:.7g} {item.get('unit') or ''}".strip()


def render_evaluation(bundle: Path, manifest: dict) -> list[str]:
    path = bundle / "evaluation.json"
    info = manifest.get("design_spec") or {}
    lines = ["", f"## 规格判定（{info.get('name')}，SHA-256 `{str(info.get('sha256'))[:12]}`）", ""]
    if not path.is_file():
        return lines + [f"未生成判定：{(manifest.get('evaluation') or {}).get('error', '运行未到达判定阶段')}", ""]
    record = json.loads(path.read_text(encoding="utf-8"))
    stages = list(record["stages"])
    if info.get("demonstration"):
        lines += ["**规格标记为演示：阈值是演示值，不是设计要求。**", ""]
    lines += ["计算完成与规格判定分别记录；缺数据、模拟来源与待补阈值均为未知，不算通过。"
              "数值来源 `service_calculated` 为服务按 CODE V 数据计算（边缘厚度、艾里斑直径）。", "",
              "总体：" + "；".join(f"{stage} {STATUS_TEXT[record['stages'][stage]['status']]}" for stage in stages), ""]
    header = "| 条目 | 必需 | 要求 | " + " | ".join(f"{s} 值 | {s} 判定" for s in stages) + " | 说明 |"
    lines += [header, "| --- | --- | --- | " + " | ".join("---: | ---" for _ in stages) + " | --- |"]
    first = record["stages"][stages[0]]
    for group in ("conditions", "requirements", "criteria"):
        for index, item in enumerate(first[group]):
            row = [item["id"], "是" if item["required"] else "否", _bounds(item)]
            reasons = []
            for stage in stages:
                other = record["stages"][stage][group][index]
                row += [_value(other), STATUS_TEXT[other["status"]]]
                if other.get("reason"):
                    reasons.append(f"{stage}: {other['reason']}" if len(stages) > 1 else other["reason"])
            lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row + ["；".join(reasons)]) + " |")
    lines += ["", "逐项精度、数值来源、厚度分段与畸变采样见 [evaluation.json](evaluation.json)。"
              "CODE V `OAL` 从第 1 面量到最后一个镜面，不含像距。", ""]
    return lines


def render_figures(manifest: dict) -> list[str]:
    lines = ["", "## 图与数据对应", "",
             "原生图由 CODE V 绘制，没有对应的结构化数值；服务重绘图（点列、MTF）由同目录快照中的数值生成。", "",
             "| 阶段 | 分析 | 来源 | 图像 | 对应数据 |", "| --- | --- | --- | --- | --- |"]
    for figure in manifest.get("figures", []):
        origin = "CODE V 原生" if figure["origin"] == "codev_native" else "服务按数值重绘"
        data = figure["plot_file"] or figure["numbers"] or "—"
        if figure["origin"] == "codev_native" and figure["raw_output"]:
            data = f"{data}；原生文本 {figure['raw_output']}"
        lines.append(f"| {figure['stage']} | {figure['name']} | {origin} | [{Path(figure['image']).name}]"
                     f"({figure['image']}) | {data} |")
    return lines


def render_failures(manifest: dict) -> list[str]:
    """The analyses that failed while the run went on, with the reason each gave."""
    failed = manifest.get("failed_analyses") or []
    if not failed:
        return []
    lines = ["## 失败的分析", "", "以下分析没有得到结果，其余分析照常运行；缺失的量在规格判定中记为未知，不算通过。", "",
             "| 阶段 | 分析 | 类型 | 原因 |", "| --- | --- | --- | --- |"]
    lines += [f"| {item['stage']} | {item['name']} | {item['kind']} | "
              f"{str(item['error']).replace('|', '/')} |" for item in failed]
    return lines + [""]


def pupil_notes(first: dict, second: dict | None = None) -> list[str]:
    """Report lines on the accepted pupil of one WAV result, or of an initial/final pair."""
    a = pupil_fractions(first)
    if a is None:
        return []
    lines = [""]
    if second is None:
        lines.append("接受光瞳比是各视场光线数除以视场 1 的光线数：固定的 WAV 光瞳网格中通过光阑面和默认孔径的部分；"
                     "被挡的光线不是追迹失败，也不计入 RMS。综合 RMS 按各视场光线数加权。")
        equal = equal_ray_weighted_rms(first)
        if equal is not None:
            lines.append(f"各视场等权（不含视场权重、不按光线数加权）的综合 RMS：{equal:.7g}；"
                         f"CODE V 的综合 RMS：{first['weighted_rms_waves']:.7g}。")
        return lines
    b = pupil_fractions(second)
    if b is None:
        return []
    spread = pupil_fraction_spread(a, b)
    lines.append("接受光瞳比（光线数／视场 1 的光线数）：初始 " + "、".join(f"{x:.4f}" for x in a)
                 + "；最终 " + "、".join(f"{x:.4f}" for x in b) + "。")
    if spread is not None and spread > PUPIL_FRACTION_TOLERANCE:
        lines.append(f"**光瞳不同**：接受光瞳比最多相差 {spread * 100:.1f} 个百分点（超过 "
                     f"{PUPIL_FRACTION_TOLERANCE * 100:g}），初始与最终的 RMS 不是在同一块光瞳上比较，"
                     "光线数少的一侧 RMS 会偏小；请同时核对接受光瞳比与光线数。")
    equal_a, equal_b = equal_ray_weighted_rms(first), equal_ray_weighted_rms(second)
    if equal_a is not None and equal_b is not None:
        lines.append(f"各视场等权（不含视场权重、不按光线数加权）的综合 RMS：初始 {equal_a:.7g}、最终 {equal_b:.7g}。")
    return lines


def render_single(bundle: Path, manifest: dict) -> None:
    stage = "lens"
    lines = ["# 镜头评价包", "", f"状态：{manifest['status']}；source={manifest['source']}", "",
             "数据来自镜头保存的焦面，不自动改焦。状态核对仅覆盖公共 LensData 光学字段。", ""]
    if manifest["source"] == "simulated":
        lines += ["**模拟流程：按文件名选择内置样本，不解析输入 .len；结果没有光学意义。**", ""]
    if manifest.get("error"):
        lines += [f"失败原因：{manifest['error']}", ""]
    lines += render_failures(manifest)
    listing = (manifest["inputs"].get(stage) or {}).get("listing") or {}
    if listing.get("path"):
        lines += [f"原生 LIS 文本：[{Path(listing['path']).name}]({listing['path']})"
                  f"（SHA-256 `{listing['sha256'][:12]}`）", ""]
    results = stage_results(bundle, manifest, stage)
    first = results.get("first_order")
    if first:
        lines += [f"## 一阶参数（长度单位 {first['units']}，F 数无量纲）", "", "| 参数 | 值 |", "| --- | ---: |"]
        lines += [f"| {key} | {value:.9g} |" for key, value in first.items()
                  if isinstance(value, (float, int)) and not isinstance(value, bool) and key != "zoom_position"]
        lines += ["", f"精度：{first['precision_note']}", "OAL 从第 1 面到最后一个镜面，不含像距。", ""]
    spots = [r for name, r in results.items() if name.startswith("spot-")]
    if spots:
        lines += ["## 点列统计（半径；原生统计与绘图采样分别列出）", "",
                  "| 视场 | 单位 | RMS 半径 | 最大半径 | 统计光线数 | 绘图光线数 |",
                  "| --- | --- | ---: | ---: | ---: | ---: |"]
        lines += [f"| {r['field_number']} | {r['units']} | {r['rms_radius']:.9g} | {r['max_radius']:.9g} | "
                  f"{r['statistics_sample_count']} | {r['plot_sample_count']} |" for r in spots]
        lines.append("")
    mtf = results.get("mtf")
    if mtf:
        lines += ["## 衍射 MTF（cycles/mm；T 子午，S 弧矢）", "",
                  "| 视场 | 频率 | T | S | 衍射极限 |", "| --- | ---: | ---: | ---: | ---: |"]
        for curve in mtf["curves"]:
            for i, freq in enumerate(mtf["frequencies"]):
                lines.append(f"| {curve['field_number']} | {freq:g} | {curve['tangential'][i]:.7g} | "
                             f"{curve['sagittal'][i]:.7g} | {curve['analytic_limit'][i]:.7g} |")
        lines.append("")
    wav = results.get("wavefront")
    if wav:
        lines += ["## 当前保存焦面 WAV（RMS 单位 waves）", "",
                  f"NRD={wav['nrd']}；参考波长 {wav['reference_wavelength_nm']:.6g} nm；Strehl 为 CODE V 打印的近似值。", "",
                  "| 视场 | RMS | Strehl | 光线数 | 接受光瞳比 |", "| --- | ---: | ---: | ---: | ---: |"]
        fractions = pupil_fractions(wav)
        lines += [f"| {f['field_number']} | {f['rms_waves']:.7g} | {f['strehl']:.7g} | {f['rays_traced']} | "
                  f"{'—' if fractions is None else format(fractions[i], '.4f')} |"
                  for i, f in enumerate(wav["fields"])]
        lines += [f"| 综合 | {wav['weighted_rms_waves']:.7g} | {wav['weighted_strehl']:.7g} | — | — |", ""]
        lines += pupil_notes(wav)
    lines += render_figures(manifest)
    if manifest.get("design_spec"):
        lines += render_evaluation(bundle, manifest)
    lines += ["", "## 产物与设置", "", "完整请求、实际设置、警告、版本、哈希和耗时见 [manifest.json](manifest.json)。", ""]
    for entry in manifest["analyses"]:
        lines.append(f"- {entry['name']}：{entry['status']}，{entry.get('elapsed_seconds', 0):.2f} 秒")
    (bundle / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_comparison(bundle: Path, manifest: dict) -> None:
    if manifest.get("mode") == "single":
        render_single(bundle, manifest)
        return
    lines = ["# 初始／最终分析对比", "", f"状态：{manifest['status']}；source={manifest['source']}", "",
             "数据来自镜头保存的焦面，不自动改焦。状态核对仅覆盖公共 LensData 光学字段。",
             "原生图由 CODE V 绘制，不代表与结构化数值使用相同采样。未进行衍射极限达标判定。", ""]
    if manifest["source"] == "simulated":
        lines += ["**模拟流程：按文件名选择内置样本，不解析输入 .len；不是作业镜头实测。**", ""]
    if manifest.get("error"):
        lines += [f"失败原因：{manifest['error']}", ""]
    lines += render_failures(manifest)
    if manifest.get("field_weight_differences"):
        lines += ["**视场权重不同：已显式启用逐视场比较，不计算跨视场加权总评分。**", "",
                  "| 视场 | 初始权重 | 最终权重 |", "| --- | ---: | ---: |"]
        for difference in manifest["field_weight_differences"]:
            lines.append(f"| {difference['field']} | {difference['initial']} | {difference['final']} |")
        lines.append("")
    if manifest.get("vignetting_differences"):
        lines += ["**渐晕因子不同（例如 AUT 中 SET VIG 重新计算）：离轴视场的有效光瞳不同，逐视场结果不是同光瞳比较。**", "",
                  "| 视场 | 因子 | 初始 | 最终 |", "| --- | --- | ---: | ---: |"]
        for difference in manifest["vignetting_differences"]:
            lines.append(f"| {difference['field']} | {difference['factor'].upper()} | "
                         f"{difference['initial']} | {difference['final']} |")
        lines.append("")
    entries = {(e["stage"], e["name"]): e for e in manifest["analyses"] if e["status"] == "succeeded"}

    def result(stage, name):
        entry = entries.get((stage, name)) or entries.get((stage, "z1-" + name))
        if entry is None:
            return None
        snapshot = json.loads((bundle / entry["snapshot"]).read_text(encoding="utf-8"))
        return snapshot[entry["request"]["kind"]]

    a, b = result("initial", "first_order"), result("final", "first_order")
    if a and b:
        zoom_note = "；变焦位置 1" if manifest.get("analysis_config", {}).get("zoom_positions") is not None else ""
        lines += [f"## 一阶参数（长度单位 {a['units']}，F 数无量纲{zoom_note}）", "",
                  "| 参数 | 初始 | 最终 | 最终 − 初始 |", "| --- | ---: | ---: | ---: |"]
        for key, value in a.items():
            other = b.get(key)
            if isinstance(value, (float, int)) and isinstance(other, (float, int)) and key != "zoom_position":
                lines.append(f"| {key} | {value:.9g} | {other:.9g} | {other - value:.9g} |")
        lines += ["", f"初始精度：{a['precision_note']}", f"最终精度：{b['precision_note']}", ""]
    lines += ["## 点列统计（半径；原生统计与绘图采样分别列出）", "",
              "| 变焦位置 | 视场 | 状态 | 单位 | RMS 半径 | 最大半径 | 统计光线数 | 绘图光线数 |",
              "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for entry in manifest["analyses"]:
        if entry["status"] == "succeeded" and entry["request"]["kind"] == "spot_diagram":
            r = result(entry["stage"], entry["name"])
            lines.append(f"| {r['zoom_position']} | {r['field_number']} | {entry['stage']} | {r['units']} | {r['rms_radius']:.9g} | "
                         f"{r['max_radius']:.9g} | {r['statistics_sample_count']} | {r['plot_sample_count']} |")
    a, b = result("initial", "mtf"), result("final", "mtf")
    if a and b:
        zoom_note = "；变焦位置 1" if manifest.get("analysis_config", {}).get("zoom_positions") is not None else ""
        lines += ["", f"## 衍射 MTF（cycles/mm；T 子午，S 弧矢{zoom_note}）", "",
                  "| 视场 | 频率 | 初始 T | 最终 T | 初始 S | 最终 S |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for ac, bc in zip(a["curves"], b["curves"]):
            for i, freq in enumerate(a["frequencies"]):
                lines.append(f"| {ac['field_number']} | {freq:g} | {ac['tangential'][i]:.7g} | "
                             f"{bc['tangential'][i]:.7g} | {ac['sagittal'][i]:.7g} | {bc['sagittal'][i]:.7g} |")
    a, b = result("initial", "wavefront"), result("final", "wavefront")
    if a and b:
        lines += ["", "## 当前保存焦面 WAV（RMS 单位 waves）", "",
                  f"NRD={a['nrd']}；镜头参考波长：初始 {a['reference_wavelength_nm']:.6g} nm、最终 {b['reference_wavelength_nm']:.6g} nm。",
                  "原生输出未给出多波长 RMS 的等效波长；Strehl 为 CODE V 打印的近似值，较大 RMS 时可能为 0。", "",
                  "| 视场 | 初始 RMS | 最终 RMS | 差值 | 初始 Strehl | 最终 Strehl | 初始光线数 | 最终光线数 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for first, last in zip(a["fields"], b["fields"]):
            lines.append(f"| {first['field_number']} | {first['rms_waves']:.7g} | {last['rms_waves']:.7g} | "
                         f"{last['rms_waves'] - first['rms_waves']:.7g} | {first['strehl']:.7g} | "
                         f"{last['strehl']:.7g} | {first['rays_traced']} | {last['rays_traced']} |")
        if not manifest.get("field_weight_differences"):
            lines.append(f"| 综合 | {a['weighted_rms_waves']:.7g} | {b['weighted_rms_waves']:.7g} | "
                         f"{b['weighted_rms_waves'] - a['weighted_rms_waves']:.7g} | "
                         f"{a['weighted_strehl']:.7g} | {b['weighted_strehl']:.7g} | — | — |")
        lines += pupil_notes(a, b)
    if manifest.get("analysis_config", {}).get("zoom_positions") is not None:
        lines += ["", "## 逐变焦数值对比", "",
                  "WAV 与原生绘图未纳入多变焦编排；每项请求和结果均记录 zoom_position。", ""]
        positions = manifest["analysis_config"]["zoom_positions"]
        for zoom in positions:
            lines += [f"### 变焦位置 {zoom}", "",
                      "| 指标 | 初始 | 最终 | 差值 |", "| --- | ---: | ---: | ---: |"]
            for name in ("first_order", "mtf"):
                left = entries.get(("initial", f"z{zoom}-{name}"))
                right = entries.get(("final", f"z{zoom}-{name}"))
                if not left or not right:
                    continue
                a_value = json.loads((bundle / left["snapshot"]).read_text(encoding="utf-8"))[left["request"]["kind"]]
                b_value = json.loads((bundle / right["snapshot"]).read_text(encoding="utf-8"))[right["request"]["kind"]]
                if name == "first_order":
                    for key in ("effective_focal_length", "f_number", "overall_length", "image_distance"):
                        lines.append(f"| {key} | {a_value[key]:.9g} | {b_value[key]:.9g} | "
                                     f"{b_value[key] - a_value[key]:.9g} |")
                else:
                    for ac, bc in zip(a_value["curves"], b_value["curves"]):
                        for i, freq in enumerate(a_value["frequencies"]):
                            for direction in ("tangential", "sagittal"):
                                av, bv = ac[direction][i], bc[direction][i]
                                lines.append(f"| MTF field {ac['field_number']} {freq:g} {direction} | "
                                             f"{av:.7g} | {bv:.7g} | {bv - av:.7g} |")
            lines.append("")
    if manifest.get("design_spec"):
        lines += render_figures(manifest) + render_evaluation(bundle, manifest)
    lines += ["", "## 产物与设置", "", "完整请求、实际设置、警告、版本、哈希和耗时见 [manifest.json](manifest.json)。", ""]
    for entry in manifest["analyses"]:
        lines.append(f"- {entry['stage']} / {entry['name']}：{entry['status']}，{entry.get('elapsed_seconds', 0):.2f} 秒")
        for artifact in entry.get("artifacts", []):
            lines.append(f"  - [{Path(artifact['path']).name}]({artifact['path']})")
    (bundle / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_comparison(initial: Path, final: Path | None, output_dir: Path, backend="com", timeout=300.0,
                   *, client_factory=StdioClient, allow_field_weight_difference=False,
                   config: AnalysisConfig | None = None,
                   max_analyses_per_session: int = 1,
                   spec_path: Path | None = None) -> tuple[Path, dict]:
    """Analyse one lens (``final=None``) or an initial/final pair into a new bundle.

    A design spec supplies the analysis configuration and adds a per-lens
    judgement; it never changes which lens state is analysed.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if (isinstance(max_analyses_per_session, bool) or not isinstance(max_analyses_per_session, int)
            or not 1 <= max_analyses_per_session <= 8):
        raise ValueError("max_analyses_per_session must be an integer from 1 to 8")
    spec = spec_hash = None
    if spec_path is not None:
        if config is not None:
            raise ValueError("A design spec defines the analysis configuration; do not pass another one")
        spec_path = spec_path.resolve()
        spec, spec_hash = read_spec(spec_path)
        profile = spec["evaluation"]
        config = AnalysisConfig(kinds=tuple(profile["analyses"]),
                                frequencies=tuple(float(n) for n in profile["mtf_frequencies"]),
                                spot_grid=profile["spot_grid"])
    config = config or AnalysisConfig()
    validate_config(config)
    multi_zoom = config.zoom_positions is not None
    if max_analyses_per_session > 1 and multi_zoom:
        raise ValueError("Bounded reuse currently supports single-zoom lenses only")
    if multi_zoom and any(k in config.kinds for k in ("wavefront", "native_plot")):
        raise ValueError("Multi-zoom comparison supports first_order, spot_diagram and mtf only")
    if config.fields is not None and any(k in config.kinds for k in ("wavefront", "native_plot")):
        raise ValueError("Field selection cannot be applied to WAV or native plots")
    single = final is None
    inputs = {"lens": initial.resolve()} if single else {"initial": initial.resolve(), "final": final.resolve()}
    for path in inputs.values():
        if not path.is_file() or path.suffix.lower() != ".len":
            raise ValueError(f"Expected existing .len: {path}")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    bundle = output_dir.resolve() / run_id
    bundle.mkdir(parents=True, exist_ok=False)
    work_root = ROOT / ".codev-run" / ("c-" + uuid.uuid4().hex[:8])
    if not str(work_root).isascii():
        raise ValueError("Repository work directory must have an ASCII path for CODE V 10.2")
    work_root.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "run_id": run_id, "created_at": now(), "status": "running",
                "mode": "single" if single else "pair",
                "source": "codev" if backend == "com" else "simulated", "backend": backend,
                "code": git_context(), "timeout_seconds": timeout, "work_directory": str(work_root),
                "allow_field_weight_difference": allow_field_weight_difference,
                "analysis_config": {"kinds": list(config.kinds), "fields": config.fields,
                                    "frequencies": list(config.frequencies), "spot_grid": config.spot_grid,
                                    "zoom_positions": config.zoom_positions},
                "state_tolerance": {"absolute": ABS_TOL, "relative": REL_TOL},
                "inputs": {}, "preflight": {}, "preflight_zoom": {}, "analyses": [], "failed_analyses": [],
                "performance": {"max_analyses_per_session": max_analyses_per_session,
                                "sessions": []}}
    if spec is not None:
        frozen_spec = bundle / "design-spec.json"
        shutil.copyfile(spec_path, frozen_spec)
        manifest["design_spec"] = {"path": str(spec_path), "sha256": spec_hash, "snapshot": frozen_spec.name,
                                   "name": spec["name"], "demonstration": spec.get("demonstration", False)}
    write_json(bundle / "manifest.json", manifest)
    sequence = 0
    active_reuse: dict | None = None

    def close_client(client, destination: Path, record: dict, primary_error: str | None = None) -> None:
        started = time.monotonic()
        try:
            if not client.broken:
                try:
                    status = client.call("get_status")[0]
                    record["com_calls_at_close"] = (status.get("details") or {}).get("com_calls")
                except Exception:
                    pass
            cleanup = client.close()
        except Exception as exc:
            cleanup = {"close_error": f"{type(exc).__name__}: {exc}"}
        record["release_seconds"] = round(time.monotonic() - started, 6)
        record["finished_at"] = now()
        record["cleanup_confirmed"] = not (
            cleanup.get("forced_server_stop") or cleanup.get("close_error")
            or cleanup.get("returncode") != 0
            or (cleanup.get("close_session") or {}).get("session_open") is not False
            or ((cleanup.get("close_session") or {}).get("details") or {}).get("cleanup_confirmed") is not True)
        write_json(destination / "cleanup.json", cleanup)
        if not record["cleanup_confirmed"] and primary_error is None:
            raise RuntimeError(f"Session cleanup was not confirmed: {cleanup}")

    def close_reuse(primary_error: str | None = None) -> None:
        nonlocal active_reuse
        if active_reuse is None:
            return
        group = active_reuse
        active_reuse = None
        close_client(group["client"], group["log_dir"], group["record"], primary_error)

    def session(stage: str, name: str, action, *, reuse: bool = False):
        nonlocal sequence, active_reuse
        destination = bundle / stage / name
        destination.mkdir(parents=True, exist_ok=False)
        client = None
        primary_error = None
        group = None
        reuse = reuse and max_analyses_per_session > 1
        try:
            if reuse and active_reuse is not None and active_reuse["stage"] != stage:
                close_reuse()
            if reuse and active_reuse is not None:
                group = active_reuse
                client = group["client"]
                work = group["work"]
                read_started = time.monotonic()
                opened, _ = client.call("get_lens")
                group["record"]["reuse_read_seconds"] = round(
                    group["record"].get("reuse_read_seconds", 0) + time.monotonic() - read_started, 6)
            else:
                sequence += 1
                work = work_root / str(sequence)
                log_dir = (bundle / stage / f"session-{sequence}") if reuse else destination
                log_dir.mkdir(parents=True, exist_ok=True)
                record = {"id": sequence, "stage": stage, "started_at": now(),
                          "mode": "bounded_reuse" if reuse else "isolated", "analyses": 0}
                manifest["performance"]["sessions"].append(record)
                started = time.monotonic()
                client = client_factory(work, backend, timeout, log_dir)
                record["client_initialize_seconds"] = round(time.monotonic() - started, 6)
                group = {"stage": stage, "work": work, "log_dir": log_dir,
                         "client": client, "record": record}
                if reuse:
                    active_reuse = group
                started = time.monotonic()
                opened, _ = client.call("open_lens", {"path": manifest["inputs"][stage]["working_snapshot"]})
                record["lens_open_seconds"] = round(time.monotonic() - started, 6)
            if opened.get("source") != manifest["source"]:
                raise ValueError("Lens source mismatch")
            write_json(destination / "lens-before.json", opened)
            status, _ = client.call("get_status")
            write_json(destination / "status-before.json", status)
            details = status.get("details") or {}
            group["record"]["com_calls_at_open"] = group["record"].get(
                "com_calls_at_open", details.get("com_calls"))
            group["record"]["engine_startup_seconds"] = details.get("session_startup_seconds")
            value = action(client, work, destination, opened, status)
            if reuse:
                group["record"]["analyses"] += 1
                if group["record"]["analyses"] >= max_analyses_per_session:
                    close_reuse()
            return value
        except BaseException as exc:
            primary_error = f"{type(exc).__name__}: {exc}"
            write_json(destination / "error.json", {"error": primary_error})
            raise
        finally:
            if client is not None:
                if reuse:
                    if primary_error is not None:
                        close_reuse(primary_error)
                else:
                    close_client(client, destination, group["record"], primary_error)

    try:
        if spec is not None and digest(bundle / "design-spec.json") != spec_hash:
            raise ValueError("Design spec changed while copying")
        for stage, source_path in inputs.items():
            source_hash = digest(source_path)
            copy = bundle / stage / "input.len"
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, copy)
            # Preserve sample name for the simulated backend; real CODE V reads bytes.
            sample_name = source_path.name if source_path.name.isascii() else "input.len"
            working = work_root / stage / sample_name
            working.parent.mkdir()
            shutil.copyfile(copy, working)
            if digest(copy) != source_hash or digest(working) != source_hash or digest(source_path) != source_hash:
                raise ValueError("Input changed while copying")
            manifest["inputs"][stage] = {"path": str(source_path), "sha256": source_hash,
                                         "snapshot": copy.relative_to(bundle).as_posix(),
                                         "working_snapshot": str(working)}
        for stage in inputs:
            def preflight(client, work, destination, lens, status):
                positions = {"1": lens}
                if multi_zoom:
                    for position in range(2, lens["zoom_positions"] + 1):
                        positions[str(position)], _ = client.call("get_lens", {"zoom_position": position})
                manifest["preflight_zoom"][stage] = positions
                manifest["inputs"][stage]["listing"] = export_listing(lens, destination, bundle, manifest["source"])
                return lens
            manifest["preflight"][stage] = session(stage, "preflight", preflight)
        manifest["field_weight_differences"] = [] if single else check_compatible(
            manifest["preflight"]["initial"], manifest["preflight"]["final"],
            allow_field_weight_difference, multi_zoom=multi_zoom)
        manifest["vignetting_differences"] = [] if single else vignetting_differences(
            manifest["preflight"]["initial"], manifest["preflight"]["final"])
        if single:
            lens = manifest["preflight"]["lens"]
            if not multi_zoom and (lens["zoom_positions"] != 1 or lens["zoom_position"] != 1):
                raise ValueError("Only single-zoom lenses are supported")
            if not lens["fields"] or not lens["wavelengths"]:
                raise ValueError("Fields and wavelengths must be available")
        if spec is not None:
            for stage in inputs:
                check_lens(spec, manifest["preflight"][stage])
        if multi_zoom and not single:
            manifest["field_weight_differences_by_zoom"] = {}
            for position in selected_positions(manifest["preflight"]["initial"], config):
                if position > 1:
                    manifest["field_weight_differences_by_zoom"][str(position)] = check_compatible(
                        manifest["preflight_zoom"]["initial"][str(position)],
                        manifest["preflight_zoom"]["final"][str(position)],
                        allow_field_weight_difference, multi_zoom=True)
        for stage in inputs:
            selected_fields(manifest["preflight"][stage], config)
            selected_positions(manifest["preflight"][stage], config)
        for stage in inputs:
            first_baseline = manifest["preflight"][stage]
            for zoom in selected_positions(first_baseline, config):
                baseline = manifest["preflight_zoom"][stage][str(zoom)]
                for base_name, request in analysis_requests(baseline, config, zoom):
                    name = f"z{zoom}-{base_name}" if multi_zoom else base_name
                    entry = {"stage": stage, "name": name, "request": request, "status": "running",
                             "input_sha256": manifest["inputs"][stage]["sha256"]}
                    manifest["analyses"].append(entry)
                    write_json(bundle / "manifest.json", manifest)
                    started = time.monotonic()
                    print(f"{stage}/{name}", flush=True)

                    def analyse(client, work, destination, before, status):
                        read_started = time.monotonic()
                        if multi_zoom:
                            all_before = {}
                            for position in range(1, first_baseline["zoom_positions"] + 1):
                                value, _ = client.call("get_lens", {"zoom_position": position})
                                all_before[str(position)] = value
                                assert_equivalent(optical_state(manifest["preflight_zoom"][stage][str(position)]),
                                                  optical_state(value), f"reopened_lens.z{position}")
                            before = all_before[str(zoom)]
                            write_json(destination / "lens-before.json", before)
                        entry["read_before_seconds"] = round(time.monotonic() - read_started, 6)
                        before_state = optical_state(before)
                        assert_equivalent(optical_state(baseline), before_state, "reopened_lens")
                        entry["provenance"] = provenance(status)
                        entry["before_fingerprint"] = fingerprint(before_state)
                        compute_started = time.monotonic()
                        snapshot, images, task_id = poll_result(client, request, timeout, destination)
                        entry["compute_seconds"] = round(time.monotonic() - compute_started, 6)
                        entry["task_id"] = task_id
                        entry["snapshot"] = (destination / "snapshot.json").relative_to(bundle).as_posix()
                        entry["actual_settings"] = (snapshot.get("task") or {}).get("settings")
                        result = validate_result(snapshot, task_id, request, manifest["source"], before)
                        if request["kind"] not in {"first_order", "wavefront"} and not result.get("image"):
                            raise ValueError("Successful analysis is missing its image")
                        export_started = time.monotonic()
                        collect_artifacts(result, images, work, destination)
                        raw = result.get("raw_output") or snapshot["task"]["raw_output"]
                        (destination / "raw-output.txt").write_text(raw, encoding="utf-8")
                        entry["export_seconds"] = round(time.monotonic() - export_started, 6)
                        read_started = time.monotonic()
                        after, _ = client.call("get_lens", {"zoom_position": zoom} if multi_zoom else None)
                        write_json(destination / "lens-after.json", after)
                        if multi_zoom:
                            for position in range(1, first_baseline["zoom_positions"] + 1):
                                value = after if position == zoom else client.call(
                                    "get_lens", {"zoom_position": position})[0]
                                assert_equivalent(optical_state(all_before[str(position)]), optical_state(value),
                                                  f"analysis_lens.z{position}")
                        after_status, _ = client.call("get_status")
                        entry["read_after_seconds"] = round(time.monotonic() - read_started, 6)
                        write_json(destination / "status-after.json", after_status)
                        after_state = optical_state(after)
                        entry["after_fingerprint"] = fingerprint(after_state)
                        entry["after_provenance"] = provenance(after_status)
                        before_calls = (status.get("details") or {}).get("com_calls")
                        after_calls = (after_status.get("details") or {}).get("com_calls")
                        if isinstance(before_calls, int) and isinstance(after_calls, int):
                            entry["com_calls_delta"] = after_calls - before_calls
                        assert_equivalent(before_state, after_state, "analysis_lens")
                        for key in ("lens_id", "revision"):
                            if entry["provenance"][key] != entry["after_provenance"][key]:
                                raise ValueError(f"Analysis lens {key} changed")
                        entry["warnings"] = snapshot["task"].get("warnings", []) + result.get("warnings", [])

                    try:
                        session(stage, name, analyse, reuse=True)
                        entry["status"] = "succeeded"
                    except AnalysisFailed as exc:
                        # The task failed but the session was closed normally:
                        # keep the failure, carry on with the other analyses.
                        entry["status"] = "failed"
                        entry["error"] = f"{type(exc).__name__}: {exc}"
                        entry["failure_kind"] = exc.kind
                        record = next((r for r in manifest["performance"]["sessions"]
                                       if r["id"] == sequence), {})
                        if record.get("cleanup_confirmed") is not True:
                            raise
                        manifest["failed_analyses"].append(
                            {"stage": stage, "name": name, "kind": exc.kind, "error": str(exc)})
                    except BaseException as exc:
                        entry["status"] = "failed"
                        entry["error"] = f"{type(exc).__name__}: {exc}"
                        raise
                    finally:
                        entry["session_id"] = sequence
                        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        destination = bundle / stage / name
                        entry["artifacts"] = [{"path": p.relative_to(bundle).as_posix(), "sha256": digest(p),
                                                "bytes": p.stat().st_size}
                                               for p in sorted(destination.glob("*")) if p.is_file()]
                        write_json(bundle / "manifest.json", manifest)
            close_reuse()
        if manifest["failed_analyses"]:
            manifest["status"] = "failed"
            manifest["error"] = (f"{len(manifest['failed_analyses'])} analyses failed and the rest ran: "
                                 + "; ".join(f"{item['stage']}/{item['name']} ({item['kind']})"
                                             for item in manifest["failed_analyses"]))
        else:
            manifest["status"] = "succeeded"
    except (Exception, KeyboardInterrupt) as exc:
        close_reuse(f"{type(exc).__name__}: {exc}")
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, KeyboardInterrupt):
            manifest["interrupted"] = True
    finally:
        for stage, info in manifest["inputs"].items():
            try:
                info["unchanged"] = all(digest(p) == info["sha256"] for p in (
                    inputs[stage], bundle / info["snapshot"], Path(info["working_snapshot"])))
            except OSError:
                info["unchanged"] = False
            if not info["unchanged"]:
                manifest["status"] = "failed"
                manifest["error"] = "Input or frozen snapshot changed during the run"
        if spec is not None:
            try:
                spec_unchanged = (digest(spec_path) == spec_hash
                                  and digest(bundle / "design-spec.json") == spec_hash)
            except OSError:
                spec_unchanged = False
            manifest["design_spec"]["unchanged"] = spec_unchanged
            if not spec_unchanged:
                manifest["status"] = "failed"
                manifest["error"] = "Design spec changed or could not be verified during the run"
            try:
                manifest["evaluation"] = write_evaluation(bundle, manifest, spec)
            except Exception as exc:  # the judgement never hides the run's own outcome
                manifest["evaluation"] = {"error": f"{type(exc).__name__}: {exc}"}
        manifest["figures"] = figure_index(manifest)
        manifest["finished_at"] = now()
        write_json(bundle / "manifest.json", manifest)
        render_comparison(bundle, manifest)
        write_evaluation_record(bundle, manifest)
    return bundle, manifest


def write_evaluation_record(bundle: Path, manifest: dict) -> None:
    """The evaluation step of an execution record: no lens is written."""
    commands = []
    for entry in manifest["analyses"]:
        for warning in entry.get("warnings", []):
            match = re.search(r"with the command '([^']+)'", warning)
            if match and match.group(1) not in commands:
                commands.append(match.group(1))
    write_record(bundle / "execution-record.json", [execution_step(
        action="evaluate", tool="codev_mcp.compare", status=manifest["status"], source=manifest["source"],
        inputs=[{"role": stage, "path": info["path"], "sha256": info["sha256"]}
                for stage, info in manifest["inputs"].items()]
        + ([{"role": "design_spec", "path": manifest["design_spec"]["path"],
             "sha256": manifest["design_spec"]["sha256"]}] if manifest.get("design_spec") else []),
        parameters={"mode": manifest.get("mode"), "analysis_config": manifest["analysis_config"]},
        native_commands=commands,
        command_note=("Native plot options CODE V drew in its own session; numeric analyses ran through "
                      "run_analysis (first order, SPO statistics, MTF_1FLD, WAV NOM)"),
        results={"evaluation": manifest.get("evaluation"), "figures": manifest.get("figures"),
                 "failed_analyses": manifest.get("failed_analyses"),
                 "report": "report.md" if manifest.get("mode") == "single" else "comparison.md"},
        outputs=[], bundle=str(bundle), error=manifest.get("error"))])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="通过 MCP 生成单镜头评价包或初始／最终镜头分析对比包")
    parser.add_argument("--lens", type=Path, help="单镜头评价；与 --initial/--final 二选一")
    parser.add_argument("--initial", type=Path)
    parser.add_argument("--final", type=Path)
    parser.add_argument("--spec", type=Path,
                        help="设计规格 JSON：决定分析配置并逐项判定；不可与分析配置选项同用")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codev-run" / "comparisons")
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--allow-field-weight-difference", action="store_true",
                        help="显式允许视场权重不同，仅逐视场比较，不生成加权总评分")
    parser.add_argument("--analyses", help="逗号分隔：first_order,spot_diagram,mtf,wavefront,native_plot")
    parser.add_argument("--fields", help="逗号分隔的视场编号；不适用于 WAV 和原生绘图")
    parser.add_argument("--mtf-frequencies", help="逗号分隔、升序的 cycles/mm 频率")
    parser.add_argument("--spot-grid", type=int, default=7)
    parser.add_argument("--zoom-positions", help="逗号分隔的变焦位置；多变焦仅支持数值一阶、点列、MTF")
    parser.add_argument("--max-analyses-per-session", type=int, default=1,
                        help="可选有界复用：同一镜头每个会话最多执行 1–8 项；默认 1 项独立会话")
    args = parser.parse_args(argv)
    pair = (args.initial, args.final)
    if not ((args.lens is not None and pair == (None, None)) or (args.lens is None and None not in pair)):
        parser.error("use either --lens, or both --initial and --final")
    if args.spec is not None and (args.analyses or args.fields or args.mtf_frequencies
                                  or args.zoom_positions or args.spot_grid != 7):
        parser.error("--spec defines analyses, frequencies and spot grid; drop the analysis options")
    try:
        config = None if args.spec is not None else AnalysisConfig(
            kinds=tuple(args.analyses.split(",")) if args.analyses else
                  (("first_order", "spot_diagram", "mtf") if args.zoom_positions else KINDS),
            fields=tuple(int(n) for n in args.fields.split(",")) if args.fields else None,
            frequencies=tuple(float(n) for n in args.mtf_frequencies.split(","))
                        if args.mtf_frequencies else tuple(FREQUENCIES),
            spot_grid=args.spot_grid,
            zoom_positions=tuple(int(n) for n in args.zoom_positions.split(","))
                           if args.zoom_positions else None,
        )
        bundle, manifest = run_comparison(args.lens or args.initial, args.final,
                                         args.output_dir, args.backend, args.timeout,
                                         allow_field_weight_difference=args.allow_field_weight_difference,
                                         config=config,
                                         max_analyses_per_session=args.max_analyses_per_session,
                                         spec_path=args.spec)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['status']}: {bundle}", flush=True)
    if manifest.get("error"):
        print(manifest["error"], file=sys.stderr)
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
