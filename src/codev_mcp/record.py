"""Structured execution records for later design walkthroughs (D9 input).

Every workflow that writes a lens leaves one ``execution-record.json`` next to
its bundle. A record lists steps in the order they ran, each with its inputs
and outputs (paths and SHA-256), the typed parameters, the equivalent native
commands a user can replay on the CODE V command line, and the recorded
results. The walkthrough generator only renders these records; it never
recomputes or estimates a number.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RECORD_SCHEMA_VERSION = 1


def execution_step(*, action: str, tool: str, status: str, source: str, inputs: list[dict],
                   parameters: dict, native_commands: list[str], command_note: str,
                   results: dict, outputs: list[dict], bundle: str, error: str | None = None) -> dict:
    """One replayable step; ``source`` keeps simulated runs visibly apart."""
    return {"action": action, "tool": tool, "status": status, "source": source,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "inputs": inputs,
            "parameters": parameters, "native_commands": list(native_commands),
            "command_note": command_note, "results": results, "outputs": outputs,
            "bundle": bundle, "error": error}


def write_record(path: Path, steps: list[dict]) -> None:
    payload = {"schema_version": RECORD_SCHEMA_VERSION, "kind": "execution_record", "steps": steps}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
