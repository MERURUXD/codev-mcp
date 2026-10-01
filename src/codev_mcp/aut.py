"""Isolated, two-step controlled AUT workflow.

``prepare`` never publishes an optimized lens. ``accept`` checks the same
baseline pointer and candidate bytes again before atomically publishing one
revision. The optimization engine lives in a separate child process so a
blocked AUT prompt can be abandoned without touching the committed lens.

Since D6 the request is a typed AUT spec (``aut_spec.py``): several stages,
each with its own variables, whitelisted constraints, general thickness
constraints, error-function parameters and typed weight changes. The original
one-variable command line is converted into a one-stage spec.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .aut_spec import (DEFAULT_RELATIVE_TOLERANCE, GENERAL_CHECKS, SPECIFIC_OPERANDS, aut_commands,
                       expand_field_ramp, judge_constraints, legacy_spec, lens_change_commands, open_commands,
                       parse_aut_listing, parse_variable_table, read_aut_spec, requested_parameters,
                       restore_commands, validate_aut_spec)
from .checkpoints import (VIGNETTING_NAMES, LensCheckpointStore, LensSnapshot, compare_snapshots,
                          hash_file, read_snapshot, utc_now, write_json_atomic)
from .com_backend import ComBackend
from .com_session import _matching_owned, list_codev_processes, shared_server_pids, terminate_processes
from .errors import CodeVError
from .evaluation import evaluate
from .listing import ordinary_surface_rows
from .models import AnalysisKind, AnalysisRequest
from .record import execution_step, write_record
from .safety import command_filespec, validate_filespec

LEGACY_MAX_CYCLES = 8
LEGACY_MAX_WALL_SECONDS = 300
STAGE_SECONDS = 120


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _audit(root: Path, event: str, details: dict[str, Any]) -> None:
    path = root / "audit.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"at": utc_now(), "event": event, **details}, ensure_ascii=False) + "\n")


def _owned_cleanup(directory: Path, *, launched_after: float) -> list[int]:
    """Kill only a candidate session's recorded, matching CODE V processes."""
    marker = directory / "codev-mcp-session.json"
    if not marker.is_file():
        return []
    try:
        record = _json(marker)
        expected = record.get("processes") or {}
        import psutil
        owned = []
        uncertain = []
        live = list_codev_processes()
        shared = shared_server_pids(int(pid_text) for pid_text in expected)  # F7: never stop the shared server
        for pid_text, identity in expected.items():
            pid = int(pid_text)
            if not isinstance(identity, dict) or pid in shared:
                continue  # old records have no creation identity; never kill by name alone
            if live.get(pid) != identity.get("name"):
                continue
            try:
                created = psutil.Process(pid).create_time()
            except psutil.Error:
                uncertain.append(pid)
                continue
            if created < launched_after - 2 or created != identity.get("created_at"):
                continue
            owned.append(pid)
        terminate_processes(owned)
        return sorted(set(uncertain + [int(pid) for pid, identity in expected.items()
                                       if isinstance(identity, dict) and int(pid) not in shared
                                       and _matching_owned(int(pid), identity) is not False]))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"candidate ownership could not be verified: {exc}") from exc


def _validate_request(surface: int, parameter: str, lower: float, upper: float,
                      target: float, cycles: int, wall_seconds: int) -> None:
    """Limits of the original one-variable command line."""
    if surface < 1 or parameter not in {"radius", "thickness"}:
        raise ValueError("choose an existing ordinary surface radius or thickness")
    if not all(math.isfinite(value) for value in (lower, upper, target)):
        raise ValueError("bounds and target must be finite")
    if lower >= upper or target < 0:
        raise ValueError("require lower < upper and a nonnegative error-function target")
    if not 1 <= cycles <= LEGACY_MAX_CYCLES or not 5 <= wall_seconds <= LEGACY_MAX_WALL_SECONDS:
        raise ValueError(f"cycles must be 1..{LEGACY_MAX_CYCLES}; wall seconds must be 5..{LEGACY_MAX_WALL_SECONDS}")


def _numeric(code: str) -> bool:
    return code in {"0", "100"}


def _relations(snapshot: LensSnapshot) -> dict[str, Any]:
    """Native relations that stay in the lens, and the parameters they control.

    A non-numeric CCY/THC code (for example PIM) marks a solve- or
    pickup-controlled parameter: it may move with the variables but can never
    be one. Coupling codes and glass variables are refused.
    """
    if snapshot.zoom_positions != 1:
        raise ValueError("AUT supports single-zoom lenses only")
    controls = snapshot.variable_controls
    if len(controls) != (snapshot.surface_count or 0):
        raise ValueError("native variable-control columns are incomplete")
    controlled = []
    for number, row in controls.items():
        if any(key not in row for key in ("CCY", "THC", "GLC")):
            raise ValueError(f"variable controls at surface {number} are incomplete")
        if row["GLC"] not in {"", "100"}:
            raise ValueError("glass variables are not supported")
        for code in ("CCY", "THC"):
            value = row[code]
            if value.isdigit() and not _numeric(value):
                raise ValueError(f"coupled variable code {code}={value} at surface {number} is not supported")
            if not value.isdigit():
                controlled.append({"surface": int(number), "code": code, "value": value,
                                   "parameter": "radius" if code == "CCY" else "thickness"})
    return {"solves": list(snapshot.solves), "pickups": list(snapshot.pickups), "controlled": controlled}


