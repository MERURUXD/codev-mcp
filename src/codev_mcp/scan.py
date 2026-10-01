"""Finite, isolated surface-parameter scan through the nine public MCP tools."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .compare import (ROOT, assert_equivalent, collect_artifacts, digest, optical_state,
                      poll_result, provenance, validate_result, write_json)
from .evaluation import evaluate, read_config
from .listing import parse_listing
from .models import LensData, ParameterEdit
from .stdio_client import StdioClient


def validate_grid(surface: int, parameter: str, values: tuple[float, ...], timeout: float) -> None:
    if isinstance(surface, bool) or not isinstance(surface, int) or surface < 1:
        raise ValueError("surface must be a positive CODE V surface number")
    if parameter not in {"radius", "thickness"}:
        raise ValueError("parameter must be radius or thickness")
    if not 2 <= len(values) <= 16 or any(not math.isfinite(v) for v in values):
        raise ValueError("grid needs 2 to 16 finite values")
    if len(set(values)) != len(values):
        raise ValueError("grid values must be unique")
    if any(v == 0 for v in values) and parameter == "radius":
        raise ValueError("zero radius is ambiguous; use a finite nonzero radius")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")


def _confirmed_cleanup(record: dict) -> bool:
    return not (record.get("forced_server_stop") or record.get("close_error")
                or record.get("returncode") != 0
                or (record.get("close_session") or {}).get("session_open") is not False
                or ((record.get("close_session") or {}).get("details") or {}).get("cleanup_confirmed") is not True)


def _checked_provenance(status: dict) -> dict:
    details = status.get("details") or {}
    value = provenance(status)
    if (status.get("session_open") is not True or status.get("lens_open") is not True
            or details.get("session_valid") is not True or details.get("lens_state") != "ready"
            or not value["lens_id"] or type(value["revision"]) is not int):
        raise ValueError("Lens version or ready state cannot be confirmed")
    return value


class ScanSessionFailure(RuntimeError):
    def __init__(self, primary: BaseException | None, cleanup: BaseException | None,
                 input_error: BaseException | None):
        self.interrupted = any(isinstance(error, KeyboardInterrupt)
                               for error in (primary, cleanup, input_error))
        self.resource_failure = cleanup is not None
        self.sources = {key: f"{type(error).__name__}: {error}"
                        for key, error in (("primary", primary), ("cleanup", cleanup),
                                           ("input", input_error)) if error is not None}
        super().__init__("; ".join(f"{key}: {value}" for key, value in self.sources.items()))


def _surface(lens: dict, number: int) -> dict:
    matches = [item for item in lens["surfaces"] if item["number"] == number]
    if len(matches) != 1 or matches[0]["role"] in {"object", "image"}:
        raise ValueError(f"surface {number} is absent or is an object/image surface")
    return matches[0]


def _write_report(bundle: Path, manifest: dict) -> None:
    rows = manifest["samples"]
    if isinstance(manifest["analysis"], dict):
        lines = ["# 像质参数扫描", "", f"状态：{manifest['status']}；source={manifest['source']}",
                 f"输入 SHA-256：`{manifest['input']['sha256']}`",
                 f"配置 SHA-256：`{manifest['evaluation_config_sha256']}`", "",
                 "计算完成与设计达标分别记录；模拟结果没有真实光学结论。", "",
                 "| 样本 | 请求值 | 回读值 | 计算状态 | 设计判定 | 候选可交付 | 错误 |",
                 "| ---: | ---: | ---: | --- | --- | --- | --- |"]
        if manifest.get("error"):
            lines.insert(3, f"失败原因：{manifest['error']}")
        for row in rows:
            candidate = row.get("candidate") or {}
            lines.append("| " + " | ".join(str(value if value is not None else "") for value in (
                row["index"], row["requested"], row.get("actual"), row["status"],
                (row.get("evaluation") or {}).get("status"), candidate.get("deliverable", False),
                row.get("error", ""))) + " |")
        (bundle / "scan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    columns = ("index", "requested", "actual", "status", "effective_focal_length",
               "f_number", "image_distance", "overall_length", "error")
    with (bundle / "metrics.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in columns} for row in rows)
    lines = ["# 参数扫描", "", f"状态：{manifest['status']}；source={manifest['source']}"]
    if manifest.get("error"):
        lines.append(f"失败原因：{manifest['error']}")
    if manifest["input"].get("verification_error"):
        lines.append(f"输入核对错误：{manifest['input']['verification_error']}")
    lines.extend([f"输入 SHA-256：`{manifest['input']['sha256']}`", "",
                  f"半径／厚度与一阶长度指标单位：{manifest.get('baseline', {}).get('units', '未确认')}", "",
                  "每个样本从同一基线快照开启独立会话。数值来自当前焦面的一阶分析；无寻优或排序。",
                  "模拟结果不具有光学意义。", "",
                  "| 样本 | 请求值 | 回读值 | 状态 | EFL | F/# | 像距 | 总长 | 错误 |",
                  "| ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |"])
    for row in rows:
        cells = [row.get(key, "") for key in columns if key != "error"] + [row.get("error", "")]
        lines.append("| " + " | ".join(str(value if value is not None else "") for value in cells) + " |")
    (bundle / "scan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _requests(lens: dict, config: dict) -> list[tuple[str, dict]]:
    analysis = config["analysis"]
    fields = analysis["fields"] or [item["number"] for item in lens["fields"]]
    available = {item["number"] for item in lens["fields"]}
    if any(n not in available for n in fields):
        raise ValueError("Requested field absent from lens")
    waves = [item["number"] for item in lens["wavelengths"]]
    common = {"zoom_position": 1, "field_numbers": fields, "wavelength_numbers": waves}
    requests = []
    for kind in analysis["kinds"]:
        if kind == "spot_diagram":
            requests.extend((f"spot-{field}", {"kind": kind, "options": dict(common, field_numbers=[field],
                              ray_grid=analysis["spot_grid"])}) for field in fields)
        elif kind == "mtf":
            requests.append((kind, {"kind": kind, "options": dict(common,
                             frequencies=analysis["frequencies"], azimuth=0.0, mtf_type="diffraction")}))
        else:
            requests.append((kind, {"kind": kind, "options": common.copy()}))
    return requests


def _fixed_focus(lens: dict) -> None:
    listing = lens.get("raw_listing")
    if not listing:
        raise ValueError("Fixed focus cannot be confirmed: native listing absent")
    native = parse_listing(listing)
    if not native.relation_data_complete:
        raise ValueError("Fixed focus cannot be confirmed: solve/pickup section incomplete")
    # Reject all native relations until their effect on the saved image plane is established.
    if native.solves or native.pickups:
        raise ValueError("Fixed focus unsupported: image-plane solve/pickup cannot be excluded")


def _metrics(results: dict, lens: dict, config: dict) -> dict:
    metrics = {}
    first = results.get("first_order") or {}
    for name in ("effective_focal_length", "f_number", "overall_length", "back_focal_length"):
        if name in first:
            metrics[name] = {"value": first[name], "unit": "ratio" if name == "f_number" else lens["units"]}
    mtf = results.get("mtf") or {}
    for curve in mtf.get("curves", []):
        for direction in ("tangential", "sagittal"):
            for frequency, value in zip(mtf["frequencies"], curve[direction]):
                name = f"mtf.f{curve['field_number']}.{direction}.{frequency:g}"
                metrics[name] = {"value": value, "unit": "ratio", "field": curve["field_number"],
                                 "direction": direction, "frequency": frequency}
    for field in config["analysis"]["fields"] or [f["number"] for f in lens["fields"]]:
        spot = results.get(f"spot-{field}") or {}
        for name in ("rms_radius", "max_radius"):
            if name in spot:
                metrics[f"spot.{name}.f{field}"] = {"value": spot[name], "unit": lens["units"], "field": field,
                                                       "statistics_sample_count": spot.get("statistics_sample_count"),
                                                       "plot_sample_count": spot.get("plot_sample_count")}
    for field in (results.get("wavefront") or {}).get("fields", []):
        for name, unit in (("rms_waves", "waves"), ("strehl", "ratio")):
            metrics[f"wavefront.{name}.f{field['field_number']}"] = {
                "value": field[name], "unit": unit, "field": field["field_number"]}
    return metrics


def _write_quality_report(bundle: Path, manifest: dict, config: dict) -> None:
    rows = []
    curve_data = {}
    for sample in manifest["samples"]:
        metrics = sample.get("metrics", {})
        for key, item in metrics.items():
            if not isinstance(item, dict):
                continue
            rows.append({"index": sample["index"], "requested": sample["requested"], "actual": sample.get("actual"),
                         "sample_status": sample["status"], "design_status": (sample.get("evaluation") or {}).get("status"),
                         "metric": key, "value": item.get("value"), "unit": item.get("unit"),
                         "field": item.get("field"), "direction": item.get("direction"), "frequency": item.get("frequency")})
            curve_data.setdefault(key, {"unit": item.get("unit"), "field": item.get("field"),
                                        "direction": item.get("direction"), "frequency": item.get("frequency"),
                                        "points": []})["points"].append({"index": sample["index"],
                                           "parameter": sample["requested"], "value": item.get("value") if sample["status"] == "succeeded" else None})
    columns = ("index", "requested", "actual", "sample_status", "design_status", "metric", "value", "unit",
               "field", "direction", "frequency")
    with (bundle / "quality-metrics.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    for curve in curve_data.values():
        present = {point["index"] for point in curve["points"]}
        curve["points"].extend({"index": row["index"], "parameter": row["requested"], "value": None}
                               for row in manifest["samples"] if row["index"] not in present)
        curve["points"].sort(key=lambda point: point["index"])
    write_json(bundle / "curves.json", curve_data)
    write_json(bundle / "evaluation-config.json", config)
    chart_dir = bundle / "charts"
    chart_dir.mkdir(exist_ok=True)
    for key, curve in curve_data.items():
        valid = [(i, point["value"]) for i, point in enumerate(curve["points"])
                 if type(point["value"]) in (int, float) and math.isfinite(point["value"])]
        if not valid:
            continue
        values = [value for _, value in valid]
        low, high = min(values), max(values)
        if low == high:
            low -= 1
            high += 1
        count = len(curve["points"])
        x = lambda i: 60 + 680 * i / max(1, count - 1)
        y = lambda value: 370 - 300 * (value - low) / (high - low)
        pieces = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="430" viewBox="0 0 800 430">',
                  '<rect width="800" height="430" fill="white"/>',
                  '<path d="M60 50 V370 H740" fill="none" stroke="#475569"/>',
                  f'<text x="60" y="28" font-size="16">{html.escape(key)} ({html.escape(str(curve["unit"]))})</text>']
        for index, point in enumerate(curve["points"]):
            value = point["value"]
            pieces.append(f'<text x="{x(index):.1f}" y="395" font-size="12" text-anchor="middle">{point["parameter"]:g}</text>')
            if value is None:
                continue
            pieces.append(f'<circle cx="{x(index):.1f}" cy="{y(value):.1f}" r="4" fill="#2563eb"/>')
            if index and curve["points"][index - 1]["value"] is not None:
                prior = curve["points"][index - 1]["value"]
                pieces.append(f'<path d="M{x(index-1):.1f} {y(prior):.1f} L{x(index):.1f} {y(value):.1f}" stroke="#2563eb" fill="none"/>')
        pieces.append('</svg>')
        name = hashlib.sha256(key.encode()).hexdigest()[:12] + ".svg"
        (chart_dir / name).write_text("\n".join(pieces), encoding="utf-8")
        curve["chart"] = f"charts/{name}"
    write_json(bundle / "curves.json", curve_data)


def run_scan(source: Path, output_dir: Path, backend: str, timeout: float,
             surface: int, parameter: str, values: tuple[float, ...],
             *, client_factory=StdioClient, config_path: Path | None = None) -> tuple[Path, dict]:
    validate_grid(surface, parameter, values, timeout)
    config, config_hash = read_config(config_path) if config_path is not None else (None, None)
    if config is not None and any(n > len(values) for n in config["export_indices"]):
        raise ValueError("export index exceeds sample count")
    source = source.resolve()
    if not source.is_file() or source.suffix.lower() != ".len":
        raise ValueError(f"Expected existing .len: {source}")
    if backend not in {"com", "simulated"}:
        raise ValueError("backend must be com or simulated")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    bundle = output_dir.resolve() / run_id
    bundle.mkdir(parents=True, exist_ok=False)
    work_root = ROOT / ".codev-run" / ("s-" + uuid.uuid4().hex[:8])
    if not str(work_root).isascii():
        raise ValueError("Repository work directory must have an ASCII path for CODE V 10.2")
    work_root.mkdir(parents=True, exist_ok=False)
    frozen = bundle / "input.len"
    shutil.copyfile(source, frozen)
    initial_hash = digest(source)
    manifest = {"schema_version": 1, "run_id": run_id, "status": "running",
                "source": "codev" if backend == "com" else "simulated",
                "input": {"path": str(source), "snapshot": "input.len", "sha256": initial_hash},
                "grid": {"surface": surface, "parameter": parameter, "values": list(values)},
                "analysis": config["analysis"] if config else "first_order",
                "evaluation_config_sha256": config_hash,
                "timeout_seconds": timeout, "samples": []}
    write_json(bundle / "manifest.json", manifest)
    try:
        if digest(frozen) != initial_hash or digest(source) != initial_hash:
            raise ValueError("Input changed while copying")
        # Preflight is read-only. Each subsequent sample gets its own frozen input copy.
        def session(index: int, destination: Path, action, input_path=frozen, expected_hash=initial_hash):
            work = work_root / str(index)
            work.mkdir(parents=True, exist_ok=False)
            name = source.name if source.name.isascii() else "input.len"
            copy = work / name
            shutil.copyfile(input_path, copy)
            if digest(copy) != expected_hash:
                raise ValueError("Frozen input copy changed")
            client = None
            cleanup = None
            primary_error = None
            result = None
            try:
                client = client_factory(work, backend, timeout, destination)
                opened, _ = client.call("open_lens", {"path": str(copy)})
                LensData.model_validate(opened)
                if opened.get("source") != manifest["source"]:
                    raise ValueError("Lens source mismatch")
                write_json(destination / "lens-before.json", opened)
                result = action(client, opened, work)
            except BaseException as exc:
                primary_error = exc
            finally:
                cleanup_error = None
                input_error = None
                if client is not None:
                    try:
                        cleanup = client.close()
                        write_json(destination / "cleanup.json", cleanup)
                        if not _confirmed_cleanup(cleanup):
                            raise RuntimeError(f"Session cleanup not confirmed: {cleanup}")
                    except BaseException as exc:
                        cleanup_error = exc
                try:
                    if digest(copy) != expected_hash:
                        raise ValueError("Sample input copy changed")
                except BaseException as exc:
                    input_error = exc
            if primary_error or cleanup_error or input_error:
                raise ScanSessionFailure(primary_error, cleanup_error, input_error)
            return result

        preflight_dir = bundle / "preflight"
        preflight_dir.mkdir()
        baseline = session(0, preflight_dir, lambda _client, lens, _work: lens)
        if baseline["zoom_positions"] != 1 or baseline["zoom_position"] != 1:
            raise ValueError("S1 supports single-zoom lenses only")
        selected = _surface(baseline, surface)
        if selected[parameter] is None or selected[f"{parameter}_is_infinite"]:
            raise ValueError("S1 requires a finite baseline parameter")
        if not baseline["fields"] or not baseline["wavelengths"]:
            raise ValueError("Lens fields and wavelengths are required")
        if config is not None:
            if backend == "com":
                _fixed_focus(baseline)
            if parameter == "thickness":
                following = [item for item in baseline["surfaces"] if item["number"] == surface + 1]
                if selected.get("glass") is not None or len(following) != 1 or following[0]["role"] == "image":
                    raise ValueError("Selected thickness must be an internal air space, not an image distance")
            _requests(baseline, config)
            if any(r["unit"] in {"mm", "cm", "inch"} and r["unit"] != baseline["units"]
                   for r in config["requirements"]):
                raise ValueError("Requirement length unit differs from lens unit")
        manifest["baseline"] = {"value": selected[parameter], "units": baseline["units"]}
        write_json(bundle / "manifest.json", manifest)
        for index, value in enumerate(values, 1):
            destination = bundle / f"sample-{index:03d}"
            destination.mkdir()
            row = {"index": index, "requested": value, "status": "running"}
            manifest["samples"].append(row)
            started = time.monotonic()
            try:
                def sample(client, opened, work):
                    assert_equivalent(optical_state(baseline), optical_state(opened), "baseline")
                    status_before, _ = client.call("get_status")
                    before_provenance = _checked_provenance(status_before)
                    edit = ParameterEdit(target="surface", surface=surface,
                                         parameter=parameter, value=value)
                    update, _ = client.call("update_lens", {"request": {"edits": [edit.model_dump(mode="json")]}})
                    write_json(destination / "update.json", update)
                    if (update.get("source") != manifest["source"] or update.get("rolled_back")
                            or not update.get("session_valid") or len(update.get("outcomes", [])) != 1
                            or not update["outcomes"][0].get("applied")):
                        raise ValueError("Sample edit was rejected or rolled back")
                    edited, _ = client.call("get_lens")
                    write_json(destination / "lens-edited.json", edited)
                    actual = _surface(edited, surface)[parameter]
                    assert_equivalent(value, actual, "edited_value")
                    row["actual"] = actual
                    status_edited, _ = client.call("get_status")
                    row["provenance"] = _checked_provenance(status_edited)
                    if row["provenance"]["lens_id"] != before_provenance["lens_id"]:
                        raise ValueError("Lens identity changed during edit")
                    if row["provenance"]["revision"] <= before_provenance["revision"]:
                        raise ValueError("Lens revision did not advance after edit")
                    request = {"kind": "first_order", "options": {
                        "zoom_position": 1,
                        "field_numbers": [f["number"] for f in edited["fields"]],
                        "wavelength_numbers": [w["number"] for w in edited["wavelengths"]]}}
                    requests = _requests(edited, config) if config else [("first_order", request)]
                    results = {}
                    for name, request in requests:
                        result_dir = destination / name if config else destination
                        result_dir.mkdir(exist_ok=True)
                        write_json(result_dir / "request.json", request)
                        snapshot, images, task_id = poll_result(client, request, timeout, result_dir)
                        result = validate_result(snapshot, task_id, request, manifest["source"], edited)
                        if config:
                            collect_artifacts(result, images, work, result_dir)
                        elif images:
                            raise ValueError("Unexpected image in first-order result")
                        results[name] = result
                        row.setdefault("task_ids", {})[name] = task_id
                        current_status, _ = client.call("get_status")
                        if _checked_provenance(current_status) != row["provenance"]:
                            raise ValueError("Lens provenance changed during analysis")
                    result = results.get("first_order")
                    after, _ = client.call("get_lens")
                    write_json(destination / "lens-after.json", after)
                    assert_equivalent(optical_state(edited), optical_state(after), "analysis_lens")
                    after_status, _ = client.call("get_status")
                    if _checked_provenance(after_status) != row["provenance"]:
                        raise ValueError("Lens provenance changed during analysis")
                    if config:
                        row["metrics"] = _metrics(results, edited, config)
                        row["evaluation"] = evaluate(config["requirements"], results, edited, manifest["source"])
                        if index in config["export_indices"]:
                            target = work / "candidate.len"
                            saved, _ = client.call("save_lens_as", {"path": str(target)})
                            if (saved.get("source") != manifest["source"] or saved.get("overwritten")
                                    or saved.get("path") != str(target) or not target.is_file()
                                    or target.stat().st_size <= 0):
                                raise ValueError("Candidate save not confirmed")
                            candidate = destination / "candidate.len"
                            shutil.copyfile(target, candidate)
                            row["candidate"] = {"path": str(candidate), "sha256": digest(candidate),
                                                "source_sha256": initial_hash, "config_sha256": config_hash,
                                                "deliverable": False}
                            saved_status, _ = client.call("get_status")
                            if _checked_provenance(saved_status) != row["provenance"]:
                                raise ValueError("Lens provenance changed during candidate save")
                    else:
                        row["task_id"] = task_id
                        row["precision_note"] = result["precision_note"]
                        for key in ("effective_focal_length", "f_number", "image_distance", "overall_length"):
                            row[key] = result[key]
                session(index, destination, sample)
                if config and row.get("candidate"):
                    candidate = Path(row["candidate"]["path"])
                    candidate_hash = row["candidate"]["sha256"]
                    reread_dir = destination / "reopen"
                    reread_dir.mkdir()
                    def reopen(client, reopened, _work):
                        opened_provenance = _checked_provenance(client.call("get_status")[0])
                        if _surface(reopened, surface)[parameter] is None:
                            raise ValueError("Candidate parameter missing after reopen")
                        assert_equivalent(row["actual"], _surface(reopened, surface)[parameter], "candidate parameter")
                        recorded = json.loads((destination / "lens-edited.json").read_text(encoding="utf-8"))
                        assert_equivalent(optical_state(recorded), optical_state(reopened), "candidate lens")
                        results = {}
                        for name, request in _requests(reopened, config):
                            sub = reread_dir / name
                            sub.mkdir()
                            write_json(sub / "request.json", request)
                            snapshot, images, task_id = poll_result(client, request, timeout, sub)
                            result = validate_result(snapshot, task_id, request, manifest["source"], reopened)
                            collect_artifacts(result, images, _work, sub)
                            results[name] = result
                            if _checked_provenance(client.call("get_status")[0]) != opened_provenance:
                                raise ValueError("Reopened lens version changed during analysis")
                        reread_lens, _ = client.call("get_lens")
                        assert_equivalent(optical_state(reopened), optical_state(reread_lens), "reopened analysis lens")
                        fresh = _metrics(results, reopened, config)
                        for key, item in row["metrics"].items():
                            assert_equivalent(item["value"], fresh[key]["value"], "candidate metric." + key)
                        assessment = evaluate(config["requirements"], results, reopened, manifest["source"])
                        if assessment["status"] != row["evaluation"]["status"]:
                            raise ValueError("Candidate design verdict changed on reopen")
                        row["candidate"]["recomputed"] = True
                    session(100 + index, reread_dir, reopen, candidate, candidate_hash)
                    if digest(candidate) != candidate_hash:
                        raise ValueError("Candidate changed during recomputation")
                    row["candidate"]["deliverable"] = True
                row["status"] = "succeeded"
            except (Exception, KeyboardInterrupt) as exc:
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, ScanSessionFailure):
                    row["error_sources"] = exc.sources
                if isinstance(exc, KeyboardInterrupt) or (isinstance(exc, ScanSessionFailure) and exc.interrupted):
                    manifest["interrupted"] = True
                    break
                if isinstance(exc, ScanSessionFailure) and exc.resource_failure:
                    manifest["cleanup_unconfirmed"] = True
                    break
                if isinstance(exc, ScanSessionFailure) and "input" in exc.sources:
                    break
            finally:
                row["elapsed_seconds"] = round(time.monotonic() - started, 3)
                write_json(bundle / "manifest.json", manifest)
        manifest["status"] = "failed" if any(r["status"] != "succeeded" for r in manifest["samples"]) else "succeeded"
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, ScanSessionFailure):
            manifest["error_sources"] = exc.sources
            manifest["interrupted"] = exc.interrupted
            manifest["cleanup_unconfirmed"] = exc.resource_failure
        elif isinstance(exc, KeyboardInterrupt):
            manifest["interrupted"] = True
    finally:
        if config_path is not None:
            try:
                manifest["evaluation_config_unchanged"] = digest(config_path) == config_hash
            except OSError as exc:
                manifest["evaluation_config_unchanged"] = False
                manifest["evaluation_config_verification_error"] = f"{type(exc).__name__}: {exc}"
            if not manifest["evaluation_config_unchanged"]:
                manifest["status"] = "failed"
                manifest["error"] = "Evaluation config changed or could not be verified during scan"
        try:
            manifest["input"]["unchanged"] = (digest(source) == initial_hash
                                                and digest(frozen) == initial_hash)
        except OSError as exc:
            manifest["input"]["unchanged"] = False
            manifest["input"]["verification_error"] = f"{type(exc).__name__}: {exc}"
        if not manifest["input"]["unchanged"]:
            manifest["status"] = "failed"
            manifest["error"] = "Input or frozen snapshot changed or could not be verified during scan"
        if not manifest["input"]["unchanged"] or manifest.get("evaluation_config_unchanged") is False:
            for sample in manifest["samples"]:
                if sample.get("candidate"):
                    sample["candidate"]["deliverable"] = False
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(bundle / "manifest.json", manifest)
        _write_report(bundle, manifest)
        if config is not None:
            _write_quality_report(bundle, manifest, config)
    return bundle, manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从固定基线执行有限半径/厚度参数扫描")
    parser.add_argument("--lens", type=Path, required=True)
    parser.add_argument("--surface", type=int, required=True)
    parser.add_argument("--parameter", choices=("radius", "thickness"), required=True)
    parser.add_argument("--values", required=True, help="逗号分隔的固定网格数值（2～16 个）")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codev-run" / "scans")
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--config", type=Path, help="versioned image-quality JSON config")
    args = parser.parse_args(argv)
    try:
        values = tuple(float(part) for part in args.values.split(","))
        bundle, manifest = run_scan(args.lens, args.output_dir, args.backend, args.timeout,
                                    args.surface, args.parameter, values, config_path=args.config)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['status']}: {bundle}")
    if manifest.get("error"):
        print(manifest["error"], file=sys.stderr)
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
