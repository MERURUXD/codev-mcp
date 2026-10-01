"""Compare several design schemes from one baseline lens (E6).

A scheme set lists schemes; each one is *typed edits → staged AUT → candidate*
on its own copy of the same baseline (a glass swap, another vignetting or field
set, another constraint set through its own AUT spec). The schemes run one after
the other (or, with ``--jobs N``, N at a time, each in its own worker process and
CODE V session) through the existing tools (``codev_mcp.edit`` for the typed edits,
``codev_mcp.aut`` for the staged AUT, optionally ``codev_mcp.compare`` for the
design spec judgement); nothing new is sent to CODE V and nothing is accepted.

The summary gathers the error function, the WAV result, the first order data and
the spec verdict, says which quantities can be compared directly (an error
function or a WAV value only compares between schemes with the same fields,
weights, vignetting factors, wavelengths, pupil and error function settings),
and names at most one scheme as a recommendation. Accepting a candidate stays
the separate ``python -m codev_mcp.aut accept`` step.

    python -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json \\
        --output-dir out [--design-spec spec.json] [--jobs 2]

``--jobs`` defaults to 1 (in-process, one scheme after the other). With more,
every scheme runs in a worker process (``python -m codev_mcp.schemes _scheme``)
that does exactly what the serial loop does for it, so the isolation, cleanup and
interrupt behaviour of one scheme is the same; only the scheduling is new (F7).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import aut as aut_module
from .aut_spec import expand_field_ramp, read_aut_spec, validate_aut_spec
from .compare import (PUPIL_FRACTION_TOLERANCE, digest, equal_ray_weighted_rms, pupil_fraction_spread,
                      pupil_fractions, run_comparison, write_json)
from .edit import run_edit
from .models import FieldSetReplacement, ParameterEdit
from .record import execution_step, write_record

SCHEMA_VERSION = 1
MAX_SCHEMES = 8
#: Most schemes that run at the same time: as many as a scheme set can hold (probed with 8 sessions, F6/F7).
MAX_JOBS = MAX_SCHEMES
#: Seconds a worker gets to clean up after an interrupt before it is stopped and marked in doubt.
INTERRUPT_GRACE_SECONDS = 180
POLL_SECONDS = 0.5
NAME = re.compile(r"[A-Za-z0-9_.-]{1,24}")
#: An error that leaves the CODE V processes in doubt stops the whole set.
CLEANUP_DOUBT = re.compile(r"cleanup(?! remaining: \[\])", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_scheme_set(path: Path) -> tuple[dict, str]:
    """Validate a scheme set file; every scheme's own AUT spec path is read later."""
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Scheme file is not UTF-8 JSON: {exc}") from None
    allowed = {"schema_version", "kind", "name", "description", "schemes"}
    if (not isinstance(payload, dict) or set(payload) - allowed or payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("kind") != "scheme_set" or not isinstance(payload.get("name"), str)
            or not payload["name"].strip()):
        raise ValueError("Scheme file needs schema_version 1, kind scheme_set, a name and schemes")
    schemes = payload.get("schemes")
    if not isinstance(schemes, list) or not 1 <= len(schemes) <= MAX_SCHEMES:
        raise ValueError(f"A scheme set needs 1 to {MAX_SCHEMES} schemes")
    names = set()
    for scheme in schemes:
        if not isinstance(scheme, dict) or set(scheme) - {"name", "reason", "edits", "field_set", "aut_spec"}:
            raise ValueError("A scheme takes name, reason, edits or field_set, and aut_spec")
        if not isinstance(scheme.get("name"), str) or not NAME.fullmatch(scheme["name"]):
            raise ValueError("Scheme names are 1-24 letters, digits, dot, underscore or minus")
        if scheme["name"] in names:
            raise ValueError(f"Duplicate scheme name {scheme['name']}")
        names.add(scheme["name"])
        if "edits" in scheme and "field_set" in scheme:
            raise ValueError(f"Scheme {scheme['name']}: give edits or a field_set, not both")
        if "edits" in scheme:
            if not isinstance(scheme["edits"], list) or not 1 <= len(scheme["edits"]) <= 100:
                raise ValueError(f"Scheme {scheme['name']}: edits needs 1 to 100 records")
            scheme["edits"] = [ParameterEdit.model_validate(item).model_dump(mode="json", exclude_none=True)
                               for item in scheme["edits"]]
        if "field_set" in scheme:
            scheme["field_set"] = FieldSetReplacement.model_validate(scheme["field_set"]).model_dump(
                mode="json", exclude_none=True)
        if "aut_spec" in scheme and not isinstance(scheme["aut_spec"], str):
            raise ValueError(f"Scheme {scheme['name']}: aut_spec is a file path relative to the scheme file")
        if "reason" in scheme and not isinstance(scheme["reason"], str):
            raise ValueError(f"Scheme {scheme['name']}: reason must be text")
    return payload, hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------------------ metrics


