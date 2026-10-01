"""Bounded, serial MCP stdio client used by the comparison workflow.

This is a protocol client, not a COM or worker client. Reader threads only drain
pipes; they never execute optical operations concurrently.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


#: How long one wait for a response lasts before the client looks for an interrupt again.
RESPONSE_POLL_SECONDS = 0.5


class ProtocolError(RuntimeError):
    pass


class StdioClient:
    def __init__(self, work: Path, backend: str, timeout: float, log_dir: Path):
        self.timeout = timeout
        self.broken = False
        self.sequence = 0
        self.responses: queue.Queue = queue.Queue()
        log_dir.mkdir(parents=True, exist_ok=True)
        self.transcript = (log_dir / "mcp.jsonl").open("x", encoding="utf-8")
        environment = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "codev_mcp", "--backend", backend,
                 "--working-directory", str(work), "--timeout", str(timeout)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=environment,
            )
        except BaseException:
            self.transcript.close()
            raise
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.logger = threading.Thread(target=self._read_stderr, args=(log_dir / "stderr.log",), daemon=True)
        self.reader.start()
        self.logger.start()
        try:
            self.server_info = self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "codev-compare", "version": "1"},
            })
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                self.responses.put(line)
        finally:
            self.responses.put(None)

    def _read_stderr(self, path: Path):
        with path.open("x", encoding="utf-8") as handle:
            for line in self.process.stderr:
                handle.write(line)
                handle.flush()

    def _send(self, message: dict):
        self.transcript.write(json.dumps({"send": message}, ensure_ascii=False) + "\n")
        self.transcript.flush()
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        if self.broken:
            raise ProtocolError("MCP stream is no longer usable")
        self.sequence += 1
        request_id = self.sequence
        deadline = time.monotonic() + (self.timeout + 15 if timeout is None else timeout)
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"MCP {method} timed out")
                try:
                    # Short slices: on Windows a lock wait with a long timeout does not react to Ctrl+C, so the
                    # interrupt would only be raised when the whole request timed out (F8).
                    line = self.responses.get(timeout=min(remaining, RESPONSE_POLL_SECONDS))
                except queue.Empty:
                    continue
                if line is None:
                    raise ProtocolError("MCP server closed stdout")
                message = json.loads(line)
                self.transcript.write(json.dumps({"receive": message}, ensure_ascii=False) + "\n")
                self.transcript.flush()
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ProtocolError("Invalid MCP response")
                if "id" not in message and "method" in message:
                    continue
                if message.get("id") != request_id:
                    raise ProtocolError("MCP response id mismatch")
                if "error" in message:
                    raise ProtocolError(f"MCP error: {message['error']}")
                return message["result"]
        except (OSError, ValueError, KeyError, ProtocolError, TimeoutError):
            self.broken = True
            raise
        except BaseException:
            # An interrupt leaves the request unanswered: the stream cannot be trusted any more.
            self.broken = True
            raise

    def call(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> tuple[dict, list]:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        blocks = result.get("content", [])
        texts = [block["text"] for block in blocks if block.get("type") == "text"]
        if result.get("isError"):
            raise RuntimeError(f"{name}: {' '.join(texts)}")
        if len(texts) != 1:
            raise ProtocolError(f"{name}: expected one JSON text block")
        return json.loads(texts[0]), [b for b in blocks if b.get("type") == "image"]

    def close(self) -> dict:
        """Let EOF trigger service-owned cleanup before terminating this server."""
        record = {"server_pid": self.process.pid, "close_session": None}
        try:
            if not self.broken and self.process.poll() is None:
                try:
                    record["close_session"] = self.call("close_session", timeout=max(30, self.timeout))[0]
                except Exception as exc:
                    record["close_error"] = str(exc)
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=self.timeout + 30 if self.broken else 30)
            except subprocess.TimeoutExpired:
                record["forced_server_stop"] = True
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
            record["returncode"] = self.process.returncode
        finally:
            self.reader.join(timeout=5)
            self.logger.join(timeout=5)
            for stream, thread in ((self.process.stdout, self.reader), (self.process.stderr, self.logger)):
                if not thread.is_alive():
                    stream.close()
            self.transcript.close()
        return record