def _spec_preflight(snapshot: LensSnapshot, lens: Any, spec: dict) -> dict[str, Any]:
    """Refuse lenses the typed stages cannot drive; list the relations that stay."""
    relations = _relations(snapshot)
    count = snapshot.surface_count or 0
    blocked = {(item["surface"], item["parameter"]) for item in relations["controlled"]}
    surfaces = {surface.number: surface for surface in snapshot.zooms[0].surfaces}
    fields = {item.number for item in snapshot.zooms[0].fields}
    wavelengths = {item.number for item in snapshot.wavelengths}
    for stage in spec["stages"]:
        for variable in stage["variables"]:
            number, parameter = variable["surface"], variable["parameter"]
            if not 1 <= number <= count - 2:
                raise ValueError(f"surface {number} is not an ordinary surface")
            if (number, parameter) in blocked:
                raise ValueError(f"{parameter} of surface {number} is controlled by a solve or pickup")
            surface = surfaces[number]
            if parameter == "thickness" and surface.thickness_infinite:
                raise ValueError(f"thickness of surface {number} is infinite and cannot be a variable")
            if parameter == "radius" and surface.radius_infinite and (
                    variable.get("lower") is not None or variable.get("upper") is not None):
                # AUT varies the curvature (CUY); a flat start has curvature 0, so a radius bound has no
                # meaning there (the sign of the radius changes when the curvature crosses zero).
                raise ValueError(f"radius of flat surface {number} cannot have radius bounds; "
                                 "leave lower and upper out")
        for item in stage.get("constraints", []):
            qualifier = SPECIFIC_OPERANDS[item["operand"]]
            if qualifier == "field" and item["field"] not in fields:
                raise ValueError(f"DIY field {item['field']} does not exist")
            if qualifier == "surface" and not 1 <= item["surface"] <= count - 2:
                raise ValueError(f"{item['operand']} surface {item['surface']} is not an ordinary surface")
            if qualifier == "surfaces" and "surfaces" in item and item["surfaces"][1] > count - 2:
                raise ValueError("OAL range exceeds the last lens surface")
        for change in stage.get("lens_changes", []):
            if change["target"] == "field" and change["field"] not in fields:
                raise ValueError(f"field {change['field']} does not exist")
            if change["target"] == "wavelength" and change["wavelength"] not in wavelengths:
                raise ValueError(f"wavelength {change['wavelength']} does not exist")
        if stage.get("set_vignetting") and not any(
                aperture.kind == "clear" for surface in lens.surfaces for aperture in surface.apertures):
            raise ValueError("SET VIG needs explicit clear apertures; without them CODE V assumes a stop "
                             "aperture and replaces the designed vignetting")
    return relations


def start_bounds(snapshot: LensSnapshot, stage: dict) -> list[dict[str, Any]]:
    """Variables that start on or outside a bound.

    CODE V accepts them without an error and moves the variable to the bound
    (E5 probe, ``local validation records``), so this is
    a note in the stage record, not a refusal.
    """
    surfaces = {surface.number: surface for surface in snapshot.zooms[0].surfaces}
    notes = []
    for variable in stage["variables"]:
        value = getattr(surfaces[variable["surface"]], variable["parameter"], None)
        if value is None:
            continue
        for side, bound in (("lower", variable.get("lower")), ("upper", variable.get("upper"))):
            if bound is None:
                continue
            position = ("outside" if (value < bound if side == "lower" else value > bound)
                        else "on" if value == bound else None)
            if position:
                notes.append({"surface": variable["surface"], "parameter": variable["parameter"],
                              "value": value, "bound": side, "limit": bound, "position": position})
    return notes


def _allowed_changes(stage: dict, relations: dict, snapshot: LensSnapshot) -> list[tuple[str, int, int, str]]:
    """Parameters one stage may move: its variables, solve-controlled values and typed edits."""
    allowed = [("surface", v["surface"], 1, v["parameter"]) for v in stage["variables"]]
    # A radius variable may leave (or reach) the flat state: the curvature crosses zero.
    allowed += [("surface", v["surface"], 1, "radius_infinite") for v in stage["variables"]
                if v["parameter"] == "radius"]
    allowed += [("surface", item["surface"], 1, item["parameter"]) for item in relations["controlled"]]
    for change in stage.get("lens_changes", []):
        allowed.append(("field", change["field"], 1, change["parameter"]) if change["target"] == "field"
                       else ("wavelength", change["wavelength"], 0, "weight"))
    if stage.get("set_vignetting"):
        allowed += [("field", item.number, 1, name) for item in snapshot.zooms[0].fields
                    for name in VIGNETTING_NAMES]
    return allowed


