"""Stand-in for ``python -m codev_mcp.schemes _scheme`` in the parallel scheduling tests.

The scheme's ``reason`` says what to do: ``sleep:<seconds>`` (optionally followed by ``,fail``, ``,doubt``,
``,crash``, ``,hang`` or ``,barrier:a+b`` to wait for named workers to start).
Start and end lines go to a separate event file for each worker, so a
test can see how many workers overlapped. No CODE V is involved.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from codev_mcp.schemes import extract_metrics

from publication_fixtures import aut_report


def log(request: dict, text: str) -> None:
    # Each file has one writer: concurrent append to a shared file can lose
    # events on Windows and make an otherwise correct schedule look wrong.
    path = Path(request["result_path"]).with_name(f"{request['scheme']['name']}-events.txt")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.monotonic_ns()} {text}\n")


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = request["scheme"]["name"]
    actions = (request["scheme"].get("reason") or "sleep:0").split(",")
    log(request, f"start {name}")
    for action in actions:
        if action.startswith("barrier:"):
            ready = Path(request["result_path"]).parent
            (ready / f"{name}-started").write_text("ready", encoding="utf-8")
            peers = action.split(":", 1)[1].split("+")
            deadline = time.monotonic() + 30
            while not all((ready / f"{peer}-started").is_file() for peer in peers):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Fake worker start barrier timed out")
                time.sleep(0.01)
    time.sleep(float(actions[0].split(":")[1]))
    if "hang" in actions:
        time.sleep(120)
    if "crash" in actions:
        return 3
    entry = {"name": name, "status": "succeeded", "directory": request["directory"]}
    if "fail" in actions or "doubt" in actions:
        entry.update(status="failed", error="RuntimeError: AUT candidate was discarded: TimeoutExpired")
    if "doubt" in actions:
        entry.update(error="RuntimeError: AUT baseline failed: x; cleanup remaining: [4242]", cleanup_in_doubt=True)
    if entry["status"] == "succeeded":
        report = aut_report()
        entry.update(metrics=extract_metrics(report), candidate={"path": "x", "sha256": "y"},
                     aut={"result": "r", "root": "d"})
    Path(request["result_path"]).write_text(json.dumps(entry), encoding="utf-8")
    log(request, f"end {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