def _signature(snapshot: dict | None, error_function: dict | None = None) -> str | None:
    """What an error function or WAV value depends on: fields, weights, vignetting, wavelengths, pupil."""
    if not snapshot:
        return None
    fields = [[item.get(key) for key in ("x_angle", "y_angle", "weight", "vux", "vlx", "vuy", "vly")]
              for item in snapshot["zooms"][0]["fields"]]
    waves = [[item.get("micrometers"), item.get("weight")] for item in snapshot.get("wavelengths", [])]
    payload = {"fields": fields, "wavelengths": waves, "reference": snapshot.get("reference_wavelength"),
               "aperture": [snapshot.get("aperture_kind"), snapshot.get("aperture_value")]}
    if error_function is not None:
        payload["error_function"] = {key: error_function.get(key) for key in ("DEL", "WTA")}
    return json.dumps(payload, sort_keys=True, default=str)


def extract_metrics(report: dict) -> dict[str, Any]:
    """The numbers of one AUT report that the summary compares.

    Only two WAV quantities are derived from the printed values: the accepted-pupil ratios and the
    composite RMS without the ray-count weighting (see ``compare.pupil_fractions``).
    """
    stages = report.get("stages") or []
    last = stages[-1] if stages else {}
    function = ((report.get("spec") or {}).get("stages") or [{}])[-1].get("error_function")
    snapshot = report.get("candidate_snapshot")
    order = report.get("after_first_order") or {}
    start_order = report.get("before_first_order") or {}
    wave = (report.get("after_wavefront") or {})
    start_wave = (report.get("before_wavefront") or {})
    wave_result = wave.get("result") if wave.get("state") == "succeeded" else None
    start_wave_result = start_wave.get("result") if start_wave.get("state") == "succeeded" else None
    constraints = last.get("constraints") or []
    field_weights = [item.get("weight") for item in ((snapshot or {}).get("zooms") or [{}])[0].get("fields", [])]
    if any(isinstance(w, bool) or not isinstance(w, (int, float)) for w in field_weights):
        field_weights = None
    return {
        "stages": len(stages), "stages_succeeded": sum(1 for stage in stages if stage.get("status") == "succeeded"),
        "initial_error": report.get("initial_error"), "final_error": report.get("final_error"),
        "target_reached": report.get("target_reached"), "stop_reason": report.get("stop_reason"),
        "all_constraints_satisfied": report.get("all_constraints_satisfied"),
        "final_constraints_satisfied": report.get("final_constraints_satisfied"),
        "explicit_bounds_satisfied": report.get("explicit_bounds_satisfied"),
        "violated_constraints": [item["label"] for item in constraints if item.get("status") == "violated"],
        "unknown_constraints": [item["label"] for item in constraints if item.get("status") == "unknown"],
        "first_order": {key: order.get(key) for key in (
            "effective_focal_length", "back_focal_length", "f_number", "overall_length", "image_distance")},
        "first_order_start": {key: start_order.get(key) for key in (
            "effective_focal_length", "back_focal_length", "f_number", "overall_length", "image_distance")},
        "wavefront": ({"weighted_rms_waves": wave_result["weighted_rms_waves"],
                       "weighted_strehl": wave_result["weighted_strehl"],
                       "pupil_fractions": pupil_fractions(wave_result),
                       "equal_field_rms_waves": equal_ray_weighted_rms(wave_result, field_weights)}
                      if wave_result else None),
        "wavefront_start": ({"weighted_rms_waves": start_wave_result["weighted_rms_waves"],
                             "weighted_strehl": start_wave_result["weighted_strehl"]} if start_wave_result else None),
        "wave_signature": _signature(snapshot),
        "error_signature": _signature(snapshot, function),
    }


