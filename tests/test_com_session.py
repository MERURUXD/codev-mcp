"""Tests for the COM session wrapper.

Only the parts that do not need a real CODE V installation are covered here:
the recovery file cleanup that stops StartCodeV from hanging, the strict parsing
of database item values, and the detection of an "Error:" line in command
output.
"""

from __future__ import annotations

import unittest.mock
import unittest
from pathlib import Path

from codev_mcp.com_session import ComSession
from codev_mcp.errors import ComputationError, InternalError, ParameterError
from tests import workspace_temp_directory


class StubObject:
    """Records what the session wrapper sent to the COM object."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def Command(self, text: str) -> str:
        self.calls.append(("Command", text))
        return self.responses.get(f"command:{text}", "Command End:\r\n")

    def EvaluateExpression(self, item: str) -> str:
        self.calls.append(("EvaluateExpression", item))
        return self.responses.get(f"eval:{item}", "0")


class RecoveryFileCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = workspace_temp_directory("comsession")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def test_removes_recovery_files_and_leaves_other_files(self):
        (self.root / "codev.rec").write_text("", encoding="utf-8")
        (self.root / "codev.1.rec").write_text("", encoding="utf-8")
        keep = self.root / "my_lens.len"
        keep.write_text("lens", encoding="utf-8")

        session = ComSession(starting_directory=self.root)
        removed = session.clear_recovery_files()

        self.assertEqual(sorted(removed), ["codev.1.rec", "codev.rec"])
        self.assertFalse((self.root / "codev.rec").exists())
        self.assertFalse((self.root / "codev.1.rec").exists())
        self.assertTrue(keep.exists())
        self.assertEqual(session.removed_recovery_files, removed)

    def test_a_clean_directory_reports_nothing(self):
        session = ComSession(starting_directory=self.root)
        self.assertEqual(session.clear_recovery_files(), [])

    def test_without_a_working_directory_nothing_happens(self):
        session = ComSession()
        self.assertEqual(session.clear_recovery_files(), [])

    def test_an_undeletable_recovery_file_is_reported(self):
        target = self.root / "codev.rec"
        target.write_text("", encoding="utf-8")
        session = ComSession(starting_directory=self.root)

        original_unlink = Path.unlink

        def failing_unlink(self, *args, **kwargs):  # noqa: ANN001
            raise OSError("locked by another session")

        Path.unlink = failing_unlink
        try:
            with self.assertRaises(ComputationError) as caught:
                session.clear_recovery_files()
        finally:
            Path.unlink = original_unlink
        self.assertIn("recovery file", caught.exception.message)
        self.assertIn("locked by another session", str(caught.exception.details))


class CommandAndEvaluation(unittest.TestCase):
    def make_session(self, responses: dict[str, str] | None = None) -> tuple[ComSession, StubObject]:
        session = ComSession()
        stub = StubObject(responses)
        session._object = stub
        return session, stub

    def test_an_error_line_becomes_a_parameter_error(self):
        session, _ = self.make_session(
            {"command:RDY S99 1.0": "Error:   Surface qualifier out of range\r\nCommand End:\r\n"}
        )
        with self.assertRaises(ParameterError) as caught:
            session.command("RDY S99 1.0")
        self.assertIn("out of range", caught.exception.message)
        self.assertIn("Command End", caught.exception.raw_output or "")

    def test_command_error_kind_can_be_overridden(self):
        session, _ = self.make_session({"command:X": "Error:   failed\r\n"})
        with self.assertRaises(ComputationError):
            session.command("X", error_kind=ComputationError)

    def test_newlines_are_never_sent(self):
        session, stub = self.make_session()
        with self.assertRaises(ParameterError):
            session.command("RDY S1 1.0\nRDY S2 2.0")
        self.assertEqual(stub.calls, [])

    def test_evaluate_number_rejects_a_non_numeric_answer(self):
        session, _ = self.make_session({"eval:(BFL Z1)": "56.12344725343356"})
        self.assertAlmostEqual(session.evaluate_number("(BFL Z1)"), 56.12344725343356)

        session, _ = self.make_session({"eval:(BFL Z1)": "NO SOLVE"})
        with self.assertRaises(InternalError):
            session.evaluate_number("(BFL Z1)")

    def test_evaluate_strips_whitespace(self):
        session, _ = self.make_session({"eval:(TIT)": "  A lens title \r\n"})
        self.assertEqual(session.evaluate("(TIT)"), "A lens title")

    def test_calls_without_a_session_are_reported_as_invalid(self):
        from codev_mcp.errors import SessionInvalidError

        session = ComSession()
        with self.assertRaises(SessionInvalidError):
            session.evaluate("(NUM S)")

    def test_a_live_engine_does_not_block_calls(self):
        from codev_mcp import com_session

        session, _ = self.make_session({"eval:(NUM S)": "12"})
        session.engine_pids = {4242}
        original = com_session.list_codev_processes
        com_session.list_codev_processes = lambda: {4242: "codevm.exe"}
        original_alive = com_session.engine_is_alive
        com_session.engine_is_alive = lambda pid: pid == 4242
        try:
            self.assertEqual(session.evaluate("(NUM S)"), "12")
        finally:
            com_session.list_codev_processes = original
            com_session.engine_is_alive = original_alive

    def test_without_recorded_processes_the_check_is_skipped(self):
        session, _ = self.make_session({"eval:(NUM S)": "12"})
        self.assertEqual(session.engine_pids, set())
        self.assertEqual(session.evaluate("(NUM S)"), "12")

    def test_a_session_without_an_engine_process_is_refused(self):
        session = ComSession()
        session.owned_processes = {10: "cvcommand.exe", 11: "cvcomsvr.exe"}
        with self.assertRaises(ComputationError) as caught:
            session._require_engine_started()
        self.assertIn("no engine process", caught.exception.message)
        self.assertIn("10", str(caught.exception.details))

    def test_an_engine_process_satisfies_the_check(self):
        session = ComSession()
        session.owned_processes = {10: "codevm.exe"}
        session.engine_pids = {10}
        session._require_engine_started()

    def test_the_engine_check_can_be_disabled(self):
        session = ComSession(require_engine=False)
        session._require_engine_started()


class CleanupIdentity(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.temp = workspace_temp_directory("cleanup")
        self.addCleanup(self.temp.cleanup)
        self.session = ComSession(starting_directory=self.temp.name)
        self.session.owned_processes = {987654321: "codevm.exe"}
        self.session.process_identities = {
            987654321: {"name": "codevm.exe", "created_at": 100.0}}
        self.session._record_session()

    def test_kill_refusal_retains_record_and_reports_failure(self):
        from codev_mcp import com_session
        with (self.mock.patch.object(self.session, "_verify_shutdown", return_value=[987654321]),
              self.mock.patch.object(com_session, "_identity", return_value={
                  "name": "codevm.exe", "created_at": 100.0}),
              self.mock.patch.object(com_session, "terminate_processes", return_value=[987654321])):
            self.assertFalse(self.session.stop())
        self.assertEqual(self.session.cleanup_remaining, [987654321])
        self.assertTrue(self.session.session_file.exists())

    def test_reused_pid_is_never_killed(self):
        from codev_mcp import com_session
        with (self.mock.patch.object(self.session, "_verify_shutdown", return_value=[987654321]),
              self.mock.patch.object(com_session, "_identity", return_value={
                  "name": "codevm.exe", "created_at": 200.0}),
              self.mock.patch.object(com_session, "terminate_processes") as kill):
            self.assertTrue(self.session.stop())
        kill.assert_not_called()
        self.assertFalse(self.session.session_file.exists())

    def test_unreadable_identity_keeps_ownership_evidence(self):
        from codev_mcp import com_session
        with (self.mock.patch.object(self.session, "_verify_shutdown", return_value=[987654321]),
              self.mock.patch.object(com_session, "_identity", side_effect=PermissionError),
              self.mock.patch.object(com_session, "terminate_processes") as kill):
            self.assertFalse(self.session.stop())
        kill.assert_not_called()
        self.assertTrue(self.session.session_file.exists())

    def test_already_exited_process_is_confirmed_without_kill(self):
        from codev_mcp import com_session
        with (self.mock.patch.object(self.session, "_verify_shutdown", return_value=[]),
              self.mock.patch.object(com_session, "_identity", return_value=None),
              self.mock.patch.object(com_session, "terminate_processes") as kill):
            self.assertTrue(self.session.stop())
        kill.assert_not_called()
        self.assertFalse(self.session.session_file.exists())

    def test_unreadable_record_is_preserved_and_blocks_new_session(self):
        from codev_mcp.errors import SessionInvalidError
        self.session.session_file.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SessionInvalidError):
            self.session.cleanup_recorded_session()
        self.assertTrue(self.session.session_file.exists())

    def test_recorded_leftover_can_be_retried_after_verified_kill(self):
        from codev_mcp import com_session
        current = {"name": "codevm.exe", "created_at": 100.0}

        def kill(_pids):
            nonlocal current
            current = None
            return []

        with (self.mock.patch.object(com_session, "_identity", side_effect=lambda _pid: current),
              self.mock.patch.object(com_session, "terminate_processes", side_effect=kill)):
            self.assertEqual(self.session.cleanup_recorded_session(), [987654321])
        self.assertFalse(self.session.session_file.exists())

    def test_recorded_reused_pid_is_left_alone(self):
        from codev_mcp import com_session
        with (self.mock.patch.object(com_session, "_identity", return_value={
                "name": "codevm.exe", "created_at": 200.0}),
              self.mock.patch.object(com_session, "terminate_processes") as kill):
            self.assertEqual(self.session.cleanup_recorded_session(), [])
        kill.assert_not_called()
        self.assertFalse(self.session.session_file.exists())


class SharedComServer(unittest.TestCase):
    """cvcomsvr.exe is shared by the sessions that run at the same time; stopping it ends theirs too (F7)."""

    LIVE = {10: "cvcomsvr.exe", 11: "codevm.exe", 12: "cvcommand.exe", 21: "codevm.exe", 22: "cvcommand.exe"}

    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.temp = workspace_temp_directory("shared")
        self.addCleanup(self.temp.cleanup)

    def test_the_server_counts_as_shared_while_another_sessions_processes_are_alive(self):
        from codev_mcp import com_session

        with self.mock.patch.object(com_session, "list_codev_processes", return_value=dict(self.LIVE)):
            self.assertEqual(com_session.shared_server_pids([10, 11, 12]), {10})
            self.assertEqual(com_session.shared_server_pids([11, 12]), set())  # not recorded by this session
        alone = {pid: name for pid, name in self.LIVE.items() if pid < 20}
        with self.mock.patch.object(com_session, "list_codev_processes", return_value=alone):
            self.assertEqual(com_session.shared_server_pids([10, 11, 12]), set())  # nobody else needs it

    def test_stop_leaves_the_shared_server_alone_and_still_confirms_the_release(self):
        from codev_mcp import com_session

        session = ComSession(starting_directory=self.temp.name)
        session.owned_processes = {10: "cvcomsvr.exe", 11: "codevm.exe", 12: "cvcommand.exe"}
        killed: list[list[int]] = []
        live = dict(self.LIVE)

        def identity(pid):
            return {"name": live[pid], "created_at": 1.0} if pid in live else None

        def kill(pids):
            killed.append(list(pids))
            for pid in pids:
                live.pop(pid, None)
            return []

        session.process_identities = {pid: {"name": name, "created_at": 1.0} for pid, name in
                                      session.owned_processes.items()}
        with (self.mock.patch.object(com_session, "list_codev_processes", side_effect=lambda: dict(live)),
              self.mock.patch.object(com_session, "_identity", side_effect=identity),
              self.mock.patch.object(com_session, "terminate_processes", side_effect=kill),
              self.mock.patch.object(com_session.time, "time", side_effect=iter(range(0, 10000, 10))),
              self.mock.patch.object(com_session.time, "sleep")):
            live.pop(11)  # the engine went away with StopCodeV
            self.assertTrue(session.stop())
        self.assertEqual(killed, [[12]])  # its own command server only
        self.assertIn(10, live)
        self.assertEqual(session.shared_processes, [10])
        self.assertEqual(session.cleanup_remaining, [])

    def test_a_recorded_leftover_session_does_not_take_the_shared_server_down(self):
        from codev_mcp import com_session

        session = ComSession(starting_directory=self.temp.name)
        session.owned_processes = {10: "cvcomsvr.exe", 12: "cvcommand.exe"}
        session.process_identities = {10: {"name": "cvcomsvr.exe", "created_at": 1.0},
                                      12: {"name": "cvcommand.exe", "created_at": 1.0}}
        session._record_session()
        live = dict(self.LIVE)
        killed: list[list[int]] = []

        def kill(pids):
            killed.append(list(pids))
            for pid in pids:
                live.pop(pid, None)
            return []

        with (self.mock.patch.object(com_session, "list_codev_processes", side_effect=lambda: dict(live)),
              self.mock.patch.object(com_session, "_identity",
                                     side_effect=lambda pid: {"name": live[pid], "created_at": 1.0} if pid in live else None),
              self.mock.patch.object(com_session, "terminate_processes", side_effect=kill)):
            self.assertEqual(session.cleanup_recorded_session(), [12])
        self.assertEqual(killed, [[12]])
        self.assertIn(10, live)


class EngineLiveness(unittest.TestCase):
    """A crashed engine sits on a dialog with no threads left."""
    def test_a_threadless_engine_is_not_alive(self):
        from codev_mcp import com_session

        class Process:
            def __init__(self, threads: int):
                self._threads = threads

            def is_running(self):
                return True

            def status(self):
                return "running"

            def num_threads(self):
                return self._threads

        with unittest.mock.patch.object(
            com_session, "_psutil", return_value=unittest.mock.Mock(
                STATUS_ZOMBIE="zombie", Process=lambda pid: Process(0)
            )
        ):
            self.assertFalse(com_session.engine_is_alive(1234))
        with unittest.mock.patch.object(
            com_session, "_psutil", return_value=unittest.mock.Mock(
                STATUS_ZOMBIE="zombie", Process=lambda pid: Process(4)
            )
        ):
            self.assertTrue(com_session.engine_is_alive(1234))

    def test_a_gone_process_is_not_alive(self):
        from codev_mcp import com_session

        fake = unittest.mock.Mock(STATUS_ZOMBIE="zombie")
        fake.Process.side_effect = OSError("no such process")
        with unittest.mock.patch.object(com_session, "_psutil", return_value=fake):
            self.assertFalse(com_session.engine_is_alive(999999))


class CrashedEngineDetection(unittest.TestCase):
    def make_session(self) -> ComSession:
        session = ComSession()
        stub = StubObject()
        session._object = stub
        return session

    def test_a_threadless_engine_makes_calls_fail_immediately(self):
        from codev_mcp import com_session
        from codev_mcp.errors import SessionInvalidError

        session = self.make_session()
        session.engine_pids = {4242}
        original = com_session.engine_is_alive
        com_session.engine_is_alive = lambda pid: False
        try:
            with self.assertRaises(SessionInvalidError) as caught:
                session.evaluate("(NUM S)")
        finally:
            com_session.engine_is_alive = original
        self.assertIn("engine process has exited", caught.exception.message)
        self.assertTrue(session.engine_dead)

    def test_a_dead_flag_blocks_calls_without_a_process_scan(self):
        from codev_mcp import com_session
        from codev_mcp.errors import SessionInvalidError

        session = self.make_session()
        session.engine_pids = {4242}
        session.engine_dead = True
        original = com_session.engine_is_alive

        def fail(*args):
            raise AssertionError("the dead flag must short circuit the scan")

        com_session.engine_is_alive = fail
        try:
            with self.assertRaises(SessionInvalidError):
                session.evaluate("(NUM S)")
        finally:
            com_session.engine_is_alive = original


class StartupLock(unittest.TestCase):
    """Sessions start one at a time, so each one's process record holds only its own processes (F7)."""
    def test_a_second_start_waits_until_the_first_has_finished(self):
        import threading
        import time

        from codev_mcp import com_session

        events: list[str] = []
        first_in = threading.Event()

        def first():
            with com_session._StartupLock():
                events.append("first in")
                first_in.set()
                time.sleep(0.6)
                events.append("first out")

        thread = threading.Thread(target=first)
        thread.start()
        first_in.wait(5)
        with com_session._StartupLock():
            events.append("second in")
        thread.join()
        self.assertEqual(events, ["first in", "first out", "second in"])

    def test_the_lock_is_released_when_the_start_fails(self):
        from codev_mcp import com_session

        with self.assertRaises(RuntimeError):
            with com_session._StartupLock():
                raise RuntimeError("start failed")
        with com_session._StartupLock():
            pass  # would block for the full wait if the mutex had not been released

    def test_a_stuck_start_makes_the_next_one_fail_instead_of_waiting_forever(self):
        import threading

        from codev_mcp import com_session

        held, release = threading.Event(), threading.Event()

        def holder():
            with com_session._StartupLock():
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        try:
            with unittest.mock.patch.object(com_session, "STARTUP_LOCK_WAIT_SECONDS", 0.3):
                with self.assertRaises(ComputationError) as caught:
                    with com_session._StartupLock():
                        pass
            self.assertIn("still starting", caught.exception.message)
        finally:
            release.set()
            thread.join()

    def test_start_runs_the_engine_start_inside_the_lock(self):
        from codev_mcp import com_session

        order: list[str] = []

        class Recorder:
            def __init__(self, note=None):
                pass

            def __enter__(self):
                order.append("lock")
                return self

            def __exit__(self, *_exc):
                order.append("unlock")

        session = ComSession()
        with (unittest.mock.patch.object(com_session, "_StartupLock", Recorder),
              unittest.mock.patch.object(ComSession, "_start_engine",
                                         lambda *args: order.append("engine") or "10.2")):
            self.assertEqual(session.start(), "10.2")
        self.assertEqual(order, ["lock", "engine", "unlock"])


if __name__ == "__main__":
    unittest.main()
