"""Uniform scaling of a lens to a target focal length through the public MCP tools (D4).

One ``update_lens`` transaction multiplies every finite radius, every thickness
that is not controlled by a solve, the object distance of a finite conjugate,
an EPD pupil value and a single circular clear aperture by the same factor.
The PIM image distance is left to CODE V and reported as a solve-coupled
change. The result is saved through ``save_lens_as``, reopened in a separate
session and checked again; the input file is never written.

The native ``SCA EFL SPC`` command was probed against this path; the typed transaction is kept because it
reuses the existing rollback and readback guarantees and records every
command it sent.
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .checkpoints import parse_variable_controls
from .compare import (ROOT, assert_equivalent, digest, optical_state, poll_result, provenance,
                      validate_result, write_json)
from .listing import ordinary_surface_rows, parse_listing
from .models import LensData, ParameterEdit
from .record import execution_step, write_record
from .safety import format_float
from .scan import _confirmed_cleanup
from .stdio_client import StdioClient

#: Uniform scaling multiplies the focal length exactly; the edits are sent with
#: twelve significant digits, so anything beyond this is a real discrepancy.
EFL_RELATIVE_TOLERANCE = 1e-9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def controlled_thicknesses(lens: dict) -> dict[int, str]:
    """Thicknesses controlled by a native relation, after checking only PIM exists.

    A lens with any other solve or with a pickup is refused: mapping those rows
    to parameters has not been verified, and scaling around them could leave a
    relation that no longer describes the design.
    """
    text = lens.get("raw_listing")
    if not text:
        raise ValueError("Native LIS listing is missing, so solves and pickups cannot be excluded")
    listing = parse_listing(text)
    if not listing.relation_data_complete:
        raise ValueError("The native solve/pickup section is incomplete")
    if listing.pickups or any(solve.upper() != "PIM" for solve in listing.solves):
        raise ValueError("Scaling supports lenses whose only native relation is a PIM solve; found "
                         f"solves={listing.solves} pickups={listing.pickups}")
    controls = parse_variable_controls(text, len(lens["surfaces"]))
    if len(controls) != len(lens["surfaces"]):
        raise ValueError("The native CCY/THC/GLC columns could not be read")
    controlled = {int(n): row["THC"] for n, row in controls.items()
                  if row.get("THC") and not row["THC"].lstrip("-").isdigit()}
    if set(controlled.values()) - {"PIM"} or len(controlled) != len(listing.solves):
        raise ValueError(f"Thickness controls {controlled} do not match the native solves {listing.solves}")
    return controlled


def plan_scaling(lens: dict, factor: float) -> tuple[list[dict], list[dict]]:
    """Typed edits for a uniform scale, and the parameters deliberately left alone.

    Raises ValueError for any lens the uniform typed scale cannot describe:
    several zoom positions, special surfaces, non-angle fields, a curved image
    surface, complex apertures or relations other than PIM.
    """
    if not _finite(factor) or factor <= 0:
        raise ValueError("scale factor must be finite and positive")
    if lens["zoom_positions"] != 1:
        raise ValueError("Scaling supports single-zoom lenses only")
    controlled = controlled_thicknesses(lens)
    ordinary_surface_rows(lens["raw_listing"], len(lens["surfaces"]))
    specification = parse_listing(lens["raw_listing"]).specification
    if specification.field_kind not in {None, "angle"} or not specification.field_angles_y:
        raise ValueError("Scaling supports angle fields only; object or image heights would need scaling too")
    if lens["aperture"]["kind"] not in {"epd", "fno", "na", "nao"}:
        raise ValueError("The system aperture type is unknown")
    edits: list[dict] = []
    left: list[dict] = []
    for surface in lens["surfaces"]:
        number, role = surface["number"], surface["role"]
        if role == "image":
            if not surface["radius_is_infinite"]:
                raise ValueError("A curved image surface cannot be scaled through update_lens")
            continue
        if role == "surface" and not surface["radius_is_infinite"]:
            edits.append({"target": "surface", "surface": number, "parameter": "radius",
                          "value": surface["radius"] * factor})
        if not surface["aperture_data_complete"]:
            raise ValueError(f"Surface {number} has incomplete aperture data")
        apertures = surface["apertures"]
        if apertures:
            item = apertures[0]
            if (len(apertures) != 1 or item["kind"] != "clear" or item["shape"] != "circular"
                    or item["x_decenter"] or item["y_decenter"] or item["rotation_degrees"]
                    or item["or_with_previous"] or not _finite(item["radius"])):
                raise ValueError(f"Surface {number} has an aperture other than one centred clear circle")
            edits.append({"target": "surface", "surface": number, "parameter": "clear_aperture_radius",
                          "value": item["radius"] * factor})
        if number in controlled:
            left.append({"surface": number, "parameter": "thickness", "control": controlled[number],
                         "before": surface["thickness"], "reason": "solve-controlled; CODE V re-derives it"})
        elif surface["thickness_is_infinite"]:
            left.append({"surface": number, "parameter": "thickness", "control": "infinite",
                         "before": None, "reason": "infinite object distance stays infinite"})
        elif surface["thickness"]:
            edits.append({"target": "surface", "surface": number, "parameter": "thickness",
                          "value": surface["thickness"] * factor})
    if lens["aperture"]["kind"] == "epd":
        edits.append({"target": "aperture", "parameter": "value", "value": lens["aperture"]["value"] * factor})
    else:
        left.append({"target": "aperture", "parameter": lens["aperture"]["kind"],
                     "before": lens["aperture"]["value"], "reason": "dimensionless pupil value is scale invariant"})
    left.append({"target": "field", "parameter": "angles, weights and vignetting factors",
                 "reason": "angles and pupil fractions are scale invariant"})
    left.append({"target": "wavelength", "parameter": "all",
                 "reason": "wavelengths are not scaled; diffraction performance changes with size"})
    return edits, left


def native_command(edit: dict, lens: dict) -> str:
    """The command update_lens sends for one typed edit (same formatting)."""
    value = format_float(edit["value"])
    if edit["target"] == "aperture":
        return f"{lens['aperture']['kind'].upper()} {value}"
    if edit["parameter"] == "clear_aperture_radius":
        surface = next(s for s in lens["surfaces"] if s["number"] == edit["surface"])
        label = surface["apertures"][0].get("label")
        label_text = f" L'{label}'" if label else ""
        return f"CIR S{edit['surface']} CLR{label_text} {value}"
    item = {"radius": "RDY", "thickness": "THI"}[edit["parameter"]]
    return f"{item} S{edit['surface']} {value}"


def _readback(lens: dict, edit: dict):
    if edit["target"] == "aperture":
        return lens["aperture"]["value"]
    surface = next(s for s in lens["surfaces"] if s["number"] == edit["surface"])
    if edit["parameter"] == "clear_aperture_radius":
        return surface["apertures"][0]["radius"]
    return surface[edit["parameter"]]


def _first_order(client, lens: dict, destination: Path, source: str, timeout: float) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    request = {"kind": "first_order", "options": {
        "zoom_position": 1, "field_numbers": [f["number"] for f in lens["fields"]],
        "wavelength_numbers": [w["number"] for w in lens["wavelengths"]]}}
    write_json(destination / "request.json", request)
    snapshot, _images, task_id = poll_result(client, request, timeout, destination)
    return validate_result(snapshot, task_id, request, source, lens)


def _session(work: Path, log_dir: Path, backend: str, timeout: float, client_factory, action):
    log_dir.mkdir(parents=True, exist_ok=True)
    client = client_factory(work, backend, timeout, log_dir)
    primary = None
    try:
        return action(client)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            cleanup = client.close()
        except Exception as exc:  # noqa: BLE001 - recorded, and it fails the run
            cleanup = {"close_error": f"{type(exc).__name__}: {exc}"}
        write_json(log_dir / "cleanup.json", cleanup)
        if not _confirmed_cleanup(cleanup) and primary is None:
            raise RuntimeError(f"Session cleanup was not confirmed: {cleanup}")


def run_scale(source: Path, output: Path, *, target_efl: float | None = None, factor: float | None = None,
              backend: str = "com", timeout: float = 300.0, output_dir: Path | None = None,
              client_factory=StdioClient) -> tuple[Path, dict]:
    if (target_efl is None) == (factor is None):
        raise ValueError("Give exactly one of target EFL or scale factor")
    for value in (target_efl, factor):
        if value is not None and (not _finite(value) or value <= 0):
            raise ValueError("target EFL and factor must be finite and positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if backend not in {"com", "simulated"}:
        raise ValueError("backend must be com or simulated")
    source = source.resolve()
    output = output.resolve()
    if not source.is_file() or source.suffix.lower() != ".len":
        raise ValueError(f"Expected existing .len: {source}")
    if output.suffix.lower() != ".len" or output.exists() or not output.parent.is_dir() or output == source:
        raise ValueError(f"Output must be a new .len in an existing directory: {output}")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    bundle = (output_dir or ROOT / ".codev-run" / "scales").resolve() / run_id
    bundle.mkdir(parents=True, exist_ok=False)
    work_root = ROOT / ".codev-run" / ("k-" + uuid.uuid4().hex[:8])
    if not str(work_root).isascii():
        raise ValueError("Repository work directory must have an ASCII path for CODE V 10.2")
    work_root.mkdir(parents=True, exist_ok=False)
    source_hash = digest(source)
    frozen = bundle / "input.len"
    shutil.copyfile(source, frozen)
    source_name = "scaled-source.len" if backend == "com" or not source.name.isascii() else source.name
    manifest = {"schema_version": 1, "kind": "scale", "run_id": run_id, "created_at": _now(),
                "status": "running", "source": "codev" if backend == "com" else "simulated",
                "input": {"path": str(source), "sha256": source_hash, "snapshot": "input.len"},
                "request": {"target_efl": target_efl, "factor": factor},
                "tolerance": {"efl_relative": EFL_RELATIVE_TOLERANCE}, "timeout_seconds": timeout}
    write_json(bundle / "manifest.json", manifest)
    started = time.monotonic()
    try:
        if digest(frozen) != source_hash:
            raise ValueError("Input changed while copying")
        work = work_root / "edit"
        work.mkdir()
        working = work / source_name
        shutil.copyfile(frozen, working)

        def edit(client):
            opened, _ = client.call("open_lens", {"path": str(working)})
            LensData.model_validate(opened)
            if opened.get("source") != manifest["source"]:
                raise ValueError("Lens source mismatch")
            write_json(bundle / "lens-before.json", opened)
            before = _first_order(client, opened, bundle / "first-order-before", manifest["source"], timeout)
            efl = before["effective_focal_length"]
            if not _finite(efl) or efl <= 0:
                raise ValueError("Scaling to a focal length needs a finite positive EFL")
            scale = factor if factor is not None else target_efl / efl
            target = efl * scale
            edits, left = plan_scaling(opened, scale)
            commands = [native_command(item, opened) for item in edits]
            manifest["plan"] = {"factor": scale, "efl_before": efl, "efl_target": target,
                                "edits": edits, "left_to_codev": left, "native_commands": commands}
            write_json(bundle / "manifest.json", manifest)
            requests = [ParameterEdit.model_validate(item).model_dump(mode="json") for item in edits]
            status_before, _ = client.call("get_status")
            update, _ = client.call("update_lens", {"request": {"edits": requests}})
            write_json(bundle / "update.json", update)
            if (update.get("source") != manifest["source"] or update.get("rolled_back")
                    or not update.get("session_valid") or len(update.get("outcomes", [])) != len(edits)
                    or not all(outcome.get("applied") for outcome in update["outcomes"])):
                raise ValueError("The scaling transaction was rejected or rolled back: "
                                 + "; ".join(update.get("warnings") or []))
            after, _ = client.call("get_lens")
            write_json(bundle / "lens-after.json", after)
            for item in edits:
                if not math.isclose(_readback(after, item), float(format_float(item["value"])),
                                    rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError(f"Read back differs for {native_command(item, opened)}")
            unchanged = optical_state(opened)
            changed = optical_state(after)
            for key in ("units", "fields", "wavelengths", "stop_surface", "zoom_positions"):
                assert_equivalent(unchanged[key], changed[key], f"scaled.{key}")
            assert_equivalent([s["glass"] for s in unchanged["surfaces"]],
                              [s["glass"] for s in changed["surfaces"]], "scaled.glass")
            coupled = []
            for item in left:
                if item.get("control") == "PIM":
                    value = next(s["thickness"] for s in after["surfaces"] if s["number"] == item["surface"])
                    coupled.append({**item, "after": value})
            result = _first_order(client, after, bundle / "first-order-after", manifest["source"], timeout)
            status_after, _ = client.call("get_status")
            manifest["transaction"] = {"warnings": update.get("warnings", []),
                                       "restore_point": update.get("restore_point"),
                                       "revision_before": provenance(status_before)["revision"],
                                       "revision_after": provenance(status_after)["revision"],
                                       "solve_coupled": coupled}
            deviation = result["effective_focal_length"] / target - 1
            manifest["first_order"] = {
                key: {"before": before.get(key), "after": result.get(key)}
                for key in ("effective_focal_length", "back_focal_length", "f_number", "overall_length",
                            "image_distance", "entrance_pupil_diameter")}
            manifest["efl_check"] = {"target": target, "after": result["effective_focal_length"],
                                     "relative_deviation": deviation,
                                     "within_tolerance": abs(deviation) <= EFL_RELATIVE_TOLERANCE,
                                     "precision_note": result.get("precision_note")}
            if not manifest["efl_check"]["within_tolerance"]:
                raise ValueError(f"Scaled EFL deviates from the target by {deviation:.3g} (relative)")
            for item in coupled:
                if not math.isclose(item["after"], result["image_distance"], rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError("The PIM image distance does not match the paraxial image distance")
            saved_path = work / "scaled.len"
            saved, _ = client.call("save_lens_as", {"path": str(saved_path)})
            if saved.get("overwritten") or not saved_path.is_file() or saved_path.stat().st_size <= 0:
                raise ValueError("Scaled lens save was not confirmed")
            return after

        after = _session(work, bundle / "edit-session", backend, timeout, client_factory, edit)
        scaled = bundle / "scaled.len"
        shutil.copyfile(work / "scaled.len", scaled)
        scaled_hash = digest(scaled)
        reopen_work = work_root / "reopen"
        reopen_work.mkdir()
        reopened_path = reopen_work / ("scaled.len" if backend == "com" else source_name)
        shutil.copyfile(scaled, reopened_path)

        def verify(client):
            reopened, _ = client.call("open_lens", {"path": str(reopened_path)})
            write_json(bundle / "reopen" / "lens.json", reopened)
            if backend == "com":
                assert_equivalent(optical_state(after), optical_state(reopened), "reopened")
            result = _first_order(client, reopened, bundle / "reopen" / "first-order", manifest["source"], timeout)
            deviation = result["effective_focal_length"] / manifest["efl_check"]["target"] - 1
            manifest["reopen"] = {"efl": result["effective_focal_length"], "relative_deviation": deviation,
                                  "state_equivalent": backend == "com"}
            if backend == "com" and abs(deviation) > EFL_RELATIVE_TOLERANCE:
                raise ValueError("The reopened scaled lens does not reproduce the target EFL")

        _session(reopen_work, bundle / "reopen", backend, timeout, client_factory, verify)
        if digest(scaled) != scaled_hash:
            raise ValueError("Scaled lens changed during verification")
        with output.open("xb") as handle:
            handle.write(scaled.read_bytes())
        if digest(output) != scaled_hash:
            raise ValueError("Output copy does not match the verified scaled lens")
        manifest["output"] = {"path": str(output), "sha256": scaled_hash, "bundle_copy": "scaled.len"}
        manifest["status"] = "succeeded"
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, KeyboardInterrupt):
            manifest["interrupted"] = True
    finally:
        try:
            manifest["input"]["unchanged"] = digest(source) == source_hash == digest(frozen)
        except OSError:
            manifest["input"]["unchanged"] = False
        if not manifest["input"]["unchanged"]:
            manifest["status"] = "failed"
            manifest["error"] = "Input changed or could not be verified during scaling"
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        manifest["finished_at"] = _now()
        write_json(bundle / "manifest.json", manifest)
        plan = manifest.get("plan") or {}
        write_record(bundle / "execution-record.json", [execution_step(
            action="scale", tool="codev_mcp.scale", status=manifest["status"], source=manifest["source"],
            inputs=[{"role": "lens", "path": str(source), "sha256": source_hash}],
            parameters={"target_efl": target_efl, "factor": plan.get("factor", factor)},
            native_commands=plan.get("native_commands", []),
            command_note=("Sent by update_lens as one typed transaction after RES of the input; "
                          "PIM image distance re-derived by CODE V"),
            results={"efl_check": manifest.get("efl_check"), "first_order": manifest.get("first_order"),
                     "solve_coupled": (manifest.get("transaction") or {}).get("solve_coupled"),
                     "left_to_codev": plan.get("left_to_codev")},
            outputs=[manifest["output"]] if manifest.get("output") else [],
            bundle=str(bundle), error=manifest.get("error"))])
        _write_report(bundle, manifest)
    return bundle, manifest


def _write_report(bundle: Path, manifest: dict) -> None:
    lines = ["# 起点缩放", "", f"状态：{manifest['status']}；source={manifest['source']}", ""]
    if manifest.get("error"):
        lines += [f"失败原因：{manifest['error']}", ""]
    plan = manifest.get("plan")
    if plan:
        lines += [f"缩放系数 {plan['factor']:.12g}：EFL {plan['efl_before']:.12g} → 目标 {plan['efl_target']:.12g}。", "",
                  "一次 `update_lens` 事务发送的等效命令（数值按 12 位有效数字）：", "", "```"]
        lines += plan["native_commands"] + ["```", "", "交给 CODE V 或保持不变的项：", ""]
        lines += [f"- {item.get('surface', item.get('target'))} {item['parameter']}：{item['reason']}"
                  for item in plan["left_to_codev"]]
    for item in (manifest.get("transaction") or {}).get("solve_coupled", []):
        lines.append(f"- 求解联动：第 {item['surface']} 面 {item['control']} 像距 {item['before']:.9g} → {item['after']:.9g}")
    if manifest.get("first_order"):
        lines += ["", "| 一阶参数 | 缩放前 | 缩放后 |", "| --- | ---: | ---: |"]
        for key, pair in manifest["first_order"].items():
            if _finite(pair["before"]) and _finite(pair["after"]):
                lines.append(f"| {key} | {pair['before']:.12g} | {pair['after']:.12g} |")
    check = manifest.get("efl_check")
    if check:
        lines += ["", f"EFL 相对偏差 {check['relative_deviation']:.3g}（容差 {EFL_RELATIVE_TOLERANCE:g}）。"]
    if manifest.get("reopen"):
        lines.append(f"另存后独立会话重开：EFL {manifest['reopen']['efl']:.12g}。")
    if manifest.get("output"):
        lines.append(f"输出：`{manifest['output']['path']}`，SHA-256 `{manifest['output']['sha256'][:12]}`。")
    lines += ["", "执行记录见 [execution-record.json](execution-record.json)。"]
    (bundle / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按目标焦距或系数整体缩放镜头，另存为新文件")
    parser.add_argument("--lens", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="新的 .len 路径；已存在则拒绝")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--efl", type=float, help="目标有效焦距（镜头长度单位）")
    group.add_argument("--factor", type=float, help="缩放系数")
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codev-run" / "scales")
    args = parser.parse_args(argv)
    try:
        bundle, manifest = run_scale(args.lens, args.output, target_efl=args.efl, factor=args.factor,
                                     backend=args.backend, timeout=args.timeout, output_dir=args.output_dir)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['status']}: {bundle}")
    if manifest.get("error"):
        print(manifest["error"], file=sys.stderr)
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