def comparability(schemes: list[dict]) -> dict[str, Any]:
    """Which quantities may be put side by side, and why the others may not."""
    done = [item for item in schemes if item.get("metrics")]

    def groups(key: str, present: Callable[[dict], bool]) -> list[list[str]]:
        found: dict[str, list[str]] = {}
        for item in done:
            if present(item["metrics"]) and item["metrics"].get(key) is not None:
                found.setdefault(item["metrics"][key], []).append(item["name"])
        return sorted(found.values(), key=lambda names: (-len(names), names))

    result = {
        "error_function": groups("error_signature", lambda m: m["final_error"] is not None),
        "wavefront": groups("wave_signature", lambda m: m["wavefront"] is not None),
        "first_order": [[item["name"] for item in done]] if done else [],
        "spec_verdict": [[item["name"] for item in done if item.get("evaluation")]],
        "pupil": pupil_groups(done),
    }
    notes = []
    for label, key in (("误差函数（ERR. F.）", "error_function"), ("WAV 加权 RMS／Strehl", "wavefront")):
        if len(result[key]) > 1:
            notes.append(f"{label}不能直接比较：方案按视场角、视场权重、渐晕因子、波长（含权重）、孔径"
                         + ("与误差函数设置" if key == "error_function" else "")
                         + "分成 " + "；".join("、".join(names) for names in result[key]) + " 几组，只有组内可比。")
    if len(result["pupil"]) > 1:
        notes.append("WAV RMS 的光瞳不同：各视场接受光瞳比（光线数／视场 1 的光线数）相差超过 "
                     f"{PUPIL_FRACTION_TOLERANCE * 100:g} 个百分点，方案分成 "
                     + "；".join("、".join(names) for names in result["pupil"])
                     + " 几组；光线数少的方案 RMS 偏小，只有组内直接可比，应同时核对接受光瞳比与光线数。")
    result["notes"] = notes
    return result


def pupil_groups(done: list[dict]) -> list[list[str]]:
    """Schemes whose WAV evaluated the same pupil: every field's accepted ratio within the tolerance.

    Greedy in the listed order, against the first member of each group, so the result is deterministic.
    A scheme without a WAV value, or with a different field count, is left out of the grouping.
    """
    groups: list[tuple[list[float], list[str]]] = []
    for item in done:
        fractions = ((item["metrics"].get("wavefront") or {}).get("pupil_fractions"))
        if not fractions:
            continue
        for reference, names in groups:
            spread = pupil_fraction_spread(reference, fractions)
            if spread is not None and spread <= PUPIL_FRACTION_TOLERANCE:
                names.append(item["name"])
                break
        else:
            groups.append((fractions, [item["name"]]))
    return sorted((names for _, names in groups), key=lambda names: (-len(names), names))


def recommend(schemes: list[dict], comparable: dict) -> dict[str, Any]:
    """At most one scheme, from the largest group whose error functions compare; never accepts anything."""
    criteria = ("在误差函数可比的最大一组中，只考虑 AUT 完成、具体约束全部满足、变量界满足、"
                "规格判定不是未通过的方案，按 WAV 加权 RMS 从小到大（缺失时按 ERR. F.）取第一名；只推荐，不接受。")
    groups = comparable["error_function"]
    if not groups:
        return {"scheme": None, "criteria": criteria, "reason": "没有完成的方案。"}
    if len(groups) > 1 and len(groups[0]) == len(groups[1]):
        # No largest group: picking one by name would be arbitrary.
        return {"scheme": None, "criteria": criteria,
                "reason": "误差函数可比的最大一组不唯一（" + "；".join("、".join(names) for names in groups)
                          + "），不推荐；请在组内分别比较。"}
    by_name = {item["name"]: item for item in schemes}
    members = [by_name[name] for name in groups[0]]
    eligible = []
    for item in members:
        metrics = item["metrics"]
        verdict = (item.get("evaluation") or {}).get("status")
        if (metrics["all_constraints_satisfied"] and metrics["explicit_bounds_satisfied"]
                and verdict != "fail"):
            wave = (metrics["wavefront"] or {}).get("weighted_rms_waves")
            eligible.append((wave if wave is not None else float("inf"), metrics["final_error"], item["name"]))
    if not eligible:
        return {"scheme": None, "criteria": criteria, "compared": [item["name"] for item in members],
                "reason": "可比的方案中没有同时满足全部约束、变量界和规格判定的。"}
    eligible.sort()
    best = eligible[0][2]
    recommendation = {"scheme": best, "criteria": criteria, "compared": [item["name"] for item in members],
                      "excluded_from_comparison": [name for names in groups[1:] for name in names],
                      "accept_command": f"python -m codev_mcp.aut accept {by_name[best]['aut']['result']}",
                      "reason": f"{best} 在可比的 {len(members)} 个方案中满足全部约束且 WAV 加权 RMS 最小。"}
    # The choice is unchanged, but a lower RMS on a smaller accepted pupil must not read as a clean win.
    apart = [names for names in comparable.get("pupil", []) if best not in names
             and any(name in names for name in recommendation["compared"])]
    if apart:
        recommendation["pupil_caveat"] = (
            "接受光瞳比不同：" + "；".join("、".join(names) for names in apart)
            + f" 与 {best} 的 WAV 光瞳不同，它们的 RMS 与 {best} 不是在同一块光瞳上比较，请先看接受光瞳比。")
    return recommendation


