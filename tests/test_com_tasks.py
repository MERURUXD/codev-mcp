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
from codev_mcp.errors import ComputationError, ParameterError, SessionInvalidError
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

class PollFailures(TaskTestCase):
    """A failed poll ends the task and stops SPO before lens work resumes (review M4, M5)."""
    session_kwargs = {"async_ticks": 3}

    def test_a_parser_error_fails_the_task_and_frees_lens_work(self):
        import unittest.mock

        self.submit_spot()
        with unittest.mock.patch.object(self.backend, "_finish_spot", side_effect=ZeroDivisionError("empty grid")):
            for _ in range(5):
                snapshot = self.backend.get_analysis()
                if snapshot.task.state is not TaskState.RUNNING:
                    break
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "internal")
        self.assertIn("ZeroDivisionError", snapshot.task.error.message)
        self.assertNotIn("StopCommand", self.session.commands)  # SPO had already finished
        self.assertIsNotNone(self.backend.get_lens())

    def test_a_failed_poll_stops_the_spo_that_may_still_run(self):
        import unittest.mock

        from codev_mcp.errors import ComputationError

        self.submit_spot()
        with unittest.mock.patch.object(self.session, "wait", side_effect=ComputationError("Wait failed")):
            snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertIn("StopCommand", self.session.commands)
        self.assertTrue(self.backend._session_valid)
        self.assertIsNotNone(self.backend.get_lens())

    def test_an_unconfirmed_stop_after_a_failed_poll_invalidates_the_session(self):
        import unittest.mock

        from codev_mcp.errors import ComputationError

        self.session.stop_takes_effect = False
        self.submit_spot()
        with unittest.mock.patch.object(self.session, "wait", side_effect=ComputationError("Wait failed")):
            snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertFalse(self.backend._session_valid)
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()

class SpotFieldMatching(TaskTestCase):
    """SPO statistics are taken from the requested field's own block (review M3)."""

    def run_spot(self, field: int):
        self.backend.run_analysis(AnalysisRequest(
            kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions(field_numbers=[field])))
        for _ in range(5):
            snapshot = self.backend.get_analysis()
            if snapshot.task.state is not TaskState.RUNNING:
                return snapshot
        self.fail("the spot task did not finish")

    def test_the_requested_field_is_matched_by_its_number(self):
        snapshot = self.run_spot(2)
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertAlmostEqual(snapshot.spot_diagram.centroid_y, 5.3617e-04, places=8)

    def test_a_field_missing_from_the_output_fails_instead_of_borrowing_another(self):
        original = self.session._spot_statistics_listing
        self.session._spot_statistics_listing = lambda: original().split("       Field  2,")[0]
        snapshot = self.run_spot(2)
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")
        self.assertEqual(snapshot.task.error.details["annotated_fields"], [1])
        self.assertIsNone(snapshot.spot_diagram)

class AnalysisOptionBounds(unittest.TestCase):
    """Analysis settings are bounded instead of silently ignored or unbounded (review M10)."""

    def test_ray_grid_and_frequencies_are_bounded(self):
        from pydantic import ValidationError

        for options in ({"ray_grid": 0}, {"ray_grid": -3}, {"ray_grid": 2000},
                        {"frequencies": [10.0, float("nan")]}, {"frequencies": [-5.0]},
                        {"frequencies": [float("inf")]}, {"frequencies": [1.0] * 102},
                        {"azimuth": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                AnalysisOptions.model_validate(options)
        AnalysisOptions.model_validate({"ray_grid": 101, "frequencies": [0.0] + [1.0] * 100})

    def test_misspelled_keys_are_refused_not_ignored(self):
        """field_number instead of field_numbers silently meant every field (review L11)."""
        from pydantic import ValidationError

        for model, payload in ((AnalysisOptions, {"field_number": [2]}),
                               (AnalysisRequest, {"kind": "mtf", "option": {}}),
                               (UpdateRequest, {"edit": []}),
                               (ParameterEdit, {"surface": 1, "parameter": "thickness", "value": 1, "zoom": 1})):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

    def test_mtf_refuses_an_azimuth_it_would_not_use(self):
        from codev_mcp.backend import check_mtf_azimuth

        check_mtf_azimuth(None)
        check_mtf_azimuth(0.0)  # the comparison CLIs pass 0
        with self.assertRaises(ParameterError):
            check_mtf_azimuth(45.0)


class MtfNonNumeric(TaskTestCase):
    def test_a_non_numeric_modulation_fails_instead_of_entering_the_result(self):
        """A non-numeric COM answer became NaN, and NaN < 0 is false (review L10)."""
        import unittest.mock

        from codev_mcp.errors import ComputationError

        with unittest.mock.patch.object(self.session, "mtf_1fld", return_value=(float("nan"), [])):
            task = self.backend.run_analysis(AnalysisRequest(
                kind=AnalysisKind.MTF, options=AnalysisOptions(frequencies=[10.0])))
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, ComputationError("x").kind.value)
        self.assertIsNone(self.backend.get_analysis().mtf)


class MtfAzimuth(TaskTestCase):
    def test_an_mtf_with_another_azimuth_is_refused_before_it_runs(self):
        with self.assertRaises(ParameterError) as caught:
            self.backend.run_analysis(AnalysisRequest(
                kind=AnalysisKind.MTF, options=AnalysisOptions(frequencies=[10.0], azimuth=45.0)))
        self.assertIn("sagittal", caught.exception.message)

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
        except ComputationError:
            self.addCleanup(self._temp.cleanup)

    def test_a_lens_whose_listing_is_truncated_is_refused(self):
        # setUp already tried to open the lens; the engine holds a lens that
        # could not be verified, so that session is dropped instead of trusted.
        # Nothing was open before, so the service is empty again (review M8).
        status = self.backend.get_status()
        self.assertEqual(status.details["lens_state"], "empty")
        self.assertFalse(status.lens_open)
        self.assertIsNone(self.backend._session)
        self.assertTrue(self.backend._session_valid)


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
