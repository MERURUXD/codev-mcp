"""Client side of the worker protocol.

The MCP server is a thin adapter: it validates tool arguments, forwards them to
a worker process and turns the response back into public models. Keeping the
process boundary here means a CODE V crash can be contained and observed
instead of taking the MCP server down with it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .errors import ErrorInfo, NotReadyError, SessionInvalidError
from .worker import PROTOCOL_VERSION

_SRC_ROOT = Path(__file__).resolve().parent.parent

#: Longer than one CODE V command may run (com_session.DEFAULT_COMMAND_TIMEOUT_MS), so a slow
#: command ends with CODE V's own timeout and a structured error instead of a killed worker.
DEFAULT_TIMEOUT_SECONDS = 660.0
#: The handshake covers the worker process start and its imports only; CODE V is
#: started by the first lens call, which gets the start budget the worker reports.
STARTUP_TIMEOUT_SECONDS = 300.0
#: A shutdown runs StopCodeV, waits up to 20 s for the processes to exit and kills what is left
#: (5 s each); a worker killed half way leaves processes and the session record behind.
SHUTDOWN_TIMEOUT_SECONDS = 120.0


class WorkerClient:
    """Owns one worker subprocess and serialises calls to it."""

    def __init__(
        self,
        backend: str = "simulated",
        *,
        working_directory: str | None = None,
        python_executable: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        stderr_tail_lines: int = 40,
    ) -> None:
        self.backend = backend
        self.working_directory = working_directory
        self.python_executable = python_executable or sys.executable
        self.timeout = timeout
        self.stderr_tail_lines = stderr_tail_lines
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._responses: "queue.Queue[str | None]" = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self.last_error: ErrorInfo | None = None
        self.session_invalid = False
        self.aborted = False
        #: Extra seconds the next call may need to start the CODE V session, as the worker last reported.
        self.session_start_seconds = 0.0

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        command = [
            self.python_executable,
            "-m",
            "codev_mcp.worker",
            "--backend",
            self.backend,
        ]
        if self.working_directory:
            command += ["--working-directory", self.working_directory]

        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(_SRC_ROOT) if not existing else str(_SRC_ROOT) + os.pathsep + existing
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"

        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(_SRC_ROOT.parent),
            env=environment,
        )
        self._stderr_lines = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._responses = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_responses, daemon=True)
        self._reader_thread.start()
        self.aborted = False

        reply = self.call("ping", timeout=STARTUP_TIMEOUT_SECONDS)
        if reply.get("protocol") != PROTOCOL_VERSION:
            raise NotReadyError(
                "Worker reported an unexpected protocol version.",
                details={"expected": PROTOCOL_VERSION, "received": reply.get("protocol")},
            )

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr_lines.append(line.rstrip("\n"))
            del self._stderr_lines[: -self.stderr_tail_lines]

    def _read_responses(self) -> None:
        """Feed worker replies to the waiting caller; None marks end of stream."""
        process = self._process
        if process is None or process.stdout is None:
            self._responses.put(None)
            return
        try:
            for line in process.stdout:
                self._responses.put(line)
        except Exception:  # noqa: BLE001 - the pipe closed
            pass
        finally:
            self._responses.put(None)

    def _abort(self, reason: str) -> None:
        """Stop a worker that stopped answering, so the service cannot wedge."""
        self.log_message(f"aborting the worker: {reason}")
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.session_invalid = True
        self.aborted = True

    def log_message(self, message: str) -> None:
        self._stderr_lines.append(f"client: {message}")
        del self._stderr_lines[: -self.stderr_tail_lines]

    def close(self, *, terminate: bool = True) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.call("shutdown", timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - closing must not raise
                pass
        if process.poll() is None and terminate:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._process = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines)

    # ------------------------------------------------------------------- calls

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None):
        """Send one request and return its result, raising CodeVError on failure."""
        process = self._process
        if process is None or process.poll() is not None:
            raise self._not_ready(f"worker is not running (method {method})")
        if process.stdin is None or process.stdout is None:
            raise self._not_ready("worker pipes are not available")

        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            deadline = self.timeout + self.session_start_seconds if timeout is None else timeout
            payload = json.dumps(
                {"id": request_id, "method": method, "params": params or {}}
            )
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise self._not_ready(f"worker refused the request: {exc}") from exc

            try:
                line = self._responses.get(timeout=deadline)
            except queue.Empty:
                message = (
                    f"the worker did not answer {method} within {deadline:.0f} seconds and was "
                    "stopped"
                )
                self._abort(message)
                raise self._not_ready(
                    message,
                    details={"method": method, "timeout_seconds": deadline},
                ) from None
            if line is None:
                raise self._not_ready(
                    f"worker exited while handling {method}",
                    details={"returncode": process.poll(), "stderr": self.stderr_tail()},
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self._abort(f"worker returned unparsable output: {exc}")
                raise self._not_ready(
                    f"worker returned unparsable output: {exc}",
                    details={"line": line[:500]},
                ) from exc
            if not isinstance(response, dict):
                message = "worker returned a non-object response; the worker was stopped"
                self._abort(message)
                raise self._not_ready(message, details={"line": line[:500]})
            if response.get("id") != request_id:
                # A response for another request id means the stream is out of
                # sync; continuing would hand one call's answer to the next.
                # Aborting the worker is the only way back to a known state.
                message = (
                    f"worker answered request {request_id} with id "
                    f"{response.get('id')!r}; the worker was stopped"
                )
                self._abort(message)
                raise self._not_ready(
                    message, details={"method": method, "line": line[:500]}
                )
            budget = response.get("session_start_seconds")
            if isinstance(budget, (int, float)) and not isinstance(budget, bool) and 0 <= budget < float("inf"):
                self.session_start_seconds = float(budget)

        if response.get("ok"):
            return response.get("result")

        error = ErrorInfo.model_validate(response.get("error") or {})
        self.last_error = error
        if error.kind.value == "session_invalid":
            self.session_invalid = True
        raise _error_from_info(error)

    def _not_ready(self, message: str, details: dict[str, Any] | None = None) -> NotReadyError:
        merged = dict(details or {})
        merged.setdefault("stderr", self.stderr_tail())
        merged.setdefault("backend", self.backend)
        error = NotReadyError(message, details=merged)
        self.last_error = error.to_info()
        return error


def _error_from_info(info: ErrorInfo):
    from . import errors

    mapping = {
        errors.ErrorKind.PARAMETER: errors.ParameterError,
        errors.ErrorKind.NOT_READY: errors.NotReadyError,
        errors.ErrorKind.COMPUTATION: errors.ComputationError,
        errors.ErrorKind.SESSION_INVALID: errors.SessionInvalidError,
        errors.ErrorKind.UNSUPPORTED: errors.UnsupportedError,
        errors.ErrorKind.NOT_FOUND: errors.NotFoundError,
        errors.ErrorKind.INTERNAL: errors.InternalError,
    }
    exception = mapping.get(info.kind, errors.InternalError)
    return exception(
        info.message, details=info.details, raw_output=info.raw_output, hint=info.hint
    )
