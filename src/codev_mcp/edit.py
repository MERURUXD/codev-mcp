"""Apply a file of typed edits to a lens copy and save the result (glass swaps, W2).

The edits are the public ``ParameterEdit`` records, sent as one ``update_lens``
transaction with its readback and rollback guarantees. Instead of edits a file
may carry one ``field_set`` (the public ``FieldSetReplacement``), which replaces
the field set as its own transaction. The result is saved
through ``save_lens_as``, reopened in a separate session and checked again;
the input file is never written. Each run leaves an execution record with the
equivalent native commands for the design walkthrough.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from .compare import ROOT, assert_equivalent, digest, optical_state, provenance, write_json
from .fieldset import field_set_commands, resolve_field_set
from .models import FieldSetReplacement, LensData, LensField, ParameterEdit
from .record import execution_step, write_record
from .safety import format_float
from .scale import _first_order, _now, _session
from .stdio_client import StdioClient

FIELD_ITEM = {"y_angle": "YAN", "x_angle": "XAN", "weight": "WTF",
              "vux": "VUX", "vlx": "VLX", "vuy": "VUY", "vly": "VLY"}


def read_edits(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Edit file is not UTF-8 JSON: {exc}") from None
    if (not isinstance(payload, dict)
            or set(payload) - {"schema_version", "kind", "name", "reason", "edits", "field_set"}
            or payload.get("schema_version") != 1 or payload.get("kind") != "lens_edits"):
        raise ValueError("Edit file needs schema_version 1, kind lens_edits, name, optional reason and "
                         "edits or field_set")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise ValueError("Edit file needs a name")
    if ("field_set" in payload) == ("edits" in payload):
        raise ValueError("Edit file needs either edits or a field_set, not both and not neither")
    if "field_set" in payload:
        try:
            payload["field_set"] = FieldSetReplacement.model_validate(payload["field_set"]).model_dump(
                mode="json", exclude_none=True)
        except ValueError as exc:
            raise ValueError(f"field_set is not valid: {exc}") from None
        payload["edits"] = []
        return payload, hashlib.sha256(raw).hexdigest()
    if not isinstance(payload.get("edits"), list) or not 1 <= len(payload["edits"]) <= 100:
        raise ValueError("Edit file needs 1 to 100 edits")
    payload["edits"] = [ParameterEdit.model_validate(item).model_dump(mode="json", exclude_none=True)
                        for item in payload["edits"]]
    return payload, hashlib.sha256(raw).hexdigest()


def native_command(edit: dict, lens: dict) -> str:
    """The command update_lens sends for one typed edit (same formatting)."""
    target, parameter, value = edit["target"], edit["parameter"], edit["value"]
    if target == "aperture":
        return f"{lens['aperture']['kind'].upper()} {format_float(float(value))}"
    if target == "surface":
        if parameter == "glass":
            return f"GLA S{edit['surface']} {value}"
        if parameter == "clear_aperture_radius":
            surface = next(s for s in lens["surfaces"] if s["number"] == edit["surface"])
            label = (surface.get("apertures") or [{}])[0].get("label")
            label_text = f" L'{label}'" if label else ""
            return f"CIR S{edit['surface']} CLR{label_text} {format_float(float(value))}"
        item = {"radius": "RDY", "thickness": "THI"}[parameter]
        return f"{item} S{edit['surface']} {format_float(float(value))}"
    if target == "field":
        return f"{FIELD_ITEM[parameter]} F{edit['field']} {format_float(float(value))}"
    if parameter == "is_reference":
        return f"REF {int(float(value))}"
    if parameter == "weight":
        return f"WTW W{edit['wavelength']} {int(float(value))}"
    return f"WL W{edit['wavelength']} {format_float(float(value) * 1000)}"


def run_edit(source: Path, edits_path: Path, output: Path, *, backend: str = "com", timeout: float = 300.0,
             output_dir: Path | None = None, client_factory=StdioClient) -> tuple[Path, dict]:
    payload, edits_hash = read_edits(edits_path)
    source, output = source.resolve(), output.resolve()
    if not source.is_file() or source.suffix.lower() != ".len":
        raise ValueError(f"Expected existing .len: {source}")
    if output.suffix.lower() != ".len" or output.exists() or not output.parent.is_dir() or output == source:
        raise ValueError(f"Output must be a new .len in an existing directory: {output}")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    bundle = (output_dir or ROOT / ".codev-run" / "edits").resolve() / run_id
    bundle.mkdir(parents=True, exist_ok=False)
    work_root = ROOT / ".codev-run" / ("e-" + uuid.uuid4().hex[:8])
    if not str(work_root).isascii():
        raise ValueError("Repository work directory must have an ASCII path for CODE V 10.2")
    work_root.mkdir(parents=True, exist_ok=False)
    source_hash = digest(source)
    shutil.copyfile(source, bundle / "input.len")
    shutil.copyfile(edits_path, bundle / "edits.json")
    manifest = {"schema_version": 1, "kind": "edit", "run_id": run_id, "created_at": _now(), "status": "running",
                "source": "codev" if backend == "com" else "simulated",
                "input": {"path": str(source), "sha256": source_hash},
                "edits_file": {"path": str(edits_path.resolve()), "sha256": edits_hash, "name": payload["name"],
                               "reason": payload.get("reason")}}
    write_json(bundle / "manifest.json", manifest)
    started = time.monotonic()
    commands: list[str] = []
    try:
        work = work_root / "edit"
        work.mkdir()
        working = work / ("edited-source.len" if backend == "com" else source.name)
        shutil.copyfile(source, working)

        def edit(client):
            opened, _ = client.call("open_lens", {"path": str(working)})
            LensData.model_validate(opened)
            if opened["zoom_positions"] != 1:
                raise ValueError("Only single-zoom lenses: the recorded commands carry no zoom qualifiers")
            write_json(bundle / "lens-before.json", opened)
            before = _first_order(client, opened, bundle / "first-order-before", manifest["source"], timeout)
            if payload.get("field_set"):
                current = [LensField.model_validate(item) for item in opened["fields"]]
                target = resolve_field_set(FieldSetReplacement.model_validate(payload["field_set"]), current)
                commands.extend(field_set_commands(target, current))
                request = {"field_set": payload["field_set"]}
            else:
                commands.extend(native_command(item, opened) for item in payload["edits"])
                request = {"edits": payload["edits"]}
            status_before, _ = client.call("get_status")
            update, _ = client.call("update_lens", {"request": request})
            write_json(bundle / "update.json", update)
            applied = (update["field_set"]["applied"] if payload.get("field_set")
                       else all(outcome.get("applied") for outcome in update.get("outcomes", []))
                       and len(update.get("outcomes", [])) == len(payload["edits"]))
            if update.get("rolled_back") or not update.get("session_valid") or not applied:
                raise ValueError("The edit transaction was rejected or rolled back: "
                                 + "; ".join(update.get("warnings") or []))
            after, _ = client.call("get_lens")
            write_json(bundle / "lens-after.json", after)
            result = _first_order(client, after, bundle / "first-order-after", manifest["source"], timeout)
            status_after, _ = client.call("get_status")
            manifest["transaction"] = {
                "outcomes": update["outcomes"], "field_set": update.get("field_set"),
                "warnings": update.get("warnings", []),
                "revision_before": provenance(status_before)["revision"],
                "revision_after": provenance(status_after)["revision"]}
            manifest["first_order"] = {key: {"before": before.get(key), "after": result.get(key)}
                                       for key in ("effective_focal_length", "back_focal_length", "f_number",
                                                   "overall_length", "image_distance")}
            saved_path = work / "edited.len"
            saved, _ = client.call("save_lens_as", {"path": str(saved_path)})
            if saved.get("overwritten") or not saved_path.is_file() or saved_path.stat().st_size <= 0:
                raise ValueError("Edited lens save was not confirmed")
            return after

        after = _session(work, bundle / "edit-session", backend, timeout, client_factory, edit)
        edited = bundle / "edited.len"
        shutil.copyfile(work_root / "edit" / "edited.len", edited)
        edited_hash = digest(edited)
        reopen_work = work_root / "reopen"
        reopen_work.mkdir()
        reopened_path = reopen_work / ("edited.len" if backend == "com" else source.name)
        shutil.copyfile(edited, reopened_path)

        def verify(client):
            reopened, _ = client.call("open_lens", {"path": str(reopened_path)})
            write_json(bundle / "reopen" / "lens.json", reopened)
            if backend == "com":
                assert_equivalent(optical_state(after), optical_state(reopened), "reopened")
            manifest["reopen"] = {"state_equivalent": backend == "com"}

        _session(reopen_work, bundle / "reopen", backend, timeout, client_factory, verify)
        with output.open("xb") as handle:
            handle.write(edited.read_bytes())
        if digest(output) != edited_hash:
            raise ValueError("Output copy does not match the verified edited lens")
        manifest["output"] = {"path": str(output), "sha256": edited_hash, "bundle_copy": "edited.len"}
        manifest["status"] = "succeeded"
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, KeyboardInterrupt):
            manifest["interrupted"] = True  # callers that loop over runs must stop (F8)
    finally:
        try:
            manifest["input"]["unchanged"] = digest(source) == source_hash
        except OSError:
            manifest["input"]["unchanged"] = False
        if not manifest["input"]["unchanged"]:
            manifest["status"] = "failed"
            manifest["error"] = "Input changed or could not be verified during editing"
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        manifest["finished_at"] = _now()
        write_json(bundle / "manifest.json", manifest)
        write_record(bundle / "execution-record.json", [execution_step(
            action="edit", tool="codev_mcp.edit", status=manifest["status"], source=manifest["source"],
            inputs=[{"role": "lens", "path": str(source), "sha256": source_hash},
                    {"role": "edits", "path": str(edits_path.resolve()), "sha256": edits_hash}],
            parameters={"name": payload["name"], "reason": payload.get("reason"), "edits": payload["edits"],
                        **({"field_set": payload["field_set"]} if payload.get("field_set") else {})},
            native_commands=commands,
            command_note="Sent by update_lens as one typed transaction after RES of the input; solves re-derive",
            results={"transaction": manifest.get("transaction"), "first_order": manifest.get("first_order")},
            outputs=[manifest["output"]] if manifest.get("output") else [],
            bundle=str(bundle), error=manifest.get("error"))])
    return bundle, manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按类型化编辑文件修改镜头副本并另存（例如换玻璃）")
    parser.add_argument("--lens", type=Path, required=True)
    parser.add_argument("--edits", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codev-run" / "edits")
    args = parser.parse_args(argv)
    try:
        bundle, manifest = run_edit(args.lens, args.edits, args.output, backend=args.backend,
                                    timeout=args.timeout, output_dir=args.output_dir)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['status']}: {bundle}")
    if manifest.get("error"):
        print(manifest["error"], file=sys.stderr)
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