# ------------------------------------------------------------------ running


def _summary_status(schemes: list[dict]) -> str:
    states = [item["status"] for item in schemes]
    if all(state == "succeeded" for state in states):
        return "succeeded"
    return "partial" if any(state == "succeeded" for state in states) else "failed"


def run_scheme(baseline: Path, scheme: dict, aut_spec: dict, aut_spec_sha: str | None, directory: Path, *,
               design_spec: Path | None, backend: str, timeout: float,
               edit_runner=run_edit, aut_runner=None, evaluator=run_comparison,
               sink: list | None = None, max_analyses_per_session: int = 1) -> dict[str, Any]:
    """One scheme; a failure is recorded in the entry, never raised (except an interrupt).

    The entry is appended to ``sink`` when it is created, so an interrupt still leaves it in the summary.
    """
    aut_runner = aut_runner or aut_module.prepare_spec
    entry: dict[str, Any] = {"name": scheme["name"], "reason": scheme.get("reason"), "status": "running",
                             "directory": str(directory), "started_at": _now()}
    if sink is not None:
        sink.append(entry)
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    try:
        start = directory / "start.len"
        if scheme.get("edits") or scheme.get("field_set"):
            payload = {"schema_version": 1, "kind": "lens_edits", "name": f"scheme-{scheme['name']}",
                       "reason": scheme.get("reason")}
            payload.update({"edits": scheme["edits"]} if scheme.get("edits") else {"field_set": scheme["field_set"]})
            edits_path = directory / "edits.json"
            write_json(edits_path, {key: value for key, value in payload.items() if value is not None})
            bundle, manifest = edit_runner(baseline, edits_path, start, backend=backend, timeout=timeout,
                                           output_dir=directory / "edit")
            entry["edit"] = {"bundle": str(bundle), "status": manifest["status"],
                             "output_sha256": (manifest.get("output") or {}).get("sha256"),
                             "error": manifest.get("error")}
            if manifest.get("interrupted"):
                raise KeyboardInterrupt  # the edit was cut short by Ctrl+C: stop, do not go on to the next scheme
            if manifest["status"] != "succeeded":
                raise ValueError(f"the typed edits failed: {manifest.get('error')}")
        else:
            shutil.copyfile(baseline, start)
            entry["edit"] = None
        entry["start"] = {"path": str(start), "sha256": digest(start)}
        report = aut_runner(start, directory / "aut", copy.deepcopy(aut_spec), spec_sha256=aut_spec_sha)
        entry["aut"] = {"result": str(directory / "aut" / "result.json"), "state": report.get("state"),
                        "error": report.get("error"), "root": str(directory / "aut")}
        if report.get("state") != "complete":
            raise ValueError(f"the AUT did not complete: {report.get('error')}")
        entry["metrics"] = extract_metrics(report)
        entry["candidate"] = {"path": report["candidate_path"], "sha256": report["candidate_sha256"]}
        if design_spec is not None:
            try:
                bundle, evaluated = evaluator(Path(report["candidate_path"]), None, directory / "evaluation",
                                              backend=backend, timeout=timeout, spec_path=design_spec,
                                              max_analyses_per_session=max_analyses_per_session)
                if evaluated.get("interrupted"):
                    raise KeyboardInterrupt
                status = (evaluated.get("evaluation") or {}).get("status") or {}
                entry["evaluation"] = {"bundle": str(bundle), "run_status": evaluated["status"],
                                       "status": next(iter(status.values()), None),
                                       "failed_analyses": evaluated.get("failed_analyses")}
                sessions = (evaluated.get("performance") or {}).get("sessions")
                if sessions is not None and any(s.get("cleanup_confirmed") is not True for s in sessions):
                    entry["cleanup_in_doubt"] = True
            except (ValueError, RuntimeError, OSError) as exc:
                entry["evaluation"] = {"status": None, "error": f"{type(exc).__name__}: {exc}"}
                if CLEANUP_DOUBT.search(entry["evaluation"]["error"]):
                    entry["cleanup_in_doubt"] = True
        entry["status"] = "succeeded"
    except KeyboardInterrupt:
        entry["status"] = "interrupted"
        entry["error"] = "KeyboardInterrupt"
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        if CLEANUP_DOUBT.search(entry["error"]):
            entry["cleanup_in_doubt"] = True
    finally:
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return entry


