"""End to end tests: worker protocol and MCP client connection.

These tests are the Phase B acceptance check from PLAN.md: a client can list and
call every tool, the simulated flow runs end to end, and every result is marked
as simulated.

Two transports are covered. The worker is driven over its own pipe protocol with
subprocess.Popen, and the MCP server is driven over the in-memory transport plus
one raw JSON-RPC exchange over a real stdio subprocess. The asyncio subprocess
transport is avoided because it needs named pipe permissions that a sandboxed
test host may not grant.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from codev_mcp.client import WorkerClient
from codev_mcp.errors import NotFoundError, NotReadyError, parse_tool_error_message
from codev_mcp.server import build_server

SRC = Path(__file__).resolve().parents[1] / "src"
WORKSPACE = SRC.parent
EXPECTED_TOOLS = {
    "get_status",
    "open_lens",
    "create_lens",
    "get_lens",
    "update_lens",
    "edit_lens_structure",
    "run_analysis",
    "get_analysis",
    "cancel_analysis",
    "save_lens_as",
    "close_session",
}

SIMPLE_LENS = {
    "aperture_value": 12,
    "wavelengths_nm": [550],
    "fields": [{"x_angle": 0, "y_angle": 0}],
    "surfaces": [
        {"radius": 40, "thickness": 5, "glass": "BK7_SCHOTT"},
        {"radius": -40, "thickness": 10},
    ],
    "stop_surface": 1,
}


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    if os.environ.get("CODEV_MCP_TEST_INSTALLED") != "1":
        environment["PYTHONPATH"] = str(SRC) if not existing else str(SRC) + os.pathsep + existing
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


class WorkerProtocol(unittest.TestCase):
    def setUp(self) -> None:
        from tests import workspace_temp_directory

        self._temp = workspace_temp_directory("worker")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.lens = self.root / "dbgauss.len"
        self.lens.write_text("! placeholder\n", encoding="utf-8")
        self.client = WorkerClient("simulated", working_directory=str(self.root / "run"), timeout=60)
        self.addCleanup(self.client.close)
        self.client.start()

    def test_ping_reports_the_protocol_and_backend(self):
        reply = self.client.call("ping")
        self.assertEqual(reply["backend"], "simulated")
        self.assertIn("protocol", reply)

    def test_construct_and_edit_structure_through_worker(self):
        lens = self.client.call("create_lens", {"request": SIMPLE_LENS})
        self.assertEqual(len(lens["surfaces"]), 4)
        result = self.client.call("edit_lens_structure", {"request": {"operations": [
            {"kind": "insert_sphere", "before_surface": 2, "radius": None,
             "thickness": 1},
            {"kind": "set_stop", "surface": 2},
        ]}})
        self.assertTrue(result["applied"])
        self.assertEqual(result["source"], "simulated")
        self.assertEqual(result["lens"]["stop_surface"], 2)
        self.assertEqual(len(result["lens"]["surfaces"]), 5)
        self.assertEqual(self.client.call("get_status")["details"]["committed_revision"], 1)

    def test_invalid_structure_shape_keeps_worker_alive(self):
        self.client.call("create_lens", {"request": SIMPLE_LENS})
        with self.assertRaises(Exception) as caught:
            self.client.call("edit_lens_structure", {"request": {"operations": [
                {"kind": "set_stop", "surface": 1, "before_surface": 2},
            ]}})
        self.assertEqual(caught.exception.kind.value, "parameter")
        self.assertTrue(self.client.alive)
        self.assertEqual(self.client.call("get_status")["details"]["committed_revision"], 0)

    def test_full_flow_through_the_worker(self):
        opened = self.client.call("open_lens", {"path": str(self.lens)})
        self.assertEqual(opened["source"], "simulated")
        update = self.client.call(
            "update_lens",
            {"request": {"edits": [{"surface": 3, "parameter": "thickness", "value": 12.5}]}},
        )
        self.assertTrue(update["outcomes"][0]["applied"])
        task = self.client.call("run_analysis", {"request": {"kind": "first_order"}})
        self.assertEqual(task["state"], "queued")
        self.client.call("get_analysis")
        finished = self.client.call("get_analysis")
        self.assertEqual(finished["task"]["state"], "succeeded")
        self.assertAlmostEqual(
            finished["first_order"]["effective_focal_length"], 100.000123456789
        )

    def test_errors_cross_the_process_boundary_with_their_kind(self):
        with self.assertRaises(NotFoundError) as caught:
            self.client.call("open_lens", {"path": str(self.root / "nope.len")})
        self.assertEqual(caught.exception.kind.value, "not_found")

    def test_unknown_method_is_a_parameter_error(self):
        with self.assertRaises(Exception) as caught:
            self.client.call("does_not_exist")
        self.assertEqual(caught.exception.kind.value, "parameter")

    def test_missing_arguments_are_rejected(self):
        with self.assertRaises(Exception) as caught:
            self.client.call("open_lens", {})
        self.assertEqual(caught.exception.kind.value, "parameter")

    def test_native_plot_crosses_the_worker_boundary(self):
        self.client.call("open_lens", {"path": str(self.lens)})
        task = self.client.call(
            "run_analysis",
            {"request": {"kind": "native_plot", "options": {"plot_type": "mtf"}}},
        )
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["settings"]["plot_type"], "mtf")
        self.client.call("get_analysis")
        finished = self.client.call("get_analysis")
        self.assertEqual(finished["task"]["state"], "succeeded")
        native = finished["native_plot"]
        self.assertEqual(native["plot_type"], "mtf")
        self.assertEqual(native["source"], "simulated")
        self.assertIsNone(native["plot_file_path"])
        self.assertIsNone(native["plot_file_bytes"])
        self.assertTrue(native["image"]["base64_data"])

    def test_an_unknown_plot_type_cannot_be_smuggled_through(self):
        self.client.call("open_lens", {"path": str(self.lens)})
        with self.assertRaises(Exception) as caught:
            self.client.call(
                "run_analysis",
                {
                    "request": {
                        "kind": "native_plot",
                        "options": {"plot_type": "layout; go; del all"},
                    }
                },
            )
        self.assertEqual(caught.exception.kind.value, "parameter")

    def test_an_out_of_sync_response_aborts_the_worker(self):
        """A reply that does not carry the request id must not be consumed.

        The worker answers requests one by one, so a mismatched id means the
        stream is desynchronised; the client has to stop the worker instead of
        handing this answer to the next call.
        """
        process = self.client._process
        self.assertIsNotNone(process)
        # Steal the real ping reply, then hand the next request a stale id.
        self.client._responses.put(json.dumps({"id": 999, "ok": True, "result": {}}) + "\n")
        with self.assertRaises(Exception) as caught:
            self.client.call("get_status")
        self.assertEqual(caught.exception.kind.value, "not_ready")
        self.assertTrue(self.client.aborted)
        self.assertTrue(self.client.session_invalid)
        # The abort must not leave a live worker behind.
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())

    def test_malformed_responses_abort_the_worker(self):
        for line in ("not json\n", "[]\n", "null\n"):
            with self.subTest(line=line):
                client = WorkerClient()
                try:
                    client.start()
                    process = client._process
                    client._responses.put(line)
                    with self.assertRaises(NotReadyError):
                        client.call("get_status")
                    self.assertTrue(client.aborted)
                    self.assertTrue(client.session_invalid)
                    self.assertIsNotNone(client.last_error)
                    self.assertIsNotNone(process.poll())
                finally:
                    client.close()

    def test_closing_the_worker_makes_further_calls_fail(self):
        self.client.close()
        with self.assertRaises(Exception) as caught:
            self.client.call("get_status")
        self.assertEqual(caught.exception.kind.value, "not_ready")

    def test_the_worker_exits_when_its_input_closes(self):
        """A disconnected MCP client must not leave the worker holding a session."""
        from codev_mcp.client import WorkerClient

        client = WorkerClient("simulated", working_directory=str(self.root / "run"), timeout=30)
        self.addCleanup(client.close)
        client.start()
        process = client._process
        self.assertIsNotNone(process)
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except Exception:  # noqa: BLE001 - the assertion below reports the failure
            pass
        self.assertEqual(process.poll(), 0, "worker did not exit after its input closed")


class WorkerTimeoutAndRebuild(unittest.TestCase):
    """A call that starts CODE V gets its start budget, and a killed worker is replaced (review H1)."""

    def setUp(self) -> None:
        from tests import workspace_temp_directory

        self._temp = workspace_temp_directory("rebuild")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def test_the_worker_reports_the_start_budget_with_every_response(self):
        import io

        from codev_mcp import worker
        from codev_mcp.simulated import SimulatedBackend

        backend = SimulatedBackend(working_directory=str(self.root / "run"))
        with unittest.mock.patch.object(SimulatedBackend, "session_start_seconds", 42.0):
            stdout = io.StringIO()
            worker.serve(backend, io.StringIO('{"id": 1, "method": "ping"}\n'), stdout)
        self.assertEqual(json.loads(stdout.getvalue())["session_start_seconds"], 42.0)


    def test_the_worker_survives_a_non_object_request_and_a_failing_shutdown(self):
        """Review L6: either one used to end the worker with an uncaught exception."""
        import io

        from codev_mcp import worker
        from codev_mcp.simulated import SimulatedBackend

        backend = SimulatedBackend(working_directory=str(self.root / "run"))
        stdout = io.StringIO()
        with unittest.mock.patch.object(SimulatedBackend, "stop", side_effect=RuntimeError("stuck")):
            code = worker.serve(backend, io.StringIO(
                '[1]\n{"id": 2, "method": "ping"}\n{"id": 3, "method": "shutdown"}\n'), stdout)
        self.assertEqual(code, 0)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([reply["ok"] for reply in replies], [False, True, False])
        self.assertEqual(replies[0]["error"]["kind"], "parameter")
        self.assertEqual(replies[2]["error"]["kind"], "internal")

    def test_the_client_adds_the_reported_budget_to_the_next_call(self):
        import io
        import queue

        client = WorkerClient("simulated", timeout=5)
        budgets = iter([100, 0, 0, 0])
        seen: list[float] = []

        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO()

            def poll(self):
                return None

        class Replies(queue.Queue):
            def get(self, block=True, timeout=None):
                seen.append(timeout)
                return json.dumps({"id": client._next_id, "ok": True, "result": {},
                                   "session_start_seconds": next(budgets)})

        client._process = Process()
        client._responses = Replies()
        client.call("get_status")
        client.call("open_lens")  # the previous reply said the next call may start CODE V
        client.call("get_lens")
        client.call("shutdown", timeout=30)  # an explicit bound stays as given
        self.assertEqual(seen, [5, 105, 5, 30])

    def test_shutdown_allows_the_backend_its_whole_cleanup(self):
        from codev_mcp import client as client_module

        client = WorkerClient("simulated", working_directory=str(self.root / "run"), timeout=60)
        client.start()
        seen: list[float | None] = []
        original = client.call

        def recording(method, params=None, *, timeout=None):
            seen.append(timeout)
            return original(method, params, timeout=timeout)

        client.call = recording
        client.close()
        self.assertEqual(seen, [client_module.SHUTDOWN_TIMEOUT_SECONDS])
        self.assertGreaterEqual(client_module.SHUTDOWN_TIMEOUT_SECONDS, 90)  # review M9

    def _state(self, **kwargs):
        from codev_mcp.server import ServiceState

        state = ServiceState("simulated", working_directory=str(self.root / "run"),
                             python_executable=None, timeout=60, **kwargs)
        self.addCleanup(state.close)
        state.start()
        return state

    def test_a_killed_worker_is_replaced_on_the_next_call(self):
        state = self._state()
        old = state.client
        old._abort("test timeout")
        self.assertFalse(old.alive)
        new = state.require()
        self.assertIsNot(new, old)
        self.assertTrue(new.alive)
        self.assertEqual(state.restarts, 1)
        self.assertEqual(new.call("get_status")["backend"], "simulated")

    def test_a_replacement_that_cannot_start_is_reported_once(self):
        state = self._state()
        state.python_executable = str(self.root / "missing-python.exe")
        state.client._abort("test timeout")
        with self.assertRaises(NotReadyError):
            state.require()
        self.assertIsNone(state.client)
        with self.assertRaises(NotReadyError):
            state.require()
        self.assertEqual(state.restarts, 1)


class McpToolSurface(unittest.TestCase):
    """Drives the server through the in-memory MCP transport."""
    def setUp(self) -> None:
        from tests import workspace_temp_directory

        self._temp = workspace_temp_directory("mcp")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.lens = self.root / "dbgauss.len"
        self.lens.write_text("! placeholder\n", encoding="utf-8")
        self.save_target = self.root / "saved.len"

    def _server(self):
        return build_server("simulated", working_directory=str(self.root / "run"))

    def test_modeling_tools_through_mcp(self):
        async def flow() -> None:
            async with create_connected_server_and_client_session(self._server()) as session:
                await session.initialize()
                created = await session.call_tool("create_lens", {"request": SIMPLE_LENS})
                self.assertFalse(created.isError)
                self.assertEqual(len(json.loads(created.content[0].text)["surfaces"]), 4)
                edited = await session.call_tool("edit_lens_structure", {"request": {
                    "operations": [{"kind": "delete_surface", "surface": 1,
                                    "new_stop_surface": 1}],
                }})
                self.assertFalse(edited.isError, edited.content)
                payload = json.loads(edited.content[0].text)
                self.assertTrue(payload["applied"])
                self.assertEqual(len(payload["lens"]["surfaces"]), 3)
        anyio.run(flow)

    def test_tool_discovery_and_full_simulated_flow(self):
        async def flow() -> None:
            async with create_connected_server_and_client_session(self._server()) as session:
                await session.initialize()

                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertEqual(names, EXPECTED_TOOLS)
                for tool in listed.tools:
                    self.assertTrue(tool.description, f"{tool.name} has no description")

                status = await session.call_tool("get_status", {})
                self.assertFalse(status.isError)
                payload = json.loads(status.content[0].text)
                self.assertEqual(payload["source"], "simulated")
                self.assertTrue(payload["ready"])

                opened = await session.call_tool("open_lens", {"path": str(self.lens)})
                self.assertFalse(opened.isError)
                lens = json.loads(opened.content[0].text)
                self.assertEqual(len(lens["surfaces"]), 13)
                self.assertEqual(lens["source"], "simulated")

                updated = await session.call_tool(
                    "update_lens",
                    {"request": {"edits": [{"surface": 3, "parameter": "thickness", "value": 12.5}]}},
                )
                self.assertFalse(updated.isError)
                self.assertTrue(json.loads(updated.content[0].text)["outcomes"][0]["applied"])

                read_back = await session.call_tool("get_lens", {})
                surfaces = json.loads(read_back.content[0].text)["surfaces"]
                self.assertAlmostEqual(surfaces[3]["thickness"], 12.5)

                saved = await session.call_tool("save_lens_as", {"path": str(self.save_target)})
                self.assertFalse(saved.isError)
                self.assertTrue(self.save_target.exists())

                submitted = await session.call_tool(
                    "run_analysis",
                    {"request": {"kind": "spot_diagram", "options": {"ray_grid": 7}}},
                )
                self.assertFalse(submitted.isError)
                self.assertEqual(json.loads(submitted.content[0].text)["state"], "queued")

                await session.call_tool("get_analysis", {})
                finished = await session.call_tool("get_analysis", {})
                self.assertFalse(finished.isError)
                kinds = [block.type for block in finished.content]
                self.assertIn("text", kinds)
                self.assertIn("image", kinds)
                snapshot = json.loads(finished.content[0].text)
                self.assertEqual(snapshot["task"]["state"], "succeeded")
                self.assertEqual(snapshot["spot_diagram"]["source"], "simulated")
                self.assertTrue(snapshot["spot_diagram"]["size_is_radius"])
                image_block = next(block for block in finished.content if block.type == "image")
                self.assertEqual(image_block.mimeType, "image/png")
                self.assertGreater(len(image_block.data), 0)

                cancelled = await session.call_tool("cancel_analysis", {})
                self.assertFalse(cancelled.isError)

                closed = await session.call_tool("close_session", {})
                self.assertFalse(closed.isError)
                self.assertFalse(json.loads(closed.content[0].text)["session_open"])

        anyio.run(flow)

    def test_failed_call_keeps_the_structured_error(self):
        async def flow() -> None:
            async with create_connected_server_and_client_session(self._server()) as session:
                await session.initialize()
                result = await session.call_tool("open_lens", {"path": str(self.root / "missing.len")})
                self.assertTrue(result.isError)
                info = parse_tool_error_message(result.content[0].text)
                self.assertIsNotNone(info)
                self.assertEqual(info.kind.value, "not_found")
                self.assertIn("missing.len", info.message)

        anyio.run(flow)

    def test_native_plot_returns_metadata_and_its_own_image_block(self):
        async def flow() -> None:
            async with create_connected_server_and_client_session(self._server()) as session:
                await session.initialize()
                await session.call_tool("open_lens", {"path": str(self.lens)})
                submitted = await session.call_tool(
                    "run_analysis",
                    {"request": {"kind": "native_plot", "options": {"plot_type": "layout"}}},
                )
                self.assertFalse(submitted.isError)
                await session.call_tool("get_analysis", {})
                finished = await session.call_tool("get_analysis", {})
                self.assertFalse(finished.isError)
                snapshot = json.loads(finished.content[0].text)
                self.assertEqual(snapshot["task"]["state"], "succeeded")
                native = snapshot["native_plot"]
                self.assertEqual(native["plot_type"], "layout")
                self.assertEqual(native["source"], "simulated")
                self.assertIsNone(native["plot_file_path"])
                self.assertIsNone(native["plot_file_bytes"])
                # The JSON metadata keeps a placeholder; the bytes travel as a
                # separate image content block.
                self.assertEqual(
                    native["image"]["base64_data"], "<returned as MCP image content>"
                )
                images = [block for block in finished.content if block.type == "image"]
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0].mimeType, "image/png")
                self.assertGreater(len(images[0].data), 0)

        anyio.run(flow)

    def test_only_the_settings_the_caller_set_reach_a_native_plot(self):
        """A model default must not look like a setting the caller chose.

        The server forwards only the fields the caller really set, so the
        default mtf_type is not sent on for a native plot, while a value the
        caller did pass is refused.
        """

        async def flow() -> None:
            async with create_connected_server_and_client_session(self._server()) as session:
                await session.initialize()
                await session.call_tool("open_lens", {"path": str(self.lens)})
                accepted = await session.call_tool(
                    "run_analysis",
                    {"request": {"kind": "native_plot", "options": {"plot_type": "spot"}}},
                )
                self.assertFalse(accepted.isError)
                await session.call_tool("get_analysis", {})
                await session.call_tool("get_analysis", {})
                rejected = await session.call_tool(
                    "run_analysis",
                    {
                        "request": {
                            "kind": "native_plot",
                            "options": {
                                "plot_type": "spot",
                                "mtf_type": "diffraction",
                            },
                        }
                    },
                )
                self.assertTrue(rejected.isError)
                info = parse_tool_error_message(rejected.content[0].text)
                self.assertIsNotNone(info)
                self.assertEqual(info.kind.value, "parameter")
                self.assertEqual(info.details["not_accepted"], ["mtf_type"])

        anyio.run(flow)


    def test_a_slow_tool_call_does_not_block_other_requests(self):
        """Worker calls run in a thread, so the server still answers while one is busy (review M2)."""
        import threading
        import time

        original = WorkerClient.call
        release = threading.Event()

        def slow_call(client, method, params=None, *, timeout=None):
            if method == "get_lens":
                release.wait(10)
            return original(client, method, params, timeout=timeout)

        async def flow() -> None:
            async with create_connected_server_and_client_session(self._server()) as session:
                await session.initialize()
                await session.call_tool("open_lens", {"path": str(self.lens)})
                async with anyio.create_task_group() as group:
                    started = time.perf_counter()
                    group.start_soon(session.call_tool, "get_lens", {})
                    await anyio.sleep(0.2)
                    await session.send_ping()
                    tools = await session.list_tools()
                    elapsed = time.perf_counter() - started
                    release.set()
                self.assertLess(elapsed, 2.0)
                self.assertEqual({tool.name for tool in tools.tools}, EXPECTED_TOOLS)

        with unittest.mock.patch.object(WorkerClient, "call", slow_call):
            anyio.run(flow)

    def test_a_worker_that_cannot_start_is_reported_not_crashed(self):
        async def flow() -> None:
            # A worker that cannot even be spawned is the cheapest way to reach
            # the degraded path without starting a real CODE V session.
            server = build_server(
                "simulated",
                working_directory=str(self.root / "run"),
                python_executable=str(self.root / "missing-python.exe"),
            )
            async with create_connected_server_and_client_session(server) as session:
                await session.initialize()

                status = await session.call_tool("get_status", {})
                self.assertFalse(status.isError)
                payload = json.loads(status.content[0].text)
                self.assertFalse(payload["ready"])
                self.assertFalse(payload["session_open"])
                self.assertTrue(payload["warnings"])

                listing = await session.call_tool("get_lens", {})
                self.assertTrue(listing.isError)
                info = parse_tool_error_message(listing.content[0].text)
                self.assertEqual(info.kind.value, "not_ready")

        anyio.run(flow)


class ToolErrorSize(unittest.TestCase):
    def test_a_huge_raw_output_is_cut_to_its_ends(self):
        """A 2 MB text buffer in an error message overwhelmed MCP clients (review L5)."""
        from codev_mcp.errors import ComputationError
        from codev_mcp.server import MAX_ERROR_RAW_OUTPUT, _tool_error

        raw = "Error: first line\n" + "x" * 2_000_000 + "\nlast line"
        info = parse_tool_error_message(str(_tool_error(ComputationError("boom", raw_output=raw))))
        self.assertLess(len(info.raw_output), MAX_ERROR_RAW_OUTPUT + 100)
        self.assertTrue(info.raw_output.startswith("Error: first line"))
        self.assertTrue(info.raw_output.endswith("last line"))
        short = parse_tool_error_message(str(_tool_error(ComputationError("boom", raw_output="ok"))))
        self.assertEqual(short.raw_output, "ok")

class ServerStdioSmokeTest(unittest.TestCase):
    """One raw JSON-RPC round trip over a real stdio subprocess."""
    def setUp(self) -> None:
        from tests import workspace_temp_directory

        self._temp = workspace_temp_directory("stdio")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def test_server_answers_initialize_and_tools_list(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codev_mcp",
                "--backend",
                "simulated",
                "--working-directory",
                str(self.root / "run"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(WORKSPACE),
            env=_child_environment(),
        )
        self.addCleanup(process.kill)

        def close_pipes() -> None:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

        self.addCleanup(close_pipes)

        def send(payload: dict) -> None:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def read_response(request_id: int) -> dict:
            while True:
                line = process.stdout.readline()
                if line == "":
                    self.fail(
                        "server closed stdout; stderr was:\n" + (process.stderr.read() or "")
                    )
                message = json.loads(line)
                if message.get("id") == request_id:
                    return message

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "1.0"},
                },
            }
        )
        initialized = read_response(1)
        self.assertIn("result", initialized)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "codev-mcp")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = read_response(2)
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS)

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_status", "arguments": {}},
            }
        )
        called = read_response(3)
        status = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(status["backend"], "simulated")
        self.assertEqual(status["source"], "simulated")
        process.terminate()


class ServerConstruction(unittest.TestCase):
    def test_server_builds_without_starting_a_worker(self):
        server = build_server("simulated", start_worker=False)
        self.assertEqual(server.name, "codev-mcp")

    def test_the_default_working_directory_leaves_site_packages_alone(self):
        """An installed package used to put its runs beside site-packages (review L7)."""
        from codev_mcp.backend import default_working_directory

        checkout = WORKSPACE / "src" / "codev_mcp" / "backend.py"
        self.assertEqual(Path(default_working_directory(checkout)), WORKSPACE / ".codev-run")
        installed = Path(r"D:\Python\Lib\site-packages\codev_mcp\backend.py")
        self.assertEqual(
            Path(default_working_directory(installed, {"LOCALAPPDATA": r"D:\Profile\AppData\Local"})),
            Path(r"D:\Profile\AppData\Local\codev-mcp"))

    def test_unknown_backend_is_rejected(self):
        from codev_mcp.backend import create_backend

        with self.assertRaises(ValueError):
            create_backend("nope")


if __name__ == "__main__":
    unittest.main()
