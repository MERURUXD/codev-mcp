"""Export a lens as a readable CODE V sequence file with the native WRL command.

Only two fixed commands reach CODE V: ``RES`` of a service-owned copy and
``WRL`` into the run directory (D5 probe: WRL writes full-precision LDM
commands, including vignetting factors, variable markers and solves). The
sequence is copied to the caller's path, which may be non-ASCII, and is never
executed by the service.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .com_session import ComSession
from .glass import ROOT
from .record import execution_step, write_record
from .safety import command_filespec


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_seq(lens: Path, output: Path, *, run_root: Path | None = None,
               session_factory: Callable[..., ComSession] = ComSession) -> dict:
    lens, output = lens.resolve(), output.resolve()
    if not lens.is_file() or lens.suffix.lower() != ".len":
        raise ValueError(f"Expected existing .len: {lens}")
    if output.suffix.lower() != ".seq" or output.exists() or not output.parent.is_dir():
        raise ValueError(f"Output must be a new .seq in an existing directory: {output}")
    run = (run_root or ROOT / ".codev-run").resolve() / (
        "seq-export-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    if not str(run).isascii():
        raise ValueError("CODE V 10.2 working directory must have an ASCII path")
    (run / "work").mkdir(parents=True, exist_ok=False)
    copy = run / "work" / "input.len"
    lens_hash = digest(lens)
    shutil.copyfile(lens, copy)
    target = run / "export"
    session = session_factory(starting_directory=run / "work")
    result = {"status": "failed", "input": {"path": str(lens), "sha256": lens_hash}}
    commands = [f"RES {command_filespec(copy)}", f"WRL {command_filespec(target)}"]
    try:
        result["codev_version"] = session.start()
        restored = session.command_raw(f"res {command_filespec(copy)}")
        if "has been restored" not in restored:
            raise ValueError("CODE V did not confirm the restore")
        reply = session.command_raw(f"wrl {command_filespec(target)}")
        written = [path for path in run.glob("export.*") if path.suffix.lower() == ".seq"]
        if "Sequence saved" not in reply or len(written) != 1 or written[0].stat().st_size <= 0:
            raise ValueError(f"WRL did not write the sequence file: {reply.strip()[:200]}")
        with output.open("xb") as handle:
            handle.write(written[0].read_bytes())
        result.update(status="succeeded", output={"path": str(output), "sha256": digest(output),
                                                  "bytes": output.stat().st_size})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        problems = [result["error"]] if result.get("error") else []
        if session.stop() is False:
            problems.append("session cleanup unconfirmed")
        if digest(lens) != lens_hash:
            problems.append("input lens changed during export")
        if problems:
            # A written file stays for diagnosis but is not reported as this step's output.
            result.update(status="failed", error="; ".join(problems))
            result.pop("output", None)
        write_record(run / "execution-record.json", [execution_step(
            action="export_seq", tool="codev_mcp.export_seq", status=result["status"], source="codev",
            inputs=[{"role": "lens", "path": str(lens), "sha256": lens_hash}], parameters={},
            native_commands=commands, command_note="RES of a service-owned copy, then WRL; the sequence is not run",
            results={}, outputs=[result["output"]] if result.get("output") else [], bundle=str(run),
            error=result.get("error"))])
    result["run_directory"] = str(run)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 CODE V WRL 把镜头导出为可读 .seq（只读原镜头）")
    parser.add_argument("--lens", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = export_seq(args.lens, args.output)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{result['status']}: {result.get('output', {}).get('path')} ({result['run_directory']})")
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
