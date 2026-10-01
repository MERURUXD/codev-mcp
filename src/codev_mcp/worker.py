"""Worker process that owns the CODE V session.

The MCP server never talks to CODE V directly. It spawns this worker, which
holds the backend object and executes every request serially on one thread,
which is what the CODE V COM interface requires.

Wire protocol: one JSON object per line on stdin, one JSON object per line on
stdout. Nothing else may be written to stdout, because the parent parses it.

    request   {"id": 1, "method": "open_lens", "params": {"path": "..."}}
    response  {"id": 1, "ok": true,  "result": {...}}
              {"id": 1, "ok": false, "error": {"kind": "...", "message": "..."}}

Diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any, Callable, TextIO

from . import __version__
from .backend import BACKEND_NAMES, Backend, create_backend
from pydantic import ValidationError

from .errors import CodeVError, InternalError, ParameterError
from .models import AnalysisRequest, CreateLensRequest, StructureRequest, UpdateRequest

PROTOCOL_VERSION = 1


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _handle_open_lens(backend: Backend, params: dict[str, Any]):
    path = params.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ParameterError("open_lens needs a non empty path string.")
    return backend.open_lens(path)


def _handle_update_lens(backend: Backend, params: dict[str, Any]):
    request = _validate(UpdateRequest, params.get("request") or params)
    return backend.update_lens(request)


def _handle_create_lens(backend: Backend, params: dict[str, Any]):
    return backend.create_lens(_validate(CreateLensRequest, params.get("request") or params))


def _handle_edit_lens_structure(backend: Backend, params: dict[str, Any]):
    return backend.edit_lens_structure(
        _validate(StructureRequest, params.get("request") or params)
    )


def _handle_run_analysis(backend: Backend, params: dict[str, Any]):
    request = _validate(AnalysisRequest, params.get("request") or params)
    return backend.run_analysis(request)


def _validate(model, payload):
    """Turn a schema violation into a structured parameter error."""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ParameterError(
            f"Invalid {model.__name__}.",
            details={"errors": exc.errors(include_url=False, include_input=False,
                                            include_context=False)},
        ) from exc


def _handle_save_lens_as(backend: Backend, params: dict[str, Any]):
    path = params.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ParameterError("save_lens_as needs a non empty path string.")
    return backend.save_lens_as(path)


HANDLERS: dict[str, Callable[[Backend, dict[str, Any]], Any]] = {
    "get_status": lambda backend, params: backend.get_status(),
    "open_lens": _handle_open_lens,
    "create_lens": _handle_create_lens,
    "get_lens": lambda backend, params: backend.get_lens(params.get("zoom_position")),
    "update_lens": _handle_update_lens,
    "edit_lens_structure": _handle_edit_lens_structure,
    "run_analysis": _handle_run_analysis,
    "get_analysis": lambda backend, params: backend.get_analysis(),
    "cancel_analysis": lambda backend, params: backend.cancel_analysis(),
    "save_lens_as": _handle_save_lens_as,
    "close_session": lambda backend, params: backend.close_session(),
}


def handle_request(backend: Backend, request: dict[str, Any]) -> dict[str, Any]:
    """Execute one request and return the response object."""
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    if method == "ping":
        return {
            "id": request_id,
            "ok": True,
            "result": {
                "protocol": PROTOCOL_VERSION,
                "service_version": __version__,
                "backend": backend.name,
            },
        }
    if method == "shutdown":
        backend.stop()
        return {"id": request_id, "ok": True, "result": {"stopped": True}}
    if method not in HANDLERS:
        return {
            "id": request_id,
            "ok": False,
            "error": {
                "kind": "parameter",
                "message": f"unknown method {method!r}",
                "details": {"known_methods": sorted(HANDLERS)},
            },
        }
    if not isinstance(params, dict):
        return {
            "id": request_id,
            "ok": False,
            "error": {"kind": "parameter", "message": "params must be an object"},
        }

    try:
        result = HANDLERS[method](backend, params)
    except CodeVError as exc:
        return {"id": request_id, "ok": False, "error": exc.to_info().model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001 - the worker must answer every request
        log("unhandled error:\n" + traceback.format_exc())
        info = InternalError(f"{type(exc).__name__}: {exc}").to_info()
        return {"id": request_id, "ok": False, "error": info.model_dump(mode="json")}

    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return {"id": request_id, "ok": True, "result": payload}


def serve(backend: Backend, stdin: TextIO, stdout: TextIO) -> int:
    log(f"worker ready: backend={backend.name} protocol={PROTOCOL_VERSION}")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(
                json.dumps(
                    {
                        "id": None,
                        "ok": False,
                        "error": {"kind": "parameter", "message": f"invalid JSON: {exc}"},
                    }
                )
                + "\n"
            )
            stdout.flush()
            continue

        response = handle_request(backend, request)
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()
        if request.get("method") == "shutdown":
            log("worker shutting down on request")
            return 0
    log("stdin closed; worker exiting")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CODE V MCP worker process.")
    parser.add_argument("--backend", choices=BACKEND_NAMES, default="simulated")
    parser.add_argument("--working-directory", default=None)
    parser.add_argument("--version", action="version", version=f"codev-mcp {__version__}")
    args = parser.parse_args(argv)

    kwargs: dict[str, Any] = {}
    if args.working_directory:
        kwargs["working_directory"] = args.working_directory
    try:
        backend = create_backend(args.backend, **kwargs)
    except Exception as exc:  # noqa: BLE001 - report why the worker cannot start
        log(f"failed to create backend {args.backend!r}: {type(exc).__name__}: {exc}")
        return 2
    try:
        return serve(backend, sys.stdin, sys.stdout)
    finally:
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