def _require_ordinary_surfaces(session: Any, surface_count: int) -> None:
    """Reject native surface sections with unmodelled shape or position data."""
    listing = session.command("lis")
    if session.output_is_truncated(listing):
        raise ValueError("the native surface listing was truncated")
    try:
        ordinary_surface_rows(listing, surface_count)
    except ValueError as exc:
        if str(exc).startswith("special surface"):
            raise ValueError("AUT does not support special surface data in the native listing") from None
        raise


def _analysis(backend: ComBackend, kind: AnalysisKind) -> dict[str, Any]:
    task = backend.run_analysis(AnalysisRequest(kind=kind))
    snapshot = backend.get_analysis()
    payload = snapshot.first_order if kind is AnalysisKind.FIRST_ORDER else snapshot.wavefront
    if task.state.value != "succeeded" or payload is None:
        raise ValueError(f"{kind.value} evaluation did not complete")
    return payload.model_dump(mode="json")


def _diagnostic_wavefront(lens_path: Path, directory: Path) -> dict[str, Any]:
    """Run WAV in a disposable session; its failure cannot taint AUT state."""
    backend = ComBackend(working_directory=directory)
    result: dict[str, Any]
    try:
        backend.open_lens(str(lens_path))
        task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        snapshot = backend.get_analysis()
        if task.state.value == "succeeded" and snapshot.wavefront is not None:
            result = {"state": "succeeded", "result": snapshot.wavefront.model_dump(mode="json")}
        else:
            result = {"state": "failed", "error": task.error.model_dump(mode="json") if task.error else None}
    except Exception as exc:
        result = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            status = backend.close_session()
            if ((status.details or {}).get("cleanup_confirmed") is not True):
                result = {"state": "failed", "cleanup_unconfirmed": True,
                          "primary_error": result.get("error"),
                          "error": f"diagnostic cleanup: {status.details}"}
        except Exception as exc:
            result = {"state": "failed", "primary_error": result.get("error"),
                      "cleanup_unconfirmed": True,
                      "error": f"diagnostic cleanup: {type(exc).__name__}: {exc}"}
    return result


def _stage_operation(config: dict[str, Any]) -> dict[str, Any]:
    """All COM work for one bounded stage runs on this child process thread."""
    root = Path(config["root"])
    phase = config["phase"]
    if phase == "baseline":
        backend = ComBackend(working_directory=root / "baseline-engine")
        primary_error = None
        try:
            lens = backend.open_lens(config["source"])
            checkpoint = backend._current_checkpoint()
            _require_ordinary_surfaces(backend._require_session(),
                                       checkpoint.snapshot.surface_count or 0)
            relations = _spec_preflight(checkpoint.snapshot, lens, config["spec"])
            return {"baseline_path": str(checkpoint.lens_path),
                    "baseline_sha256": checkpoint.lens_sha256,
                    "baseline_snapshot": checkpoint.snapshot.to_dict(),
                    "checkpoint_directory": str(checkpoint.lens_path.parent),
                    "backend_id": checkpoint.backend_id, "lens_id": checkpoint.lens_id,
                    "revision": checkpoint.revision, "relations": relations}
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                status = backend.close_session()
                if (status.details or {}).get("cleanup_confirmed") is not True:
                    raise RuntimeError(f"baseline cleanup unconfirmed: {status.details}")
            except BaseException as exc:
                raise RuntimeError(f"baseline primary: {primary_error}; cleanup: {exc}") from exc
    if phase == "diagnostic":
        return _diagnostic_wavefront(Path(config["lens_path"]), root / config["directory"])
    if phase == "verify":
        verifier = ComBackend(working_directory=root / config["directory"])
        primary_error = None
        try:
            verifier.open_lens(config["lens_path"])
            actual = verifier._current_checkpoint().snapshot
            _require_ordinary_surfaces(verifier._require_session(), actual.surface_count or 0)
            if compare_snapshots(LensSnapshot.from_dict(config["expected"]), actual):
                raise ValueError("accepted lens did not reopen to its recorded state")
            return {"verified": True}
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                status = verifier.close_session()
                if (status.details or {}).get("cleanup_confirmed") is not True:
                    raise RuntimeError(f"accept verification cleanup unconfirmed: {status.details}")
            except BaseException as exc:
                raise RuntimeError(f"accept primary: {primary_error}; cleanup: {exc}") from exc
    raise ValueError(f"Unknown AUT stage: {phase}")


def _stage_child(config_path: Path) -> int:
    config = _json(config_path)
    output = Path(config["result_path"])
    try:
        result = {"state": "complete", "value": _stage_operation(config)}
    except Exception as exc:
        result = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
    write_json_atomic(output, result)
    return 0 if result["state"] == "complete" else 1