def run_schemes(baseline: Path, scheme_file: Path, aut_spec_path: Path | None, output_dir: Path, *,
                design_spec: Path | None = None, backend: str = "com", timeout: float = 300.0,
                edit_runner=run_edit, aut_runner=None, evaluator=run_comparison,
                jobs: int = 1, launcher: Callable[[Path], list[str]] | None = None,
                max_analyses_per_session: int = 1) -> tuple[Path, dict]:
    if type(max_analyses_per_session) is not int or not 1 <= max_analyses_per_session <= 8:
        raise ValueError("max_analyses_per_session must be an integer from 1 to 8")
    if type(jobs) is not int or not 1 <= jobs <= MAX_JOBS:
        raise ValueError(f"jobs must be an integer 1..{MAX_JOBS}")
    if jobs > 1 and (aut_runner is not None or edit_runner is not run_edit or evaluator is not run_comparison):
        raise ValueError("replacement runners only work in-process (jobs=1)")
    baseline, scheme_file = baseline.resolve(), scheme_file.resolve()
    if not baseline.is_file() or baseline.suffix.lower() != ".len":
        raise ValueError(f"Expected existing baseline .len: {baseline}")
    payload, scheme_sha = read_scheme_set(scheme_file)
    default_spec = default_sha = None
    if aut_spec_path is not None:
        default_spec, default_sha = read_aut_spec(aut_spec_path.resolve())
    specs: dict[str, tuple[dict, str | None]] = {}
    for scheme in payload["schemes"]:
        if scheme.get("aut_spec"):
            path = (scheme_file.parent / scheme["aut_spec"]).resolve()
            if not path.is_file():
                raise ValueError(f"Scheme {scheme['name']}: AUT spec not found: {path}")
            specs[scheme["name"]] = read_aut_spec(path)
        elif default_spec is not None:
            specs[scheme["name"]] = (default_spec, default_sha)
        else:
            raise ValueError(f"Scheme {scheme['name']} has no AUT spec: give --aut-spec or an aut_spec per scheme")
    for spec, _ in specs.values():
        validate_aut_spec(expand_field_ramp(spec))
    if design_spec is not None:
        design_spec = design_spec.resolve()
        if not design_spec.is_file():
            raise ValueError(f"Design spec not found: {design_spec}")
    output_dir = output_dir.resolve()
    if output_dir.exists() or not str(output_dir).isascii():
        raise ValueError("Output directory must be new and use an ASCII path (CODE V 10.2)")
    output_dir.mkdir(parents=True)
    baseline_hash = digest(baseline)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "kind": "scheme_comparison", "name": payload["name"],
        "created_at": _now(), "source": "codev" if backend == "com" else "simulated", "status": "running",
        "baseline": {"path": str(baseline), "sha256": baseline_hash},
        "scheme_file": {"path": str(scheme_file), "sha256": scheme_sha}, "jobs": jobs,
        "max_analyses_per_session": max_analyses_per_session, "schemes": []}
    write_json(output_dir / "summary.json", manifest)
    stop_reason = None
    try:
        if jobs > 1:
            stop_reason = _run_parallel(baseline, payload["schemes"], specs, output_dir, manifest, jobs,
                                        design_spec=design_spec, backend=backend, timeout=timeout,
                                        launcher=launcher, max_analyses_per_session=max_analyses_per_session)
        else:
            for index, scheme in enumerate(payload["schemes"]):
                spec, spec_sha = specs[scheme["name"]]
                entry = run_scheme(baseline, scheme, spec, spec_sha, output_dir / scheme["name"],
                                   design_spec=design_spec, backend=backend, timeout=timeout,
                                   edit_runner=edit_runner, aut_runner=aut_runner, evaluator=evaluator,
                                   sink=manifest["schemes"], max_analyses_per_session=max_analyses_per_session)
                write_json(output_dir / "summary.json", manifest)
                if entry.get("cleanup_in_doubt"):
                    stop_reason = f"scheme {scheme['name']} left its CODE V processes in doubt; the rest were not run"
                    for later in payload["schemes"][index + 1:]:
                        manifest["schemes"].append({"name": later["name"], "status": "skipped", "error": stop_reason})
                    break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        manifest["baseline"]["unchanged"] = digest(baseline) == baseline_hash
        manifest["status"] = _summary_status(manifest["schemes"]) if manifest["schemes"] else "failed"
        if stop_reason:
            manifest["stop_reason"] = stop_reason
            manifest["status"] = "partial" if any(s["status"] == "succeeded" for s in manifest["schemes"]) else "failed"
        if not manifest["baseline"]["unchanged"]:
            manifest["status"] = "failed"
            manifest["error"] = "The baseline lens changed during the run"
        manifest["comparability"] = comparability(manifest["schemes"])
        manifest["recommendation"] = recommend(manifest["schemes"], manifest["comparability"])
        manifest["finished_at"] = _now()
        write_json(output_dir / "summary.json", manifest)
        (output_dir / "summary.md").write_text(render_summary(manifest), encoding="utf-8", newline="\n")
        write_record(output_dir / "execution-record.json", [execution_step(
            action="scheme_compare", tool="codev_mcp.schemes", status=manifest["status"], source=manifest["source"],
            inputs=[{"role": "baseline", "path": str(baseline), "sha256": baseline_hash},
                    {"role": "scheme_set", "path": str(scheme_file), "sha256": scheme_sha}],
            parameters={"schemes": [item["name"] for item in manifest["schemes"]], "jobs": jobs,
                        "max_analyses_per_session": max_analyses_per_session}, native_commands=[],
            command_note=("Each scheme ran codev_mcp.edit and codev_mcp.aut in its own subdirectory"
                          + (f" ({jobs} at a time, each in its own worker process)" if jobs > 1 else "")
                          + "; their execution records hold the native commands. Nothing was accepted."),
            results={"recommendation": manifest["recommendation"], "comparability": manifest["comparability"],
                     "schemes": [{key: item.get(key) for key in ("name", "status", "metrics", "candidate", "error")}
                                 for item in manifest["schemes"]]},
            outputs=[], bundle=str(output_dir), error=manifest.get("error"))])
    return output_dir, manifest


