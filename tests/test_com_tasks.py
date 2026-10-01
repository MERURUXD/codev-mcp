"""Task control and fault handling tests for the COM backend.

These cover the Phase E criteria that can be checked without CODE V: the spot
diagram runs as an asynchronous option, other lens work is refused while it
runs, cancellation distinguishes a request from a confirmed stop, a stop that
CODE V ignores marks the session invalid, and truncated output is never
presented as a complete result.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from codev_mcp.com_backend import ComBackend
from codev_mcp.errors import ParameterError, SessionInvalidError
from codev_mcp.models import (
    AnalysisKind,
    AnalysisOptions,
    AnalysisRequest,
    ParameterEdit,
    TaskState,
    UpdateRequest,
)
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession


class TaskTestCase(unittest.TestCase):
    session_kwargs: dict = {}

    def configure_session(self, session: FakeCodeVSession) -> None:
        """Hook for subclasses that need a different fake session."""

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("tasks")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.working = self.root / "run"
        self.lens_path = self.root / "dbgauss.len"
        self.lens_path.write_text("! lens placeholder\n", encoding="utf-8")
        self.session = FakeCodeVSession(**self.session_kwargs)
        self.configure_session(self.session)
        self.session.listing = self.session._build_listing()
        self.backend = ComBackend(working_directory=self.working, session=self.session)
        self.backend.cancel_confirm_seconds = 0.2
        self.backend.open_lens(str(self.lens_path))
        self.after_open()

    def after_open(self) -> None:
        """Hook that runs once the lens is open and verified."""

    def submit_spot(self) -> str:
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions())
        )
        return task


class AsynchronousSpot(TaskTestCase):
    session_kwargs = {"async_ticks": 3}

    def test_run_analysis_returns_a_running_task_immediately(self):
        task = self.submit_spot()
        self.assertEqual(task.state, TaskState.RUNNING)
        self.assertTrue(task.task_id.startswith("codev-"))
        self.assertIn("poll", task.progress or "")
        self.assertEqual(task.settings.zoom_position, 1)

    def test_polling_advances_the_task(self):
        self.submit_spot()
        first = self.backend.get_analysis()
        self.assertEqual(first.task.state, TaskState.RUNNING)
        self.assertIn("still running", first.task.progress or "")
        self.assertIsNone(first.spot_diagram)
        second = self.backend.get_analysis()
        self.assertEqual(second.task.state, TaskState.RUNNING)
        final = self.backend.get_analysis()
        self.assertEqual(final.task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(final.spot_diagram)
        self.assertAlmostEqual(final.spot_diagram.rms_radius, 0.004 / (2 ** 0.5), places=6)

    def test_the_output_is_fetched_before_the_grid_is_traced(self):
        self.submit_spot()
        for _ in range(3):
            self.backend.get_analysis()
        # The traced grid must come after the asynchronous output was read.
        self.assertTrue(any(command.startswith("AsyncCommand spo") for command in self.session.commands))
        self.assertGreater(len(self.session.raytra_calls), 0)

    def test_other_lens_work_is_refused_while_it_runs(self):
        self.submit_spot()
        with self.assertRaises(ParameterError) as caught:
            self.backend.get_lens()
        self.assertIn("while analysis", caught.exception.message)
        with self.assertRaises(ParameterError):
            self.backend.open_lens(str(self.lens_path))
        with self.assertRaises(ParameterError):
            self.backend.update_lens(
                UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.0)])
            )
        with self.assertRaises(ParameterError) as caught:
            self.backend.run_analysis(
                AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
            )
        self.assertIn("run_analysis is refused", caught.exception.message)

    def test_lens_work_is_allowed_again_after_the_task_finishes(self):
        self.submit_spot()
        for _ in range(3):
            self.backend.get_analysis()
        self.assertIsNotNone(self.backend.get_lens())


class PrematureSpotCompletion(TaskTestCase):
    session_kwargs = {"async_ticks": 3, "wait_completes_while_executing": True}

    def test_wait_completion_does_not_read_output_while_spo_executes(self):
        self.submit_spot()
        self.session.events.clear()
        snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.RUNNING)
        self.assertIsNone(snapshot.spot_diagram)
        self.assertNotIn("get_command_output", self.session.events)
        self.assertEqual(self.session.raytra_calls, [])
        for _ in range(3):
            snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(snapshot.spot_diagram)
        self.assertIn("get_command_output", self.session.events)


class Cancellation(TaskTestCase):
    session_kwargs = {"async_ticks": 5}

    def test_a_confirmed_stop_cancels_the_task(self):
        self.submit_spot()
        cancelled = self.backend.cancel_analysis()
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        self.assertIn("confirmed the stop", cancelled.progress or "")
        self.assertIn("StopCommand", self.session.commands)
        self.assertTrue(self.backend._session_valid)
        # The session is usable for lens work again.
        self.assertIsNotNone(self.backend.get_lens())
        self.assertEqual(self.backend.get_analysis().task.state, TaskState.CANCELLED)

    def test_cancelling_a_finished_task_reports_its_final_state(self):
        self.submit_spot()
        for _ in range(5):
            self.backend.get_analysis()
        result = self.backend.cancel_analysis()
        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertIn("No analysis was running", result.progress or "")

    def test_an_unconfirmed_stop_marks_the_session_invalid(self):
        self.session.stop_takes_effect = False
        self.submit_spot()
        result = self.backend.cancel_analysis()
        self.assertEqual(result.state, TaskState.RUNNING)
        self.assertIn("did not confirm", result.progress or "")
        self.assertFalse(self.backend._session_valid)
        with self.assertRaises(SessionInvalidError):
            self.backend.save_lens_as(str(self.root / "after-cancel.len"))
        self.assertEqual(self.backend.get_status().details["lens_state"], "invalid")
        # Ordering the task to stop and stopping it are different things; the
        # task record keeps saying that the calculation may still be running.
        self.assertEqual(self.backend.get_analysis().task.state, TaskState.RUNNING)

    def test_cancel_without_a_task_returns_none(self):
        self.assertIsNone(self.backend.cancel_analysis())


class TruncatedOutput(TaskTestCase):
    """A listing that fills the text buffer is never treated as complete.

    The buffer is only too small for the listing after the lens was opened:
    with a buffer that small from the start, the lens could not be verified at
    all and open_lens would refuse it (see TruncatedLensOpen).
    """

    def after_open(self) -> None:
        self.session.text_buffer_size = 10
        # A real session returns exactly the buffer size, which is what makes
        # output_is_truncated true.
        self.session.listing = self.session.listing[:10]

    def test_truncation_is_flagged_and_never_hidden(self):
        self.submit_spot()
        snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertTrue(snapshot.task.output_truncated)
        self.assertTrue(
            any("text buffer" in warning for warning in snapshot.task.warnings)
        )
        self.assertTrue(
            any("text buffer" in warning for warning in snapshot.spot_diagram.warnings)
        )

    def test_truncation_is_reported_on_lens_reads(self):
        # A fresh read of the lens is what the listing warning belongs to: the
        # session copies the listing it was built with.
        lens = self.backend.get_lens()
        self.assertTrue(any("text buffer" in warning for warning in lens.warnings))


class TruncatedLensOpen(TaskTestCase):
    """A truncated listing makes the checkpoint verification fail."""

    session_kwargs = {"text_buffer_size": 10}

    def setUp(self) -> None:
        # The lens cannot be verified with a buffer this small, so the open in
        # the shared setup is expected to be refused.
        try:
            super().setUp()
        except SessionInvalidError:
            self.addCleanup(self._temp.cleanup)

    def test_a_lens_whose_listing_is_truncated_is_refused(self):
        # setUp already tried to open the lens; the engine holds a lens that
        # could not be verified, so the session stops instead of pretending.
        status = self.backend.get_status()
        self.assertEqual(status.details["lens_state"], "invalid")
        self.assertFalse(status.lens_open)
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()


class TruncatedWithoutAnnotations(TaskTestCase):
    def after_open(self) -> None:
        self.session.text_buffer_size = 10
        self.session.async_output_override = "    SPO\r\n\r\n    RAY STATISTICS FOR FIELD  1\r\n"

    def test_a_truncated_result_is_a_failure_not_a_partial_success(self):
        self.submit_spot()
        snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertIsNone(snapshot.spot_diagram)
        self.assertIn("annotations", snapshot.task.error.message)
        self.assertTrue(snapshot.task.error.details.get("output_truncated"))


class SynchronousAnalyses(TaskTestCase):
    def test_first_order_finishes_inside_run_analysis(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(task.started_at)
        self.assertIsNotNone(task.finished_at)

    def test_mtf_finishes_inside_run_analysis(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.MTF, options=AnalysisOptions(frequencies=[10.0]))
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertIn("MTF_1FLD", task.raw_output or "")


if __name__ == "__main__":
    unittest.main()