def _run_stage(root: Path, phase: str, payload: dict[str, Any], *,
               seconds: int = STAGE_SECONDS, label: str = "") -> dict[str, Any]:
    label = label or phase
    config_path = root / f"{label}-request.json"
    result_path = root / f"{label}-result.json"
    config = {**payload, "root": str(root), "phase": phase,
              "result_path": str(result_path)}
    write_json_atomic(config_path, config)
    launched_after = time.time()
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), child_env.get("PYTHONPATH", "")])
    with (root / f"{label}-stderr.txt").open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen([sys.executable, "-m", "codev_mcp.aut", "_stage",
                                  str(config_path)], cwd=str(root), env=child_env,
                                 stdout=subprocess.DEVNULL, stderr=stderr)
    try:
        child.wait(timeout=seconds)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        child.kill()
        child.wait(timeout=10)
        remaining = _owned_cleanup(root / ("baseline-engine" if phase == "baseline" else
                                  payload["directory"]), launched_after=launched_after)
        raise RuntimeError(f"AUT {label} interrupted or timed out; cleanup remaining: {remaining}") from exc
    result = _json(result_path) if result_path.is_file() else {
        "state": "failed", "error": f"stage exited {child.returncode} without a result"}
    if result["state"] != "complete" or child.returncode != 0:
        remaining = _owned_cleanup(root / ("baseline-engine" if phase == "baseline" else
                                   payload["directory"]), launched_after=launched_after)
        raise RuntimeError(f"AUT {label} failed: {result.get('error')}; "
                           f"cleanup remaining: {remaining}")
    if phase == "diagnostic" and result["value"].get("cleanup_unconfirmed"):
        result["value"]["cleanup_remaining"] = _owned_cleanup(
            root / payload["directory"], launched_after=launched_after)
    return result["value"]


def _value(snapshot: LensSnapshot, number: int, parameter: str):
    surface = next(item for item in snapshot.zooms[0].surfaces if item.number == number)
    return getattr(surface, parameter)


def _is_flat(snapshot: LensSnapshot, number: int, parameter: str) -> bool:
    """True when the parameter reads as infinity (a plane radius); ``_value`` is None then."""
    surface = next(item for item in snapshot.zooms[0].surfaces if item.number == number)
    return bool(getattr(surface, f"{parameter}_infinite"))


def _vignetting(snapshot: LensSnapshot) -> list[dict[str, Any]]:
    return [{"field": item.number, **{name: getattr(item, name) for name in VIGNETTING_NAMES}}
            for item in snapshot.zooms[0].fields]


def _general_checks(stage: dict, lens: Any) -> list[dict[str, Any]]:
    """CODE V prints no values for general constraints; check the candidate instead."""
    requirements, limits = [], {}
    for key, value in stage.get("general_constraints", {}).items():
        metric, side = GENERAL_CHECKS[key]
        # Same default tolerance as the specific constraints: CODE V meets a bound only to its own
        # numerical tolerance (a thickness driven to 0.1 reads back as 0.09999999999999896).
        tolerance = DEFAULT_RELATIVE_TOLERANCE * max(1.0, abs(value))
        limits[key] = (value, tolerance)
        requirements.append({"id": key, "metric": metric, "unit": lens.units.value, "field": None,
                             "direction": None, "frequency": None,
                             "minimum": value - tolerance if side == "minimum" else None,
                             "maximum": value + tolerance if side == "maximum" else None, "required": True})
    if not requirements:
        return []
    items = evaluate(requirements, {}, lens.model_dump(mode="json"), "codev")["requirements"]
    for item in items:
        item["limit"], item["tolerance"] = limits[item["id"]]
        item["note"] = ("service check over every element, within the default constraint tolerance; CODE V "
                        "applies the general constraint to variable-thickness elements and defines edges by "
                        "reference rays")
    return items


def _find_listing(base: Path) -> Path | None:
    for candidate in base.parent.glob(base.name + ".*"):
        if candidate.suffix.lower() == ".lis":
            return candidate
    return None