# ---------------------------------------------------------------- parallel


def _worker_command(request: Path) -> list[str]:
    return [sys.executable, "-m", "codev_mcp.schemes", "_scheme", str(request)]


def _scheme_worker(request_path: Path) -> int:
    """Worker process: run one scheme exactly as the serial loop does and write its entry."""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    sink: list[dict] = []
    code = 0
    try:
        run_scheme(Path(request["baseline"]), request["scheme"], request["aut_spec"], request["aut_spec_sha"],
                   Path(request["directory"]),
                   design_spec=Path(request["design_spec"]) if request["design_spec"] else None,
                   backend=request["backend"], timeout=request["timeout"], sink=sink,
                   max_analyses_per_session=request.get("max_analyses_per_session", 1))
    except KeyboardInterrupt:
        code = 2  # run_scheme has already marked the entry interrupted
    finally:
        if sink:
            write_json(Path(request["result_path"]), sink[0])
    return code


def _collect(name: str, directory: Path, result_path: Path, returncode: int, started: float) -> dict[str, Any]:
    """The entry a finished worker left; a worker that left none is a failure with its processes in doubt."""
    if result_path.is_file():
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"name": name, "status": "failed", "directory": str(directory),
            "error": f"RuntimeError: the scheme worker exited {returncode} without a result; "
                     "its CODE V processes are in doubt (cleanup unconfirmed)",
            "cleanup_in_doubt": True, "elapsed_seconds": round(time.monotonic() - started, 3)}


def _run_parallel(baseline: Path, schemes: list[dict], specs: dict, output_dir: Path, manifest: dict, jobs: int, *,
                  design_spec: Path | None, backend: str, timeout: float,
                  launcher: Callable[[Path], list[str]] | None = None,
                  max_analyses_per_session: int = 1) -> str | None:
    """Run the schemes ``jobs`` at a time; returns the stop reason (None when the set ran through).

    A scheme that leaves its processes in doubt stops new launches; the ones already running finish
    normally and the rest are skipped. An interrupt reaches the workers through the console (each one
    cleans up as the serial run would); a worker that has not ended after the grace period is stopped
    and recorded in doubt.
    """
    launcher = launcher or _worker_command
    requests = output_dir / "_workers"
    requests.mkdir()
    entries: dict[str, dict[str, Any]] = {}
    running: dict[str, dict[str, Any]] = {}
    queue = list(schemes)
    stop_reason: str | None = None
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", "")])

    def publish() -> None:
        manifest["schemes"][:] = [entries[s["name"]] for s in schemes if s["name"] in entries]
        write_json(output_dir / "summary.json", manifest)

    def launch(scheme: dict) -> None:
        name = scheme["name"]
        spec, spec_sha = specs[name]
        directory = output_dir / name
        request = requests / f"{name}.json"
        result = requests / f"{name}-entry.json"
        write_json(request, {"baseline": str(baseline), "scheme": scheme, "aut_spec": spec, "aut_spec_sha": spec_sha,
                             "directory": str(directory), "design_spec": str(design_spec) if design_spec else None,
                             "backend": backend, "timeout": timeout, "result_path": str(result),
                             "max_analyses_per_session": max_analyses_per_session})
        with (requests / f"{name}-stderr.txt").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(launcher(request), env=env, cwd=str(output_dir),
                                       stdout=subprocess.DEVNULL, stderr=stderr)
        running[name] = {"process": process, "directory": directory, "result": result,
                         "started": time.monotonic()}
        entries[name] = {"name": name, "reason": scheme.get("reason"), "status": "running",
                         "directory": str(directory), "started_at": _now()}
        publish()

    def finish(name: str) -> None:
        nonlocal stop_reason
        job = running.pop(name)
        entry = _collect(name, job["directory"], job["result"], job["process"].returncode, job["started"])
        entries[name] = entry
        publish()
        if entry.get("cleanup_in_doubt") and stop_reason is None:
            stop_reason = f"scheme {name} left its CODE V processes in doubt; the rest were not run"

    try:
        while queue or running:
            while queue and len(running) < jobs and stop_reason is None:
                launch(queue.pop(0))
            for name in [n for n, job in running.items() if job["process"].poll() is not None]:
                finish(name)
            if stop_reason is not None:
                for scheme in queue:
                    entries[scheme["name"]] = {"name": scheme["name"], "status": "skipped", "error": stop_reason}
                queue.clear()
                publish()
            if running:
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        deadline = time.monotonic() + INTERRUPT_GRACE_SECONDS
        try:
            while running and time.monotonic() < deadline:
                for name in [n for n, job in running.items() if job["process"].poll() is not None]:
                    finish(name)
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            pass
        for name, job in list(running.items()):
            job["process"].kill()
            job["process"].wait(timeout=10)
            finish(name)
            entries[name].update(status="interrupted", cleanup_in_doubt=True,
                                 error="KeyboardInterrupt: the worker did not end in time; its CODE V "
                                       "processes are in doubt (cleanup unconfirmed)")
        publish()
        return "interrupted"
    return stop_reason


# ---------------------------------------------------------------- rendering


def _num(value, digits: int = 6) -> str:
    if value is None:
        return "缺失"
    if isinstance(value, bool):
        return "是" if value else "否"
    return f"{value:.{digits}g}" if isinstance(value, float) else str(value)


STATUS_TEXT = {"succeeded": "完成", "failed": "失败", "skipped": "未运行", "interrupted": "中断"}
VERDICT_TEXT = {"pass": "通过", "fail": "未通过", "unknown": "未知", None: "—"}


def render_pupil(manifest: dict) -> list[str]:
    """WAV accepted-pupil ratios per scheme, beside the composite RMS with and without the ray-count weighting."""
    rows = []
    for item in manifest["schemes"]:
        wave = (item.get("metrics") or {}).get("wavefront")
        if not wave or not wave.get("pupil_fractions"):
            continue
        rows.append(f"| {item['name']} | " + "、".join(f"{x:.3f}" for x in wave["pupil_fractions"])
                    + f" | {_num(wave['weighted_rms_waves'])} | {_num(wave.get('equal_field_rms_waves'))} |")
    if not rows:
        return []
    return ["", "## WAV 接受光瞳比", "",
            "接受光瞳比是各视场光线数除以视场 1 的光线数：固定的 WAV 光瞳网格中通过光阑面和默认孔径的部分，被挡的光线"
            "不是追迹失败，也不计入 RMS；CODE V 的综合 RMS 按光线数加权，等权列按视场权重直接平均各视场方差。", "",
            "| 方案 | 各视场接受光瞳比 | 综合 RMS（CODE V，按光线数加权） | 综合 RMS（视场权重，不按光线数） |",
            "| --- | --- | ---: | ---: |"] + rows