def _run_stages(backend: ComBackend, session: Any, root: Path, engine_dir: Path, config: dict[str, Any],
                baseline: LensSnapshot, relations: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Run every stage in order; stop at the first failure and keep earlier candidates."""
    spec = config["spec"]
    count = baseline.surface_count or 0
    current = baseline
    records: list[dict[str, Any]] = []
    for index, stage in enumerate(spec["stages"], 1):
        record: dict[str, Any] = {"index": index, "name": stage["name"], "status": "running",
                                  "native_commands": []}
        records.append(record)
        notes = start_bounds(current, stage)
        if notes:
            record["start_bounds"] = notes
        if "ramp_step" in stage:
            record["ramp_step"] = stage["ramp_step"]

        def send(command: str) -> str:
            record["native_commands"].append(command)
            return session.command(command)

        try:
            for command in lens_change_commands(stage) + open_commands(stage):
                send(command)
            for command in ("aut", "err cdv", "mxc 0", "vli y"):
                send(command)
            dry = send("go")
            if session.output_is_truncated(dry):
                raise ValueError("zero-cycle AUT output was truncated")
            listed = parse_variable_table(dry)
            if listed != requested_parameters(stage):
                raise ValueError(f"AUT variable list {sorted(listed)} differs from the request "
                                 f"{sorted(requested_parameters(stage))}")
            for command in aut_commands(stage, count, int(spec.get("wall_seconds", 600))):
                send(command)
            output_base = engine_dir / f"aut-stage-{index}"
            send(f"out {command_filespec(output_base)}")
            record["native_commands"].append("go")
            state["pending"] = True
            session.async_command("go")
            while session.is_executing_command():
                session.wait(1)
            terminal = session.get_command_output()  # before any other command
            send("out t")
            state["pending"] = False
            listing_path = _find_listing(output_base)
            if listing_path is None:
                raise ValueError("the redirected AUT output file was not written")
            copy = root / f"stage-{index}-aut-output.lis"
            shutil.copyfile(listing_path, copy)
            record["output"] = {"path": copy.name, "sha256": hash_file(copy), "bytes": copy.stat().st_size,
                                "terminal_chars": len(terminal)}
            parsed = parse_aut_listing(copy.read_text(encoding="utf-8", errors="replace"))
            for command in restore_commands(baseline.variable_controls, stage):
                send(command)
            if stage.get("set_vignetting"):
                record["set_vignetting_reply"] = send("set vig").strip()
            backend._lens = None
            lens = backend._read_lens()
            after = read_snapshot(session, lens, backend._listing)
            _require_ordinary_surfaces(session, after.surface_count or 0)
            differences = compare_snapshots(current, after,
                                            allowed_changes=_allowed_changes(stage, relations, current))
            if differences:
                raise ValueError(f"AUT changed state outside the stage's parameters: {differences[:3]}")
            variables = []
            for item in stage["variables"]:
                before_value = _value(current, item["surface"], item["parameter"])
                after_value = _value(after, item["surface"], item["parameter"])
                unbounded = item.get("lower") is None and item.get("upper") is None
                inside = (unbounded if after_value is None else (
                    (item.get("lower") is None or after_value >= item["lower"])
                    and (item.get("upper") is None or after_value <= item["upper"])))
                variables.append({**item, "before": before_value, "after": after_value,
                                  "before_infinite": _is_flat(current, item["surface"], item["parameter"]),
                                  "after_infinite": _is_flat(after, item["surface"], item["parameter"]),
                                  "within_bounds": inside})
            coupled = [{**item, "before": _value(current, item["surface"], item["parameter"]),
                        "after": _value(after, item["surface"], item["parameter"])}
                       for item in relations["controlled"]
                       if _value(current, item["surface"], item["parameter"]) != _value(after, item["surface"], item["parameter"])]
            constraints = judge_constraints(stage, parsed, count)
            general = _general_checks(stage, lens)
            record.update({
                "completion": parsed["completion"], "initial_error": parsed["initial_error"],
                "final_error": parsed["final_error"], "final_cycle": parsed["final_cycle"],
                "target": stage["error_function"].get("TAR", 0.0),
                "target_reached": parsed["final_error"] <= stage["error_function"].get("TAR", 0.0),
                "variables": variables, "solve_coupled": coupled, "constraints": constraints,
                "general_constraints": {"settings": stage.get("general_constraints", {}),
                                        "active_in_final_table": parsed["active_general"],
                                        "frozen_violations": parsed["frozen_violations"],
                                        "service_checks": general},
                "lens_changes": stage.get("lens_changes", []),
                "constraints_satisfied": all(item["status"] == "satisfied" for item in constraints),
                "bounds_satisfied": all(item["within_bounds"] for item in variables)})
            if stage.get("set_vignetting"):
                record["vignetting"] = {"before": _vignetting(current), "after": _vignetting(after)}
            candidate = root / f"candidate-stage-{index}.len"
            send(f"sav {command_filespec(candidate)}")
            if not candidate.is_file() or candidate.stat().st_size == 0:
                raise ValueError("CODE V did not write a nonempty stage candidate")
            backend._load_lens_file(session, candidate)
            restored = read_snapshot(session, backend._read_lens(), backend._listing)
            if compare_snapshots(after, restored):
                raise ValueError("saved stage candidate did not reopen to the verified state")
            record["candidate"] = {"path": str(candidate), "sha256": hash_file(candidate),
                                   "snapshot": restored.to_dict()}
            record["status"] = "succeeded"
            current = restored
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            break
    return records


def _spec_child(config_path: Path) -> int:
    config = _json(config_path)
    root = Path(config["root"])
    engine_dir = root / "candidate-engine"
    launched_after = time.time()
    backend = ComBackend(working_directory=engine_dir)
    state = {"pending": False}
    result: dict[str, Any] = {"state": "failed", "result_source": "codev"}
    try:
        backend.open_lens(str(Path(config["baseline_path"])))
        session = backend._require_session()
        before = backend._current_checkpoint().snapshot
        _require_ordinary_surfaces(session, before.surface_count or 0)
        if compare_snapshots(LensSnapshot.from_dict(config["baseline_snapshot"]), before):
            raise ValueError("candidate session did not restore the committed baseline")
        relations = _spec_preflight(before, backend._read_lens(), config["spec"])
        before_analysis = _analysis(backend, AnalysisKind.FIRST_ORDER)
        if before_analysis.get("effective_focal_length") is None:
            raise ValueError("AUT requires a focal lens with a finite effective focal length")
        stages = _run_stages(backend, session, root, engine_dir, config, before, relations, state)
        result["stages"] = stages
        good = [stage for stage in stages if stage["status"] == "succeeded"]
        if good:
            result["last_good_stage"] = good[-1]["index"]
        if len(good) != len(config["spec"]["stages"]):
            failed = next(stage for stage in stages if stage["status"] != "succeeded")
            raise ValueError(f"stage {failed['index']} ({failed['name']}) failed: {failed.get('error')}")
        final = good[-1]
        after_analysis = _analysis(backend, AnalysisKind.FIRST_ORDER)
        if after_analysis.get("effective_focal_length") is None:
            raise ValueError("AUT produced a lens without a finite effective focal length")
        verified = read_snapshot(session, backend._read_lens(), backend._listing)
        if compare_snapshots(LensSnapshot.from_dict(final["candidate"]["snapshot"]), verified):
            raise ValueError("analysis changed candidate lens state")
        result.update({
            "state": "complete", "candidate_path": final["candidate"]["path"],
            "candidate_sha256": final["candidate"]["sha256"],
            "candidate_snapshot": final["candidate"]["snapshot"], "relations": relations,
            "before_first_order": before_analysis, "after_first_order": after_analysis,
            "stop_reason": final["completion"], "final_error": final["final_error"],
            "initial_error": stages[0]["initial_error"], "target_reached": final["target_reached"],
            "all_constraints_satisfied": all(stage["constraints_satisfied"] for stage in good),
            "final_constraints_satisfied": final["constraints_satisfied"],
            "explicit_bounds_satisfied": all(stage["bounds_satisfied"] for stage in good),
            "constraint_scope": ("specific constraints from the final AUT tables of each stage; general "
                                 "thickness constraints by service checks on each stage candidate")})
    except Exception as exc:  # candidate failures never publish anything
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if state["pending"]:
            # An AUT that did not hand back its prompt: no more COM calls.
            result["discarded_unterminated_session"] = True
            result["cleanup_remaining"] = _owned_cleanup(engine_dir, launched_after=launched_after)
        else:
            try:
                status = backend.close_session()
                if (status.details or {}).get("cleanup_confirmed") is not True:
                    result["cleanup_error"] = f"candidate cleanup unconfirmed: {status.details}"
                    result["state"] = "failed"
            except Exception as exc:
                result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                result["state"] = "failed"
        write_json_atomic(root / "child-result.json", result)
    return 0 if result["state"] == "complete" else 1


def prepare(source: Path, root: Path, *, surface: int, parameter: str,
            lower: float, upper: float, target: float, cycles: int,
            wall_seconds: int) -> dict[str, Any]:
    """The original one-variable entry point, converted into a one-stage spec."""
    _validate_request(surface, parameter, lower, upper, target, cycles, wall_seconds)
    spec = legacy_spec(surface, parameter, lower, upper, target, cycles, wall_seconds)
    return prepare_spec(source, root, spec, spec_sha256=None)


def _record_prepare(root: Path, report: dict[str, Any]) -> None:
    commands = []
    for stage in report.get("stages", []):
        commands.append(f"! stage {stage['index']} ({stage['name']}): {stage['status']}")
        commands += stage.get("native_commands", [])
    write_record(root / "execution-record.json", [execution_step(
        action="aut", tool="codev_mcp.aut prepare", status=report["state"], source="codev",
        inputs=[{"role": "lens", "path": report["source"], "sha256": report["source_sha256"]},
                {"role": "aut_spec", "sha256": report.get("spec_sha256")}],
        parameters={"spec": report["spec"]},
        native_commands=commands,
        command_note=("Sent in the isolated candidate session after RES of the committed baseline; "
                      "'out <file>' only redirects the listing and can be dropped when replaying"),
        results={key: report.get(key) for key in (
            "stages", "before_first_order", "after_first_order", "final_error", "initial_error",
            "target_reached", "all_constraints_satisfied", "final_constraints_satisfied",
            "explicit_bounds_satisfied", "relations", "before_wavefront", "after_wavefront",
            "last_good_stage", "error")},
        outputs=[{"role": "candidate", "path": report["candidate_path"], "sha256": report["candidate_sha256"]}]
        if report.get("candidate_path") else [],
        bundle=str(root), error=report.get("error"))])


def prepare_spec(source: Path, root: Path, spec: dict[str, Any], *,
                 spec_sha256: str | None) -> dict[str, Any]:
    spec_input = spec
    spec = expand_field_ramp(spec)
    validate_aut_spec(spec)
    source = validate_filespec(str(source), must_exist=True)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source_digest = hash_file(source)
    wall_seconds = int(spec.get("wall_seconds", 600))
    baseline = {"root": str(root), "source": str(source), "source_sha256": source_digest,
                "spec": spec, "spec_sha256": spec_sha256, "wall_seconds": wall_seconds,
                **({"spec_input": spec_input} if spec_input is not spec else {}),
                "stage_timeout_seconds": STAGE_SECONDS,
                "prepare_budget_seconds": wall_seconds + 3 * STAGE_SECONDS}
    try:
        baseline.update(_run_stage(root, "baseline", baseline))
    except Exception as exc:
        failed = {**baseline, "state": "failed", "phase": "baseline", "error": str(exc)}
        write_json_atomic(root / "result.json", failed)
        _record_prepare(root, failed)
        raise
    write_json_atomic(root / "request.json", baseline)
    launched_after = time.time()
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), child_env.get("PYTHONPATH", "")]
    )
    with (root / "child-stderr.txt").open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen([sys.executable, "-m", "codev_mcp.aut", "_child",
                                  str(root / "request.json")], cwd=str(root), env=child_env,
                                 stdout=subprocess.DEVNULL, stderr=stderr)
    try:
        child.wait(timeout=wall_seconds)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        child.kill()
        child.wait(timeout=10)
        remaining = _owned_cleanup(root / "candidate-engine", launched_after=launched_after)
        failure = {"state": "failed", "error": type(exc).__name__,
                   "cleanup_remaining": remaining}
        write_json_atomic(root / "result.json", {**baseline, **failure})
        _audit(root, "candidate_discarded", failure)
        _record_prepare(root, {**baseline, **failure})
        raise RuntimeError(f"AUT candidate was discarded: {type(exc).__name__}") from exc
    child_result = _json(root / "child-result.json") if (root / "child-result.json").is_file() else {
        "state": "failed", "error": f"candidate worker exited {child.returncode} without a result"}
    if child_result["state"] != "complete":
        child_result["cleanup_remaining"] = _owned_cleanup(
            root / "candidate-engine", launched_after=launched_after)
    if hash_file(source) != source_digest:
        child_result = {**child_result, "state": "failed", "error": "source lens changed during AUT"}
    if child_result["state"] == "complete":
        for key, lens_path, directory in (
                ("before_wavefront", baseline["baseline_path"], "before-wavefront-engine"),
                ("after_wavefront", child_result["candidate_path"], "after-wavefront-engine")):
            try:
                child_result[key] = _run_stage(root, "diagnostic",
                                               {"lens_path": lens_path, "directory": directory},
                                               label=key)
            except Exception as exc:
                child_result[key] = {"state": "failed", "stage_error": True,
                                     "error": str(exc)}
            if child_result[key].get("cleanup_unconfirmed") or child_result[key].get("stage_error"):
                child_result["state"] = "failed"
                child_result["error"] = f"{key} cleanup or deadline failure: {child_result[key].get('error')}"
                break
        if hash_file(source) != source_digest:
            child_result = {**child_result, "state": "failed", "error": "source lens changed during diagnostics"}
    # Child output is diagnostic data, never an authority over baseline identity.
    report = {**child_result, **baseline, "accepted": False}
    write_json_atomic(root / "result.json", report)
    _audit(root, "candidate_prepared" if report["state"] == "complete" else "candidate_failed",
           {"state": report["state"], "error": report.get("error")})
    _record_prepare(root, report)
    return report


def accept(result_path: Path) -> dict[str, Any]:
    result_path = result_path.resolve()
    root = result_path.parent
    report = _json(result_path)
    if report.get("state") != "complete" or report.get("accepted"):
        raise ValueError("only an unaccepted complete candidate can be published")
    validate_aut_spec(report["spec"])
    candidate = Path(report["candidate_path"]).resolve()
    if candidate.parent != root or hash_file(candidate) != report["candidate_sha256"]:
        raise ValueError("candidate file is outside its run directory or changed")
    if hash_file(Path(report["source"])) != report["source_sha256"]:
        raise ValueError("source lens changed after candidate preparation")
    directory = Path(report["checkpoint_directory"]).resolve()
    if directory.parent.parent != root / "baseline-engine" / "checkpoints":
        raise ValueError("checkpoint directory is outside this run")
    store = LensCheckpointStore(directory.parent.parent, backend_id=report["backend_id"])
    def current():
        checkpoint = store.load_current(directory)
        if (checkpoint.lens_id != report["lens_id"] or
                checkpoint.revision != report["revision"] or
                checkpoint.lens_sha256 != report["baseline_sha256"]):
            raise ValueError("the committed baseline changed; candidate is stale")
        return checkpoint
    baseline_checkpoint = current()
    expected = LensSnapshot.from_dict(report["candidate_snapshot"])
    relations = _relations(baseline_checkpoint.snapshot)
    allowed = []
    for stage in report["spec"]["stages"]:
        allowed += _allowed_changes(stage, relations, baseline_checkpoint.snapshot)
    if compare_snapshots(baseline_checkpoint.snapshot, expected, allowed_changes=allowed):
        raise ValueError("candidate changed state outside the requested stages")
    _run_stage(root, "verify", {"lens_path": str(candidate),
                                "directory": "accept-engine", "expected": expected.to_dict()},
               label="accept-verify")
    current()
    revision = int(report["revision"]) + 1
    destination = directory / f"revision-{revision:06d}.len"
    if destination.exists():
        if hash_file(destination) != report["candidate_sha256"]:
            raise ValueError("the unpublished revision path contains different lens bytes")
    else:
        with candidate.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    if (hash_file(candidate) != report["candidate_sha256"] or
            hash_file(destination) != report["candidate_sha256"]):
        raise ValueError("candidate or copied revision changed during acceptance")
    _run_stage(root, "verify", {"lens_path": str(destination),
                                "directory": "accept-copy-engine", "expected": expected.to_dict()},
               label="accept-copy-verify")
    # Deny concurrent writes/deletes while the verified bytes become current.
    import win32con
    import win32file
    try:
        revision_handle = win32file.CreateFile(
            str(destination), win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
            None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
    except Exception as exc:
        raise ValueError(f"the revision file could not be locked for publication: {exc}") from exc
    try:
        if hash_file(destination) != report["candidate_sha256"]:
            raise ValueError("copied revision changed after readback")
        try:
            current()
            published = store.publish(directory, report["lens_id"], revision, destination,
                                      expected, source_path=report["source"],
                                      expected_sha256=report["candidate_sha256"])
        except Exception as exc:
            try:
                pointer = store.load_current(directory)
            except Exception:
                pointer = None
            if (pointer is not None and pointer.revision == revision
                    and pointer.lens_sha256 == hash_file(destination)):
                published = pointer  # the atomic pointer already committed
            else:
                _audit(root, "accept_failed", {"revision": revision,
                                                "pointer_unchanged": pointer is not None and pointer.revision == report["revision"],
                                                "error": f"{type(exc).__name__}: {exc}"})
                raise
    finally:
        revision_handle.Close()
    report["accepted"] = True
    report["accepted_at"] = utc_now()
    report["accepted_revision"] = revision
    report["accepted_sha256"] = published.lens_sha256
    report["accepted_path"] = str(destination)
    try:
        write_json_atomic(result_path, report)
        _audit(root, "candidate_accepted", {"revision": revision, "sha256": published.lens_sha256})
        record_path = root / "execution-record.json"
        steps = _json(record_path)["steps"] if record_path.is_file() else []
        steps.append(execution_step(
            action="aut_accept", tool="codev_mcp.aut accept", status="succeeded", source="codev",
            inputs=[{"role": "candidate", "path": str(candidate), "sha256": report["candidate_sha256"]}],
            parameters={"baseline_revision": report["revision"]}, native_commands=[],
            command_note="No CODE V command: the verified candidate bytes become the next revision",
            results={"accepted_revision": revision},
            outputs=[{"role": "accepted", "path": str(destination), "sha256": published.lens_sha256}],
            bundle=str(root)))
        write_record(record_path, steps)
    except OSError:
        # current.json is already the commit boundary; diagnostics cannot undo it.
        pass
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlled, isolated CODE V AUT")
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--lens", type=Path, required=True)
    prep.add_argument("--output-dir", type=Path, required=True)
    prep.add_argument("--spec", type=Path, help="typed multi-variable, multi-stage AUT spec JSON")
    prep.add_argument("--surface", type=int)
    prep.add_argument("--parameter", choices=("radius", "thickness"))
    prep.add_argument("--lower", type=float)
    prep.add_argument("--upper", type=float)
    prep.add_argument("--target", type=float)
    prep.add_argument("--cycles", type=int, default=2)
    prep.add_argument("--wall-seconds", type=int, default=120)
    use = sub.add_parser("accept")
    use.add_argument("result", type=Path)
    internal = sub.add_parser("_child", help=argparse.SUPPRESS)
    internal.add_argument("request", type=Path)
    stage = sub.add_parser("_stage", help=argparse.SUPPRESS)
    stage.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            legacy = [args.surface, args.parameter, args.lower, args.upper, args.target]
            if args.spec is not None:
                if any(value is not None for value in legacy):
                    parser.error("--spec replaces --surface/--parameter/--lower/--upper/--target")
                spec, digest = read_aut_spec(args.spec)
                result = prepare_spec(args.lens, args.output_dir, spec, spec_sha256=digest)
            else:
                if any(value is None for value in legacy):
                    parser.error("give --spec, or all of --surface --parameter --lower --upper --target")
                result = prepare(args.lens, args.output_dir, surface=args.surface,
                                 parameter=args.parameter, lower=args.lower, upper=args.upper,
                                 target=args.target, cycles=args.cycles,
                                 wall_seconds=args.wall_seconds)
            print(args.output_dir.resolve() / "result.json")
            return 0 if result["state"] == "complete" else 1
        if args.action == "accept":
            print(accept(args.result)["accepted_revision"])
            return 0
        return _spec_child(args.request) if args.action == "_child" else _stage_child(args.request)
    except (ValueError, RuntimeError, CodeVError, OSError) as exc:
        print(f"AUT {args.action} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