def render_summary(manifest: dict) -> str:
    lines = [f"# 方案比较：{manifest['name']}", "",
             f"来源：{manifest['source']}；状态：{manifest['status']}。基线镜头 `{manifest['baseline']['path']}`"
             f"（SHA-256 前 12 位 `{manifest['baseline']['sha256'][:12]}`，未改动：{_num(manifest['baseline'].get('unchanged'))}）。",
             "每个方案：类型化修改 → 分阶段 AUT → 候选。**所有候选都未被接受**；接受需要另外运行 `codev_mcp.aut accept`。", ""]
    if manifest.get("stop_reason"):
        lines += [f"> 停止原因：{manifest['stop_reason']}", ""]
    lines += ["## 方案与结果", "",
              "| 方案 | 状态 | 阶段 | ERR. F. 初始→最终 | WAV 加权 RMS（waves）起始→候选 | Strehl 候选 | 约束全部满足 | 变量界 | 规格判定 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in manifest["schemes"]:
        metrics = item.get("metrics")
        if not metrics:
            lines.append(f"| {item['name']} | {STATUS_TEXT.get(item['status'], item['status'])} | — | — | — | — | — | — | — |")
            continue
        wave, start = metrics.get("wavefront"), metrics.get("wavefront_start")
        lines.append(
            f"| {item['name']} | {STATUS_TEXT.get(item['status'], item['status'])} | "
            f"{metrics['stages_succeeded']}/{metrics['stages']} | {_num(metrics['initial_error'])}→{_num(metrics['final_error'])} | "
            f"{_num((start or {}).get('weighted_rms_waves'))}→{_num((wave or {}).get('weighted_rms_waves'))} | "
            f"{_num((wave or {}).get('weighted_strehl'))} | {_num(metrics['all_constraints_satisfied'])} | "
            f"{_num(metrics['explicit_bounds_satisfied'])} | "
            f"{VERDICT_TEXT.get((item.get('evaluation') or {}).get('status'), '—')} |")
    lines += ["", "## 一阶参数（候选；括号内为该方案修改后、AUT 前的起点）", "",
              "| 方案 | EFL | BFL | F/# | OAL | 像距 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for item in manifest["schemes"]:
        metrics = item.get("metrics")
        if not metrics:
            continue
        cells = []
        for key in ("effective_focal_length", "back_focal_length", "f_number", "overall_length", "image_distance"):
            cells.append(f"{_num(metrics['first_order'].get(key))}（{_num(metrics['first_order_start'].get(key))}）")
        lines.append(f"| {item['name']} | " + " | ".join(cells) + " |")
    lines += ["", "## 可比与不可比", ""]
    comparable = manifest["comparability"]
    lines.append("- 一阶参数与规格判定：各方案对同一规格，可直接并列。")
    for label, key in (("误差函数（ERR. F.）", "error_function"), ("WAV 加权 RMS／Strehl", "wavefront")):
        groups = comparable[key]
        if len(groups) <= 1:
            lines.append(f"- {label}：{'全部完成的方案可比' if groups else '无数据'}。")
        else:
            lines.append(f"- {label}：**不可直接比较**，分组 " + "；".join("、".join(names) for names in groups) + "。")
    lines += [""] + [f"> {note}" for note in comparable["notes"]]
    lines += render_pupil(manifest)
    lines += ["", "## 推荐（不自动接受）", ""]
    recommendation = manifest["recommendation"]
    lines.append(f"- 依据：{recommendation['criteria']}")
    lines.append(f"- 结论：{recommendation['reason']}")
    if recommendation.get("pupil_caveat"):
        lines.append(f"- **注意**：{recommendation['pupil_caveat']}")
    if recommendation.get("accept_command"):
        lines.append(f"- 如决定采用，另行运行：`{recommendation['accept_command']}`")
    lines += ["", "## 记录", ""]
    for item in manifest["schemes"]:
        details = [f"{item['name']}：目录 `{item.get('directory', '—')}`"]
        if item.get("edit"):
            details.append(f"类型化修改运行包 `{item['edit']['bundle']}`")
        if item.get("aut"):
            details.append(f"AUT 运行目录 `{item['aut']['root']}`")
        if item.get("error"):
            details.append(f"错误：{item['error']}")
        lines.append("- " + "；".join(details))
    lines += ["", "各方案的设计记录：`python -m codev_mcp.walkthrough --run <类型化修改运行包> --run <AUT 运行目录> --output ...`。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    args_list = sys.argv[1:] if argv is None else list(argv)
    if len(args_list) == 2 and args_list[0] == "_scheme":
        return _scheme_worker(Path(args_list[1]))
    parser = argparse.ArgumentParser(description="从同一基线批量运行多个设计方案（修改 → 分阶段 AUT），汇总并只推荐、不接受")
    parser.add_argument("--lens", type=Path, required=True, help="基线 .len")
    parser.add_argument("--schemes", type=Path, required=True, help="scheme_set JSON")
    parser.add_argument("--aut-spec", type=Path, help="默认 AUT 规格（可含 field_ramp）；方案可用自己的 aut_spec 覆盖")
    parser.add_argument("--design-spec", type=Path, help="可选设计规格：对每个候选做完整评价并给出规格判定")
    parser.add_argument("--max-analyses-per-session", type=int, default=1,
                        help="规格评价的有界会话复用：1～8 项，默认每项独立；可用 8 减少完整评价的启动次数")
    parser.add_argument("--output-dir", type=Path, required=True, help="新的、路径为 ASCII 的输出目录")
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--jobs", type=int, default=1,
                        help=f"同时运行的方案数（1～{MAX_JOBS}，默认 1）；每个方案在自己的工作进程和 CODE V 会话中运行")
    args = parser.parse_args(argv)
    try:
        directory, manifest = run_schemes(args.lens, args.schemes, args.aut_spec, args.output_dir,
                                          design_spec=args.design_spec, backend=args.backend, timeout=args.timeout,
                                          jobs=args.jobs, max_analyses_per_session=args.max_analyses_per_session)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['status']}: {directory}")
    print(manifest["recommendation"]["reason"])
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
