"""Tests for the Phase H native plot export, driven by the fake session.

CODE V draws these plots itself and the service only chooses the option, the
output file and the checks on the result, so these tests pin down exactly that:
the five allowed plot types and their fixed commands, the order in which the
option output is read and the graphics file is closed and converted, and the
files that have to exist and be valid before anything is reported as a success.
"""

from __future__ import annotations

import base64
import struct
import unittest
import unittest.mock
import zlib
from pathlib import Path

from codev_mcp.com_backend import NATIVE_PLOT_COMMANDS, ComBackend
from codev_mcp.errors import (
    CodeVError,
    ComputationError,
    ParameterError,
    SessionInvalidError,
    UnsupportedError,
)
from codev_mcp.models import (
    AnalysisKind,
    AnalysisOptions,
    AnalysisRequest,
    MtfType,
    NativePlotType,
    TaskState,
)
from codev_mcp import plotting
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession, corrupt_png_idat

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

EXPECTED_COMMANDS = {
    NativePlotType.LAYOUT: "vie;lab no;go",
    NativePlotType.SPOT: "spo;air yes;go",
    NativePlotType.MTF: "mtf;mfr 100;ifr 10;go",
    NativePlotType.RAY_ABERRATION: "rim;go",
    NativePlotType.FIELD_ABERRATION: "fie;lsa;go",
}


class NativePlotTestCase(unittest.TestCase):
    session_kwargs: dict = {}

    def configure_session(self, session: FakeCodeVSession) -> None:
        """Hook for subclasses that need a different fake session."""

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("native-plot")
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

    def submit(self, plot_type=NativePlotType.LAYOUT, **options):
        return self.backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.NATIVE_PLOT,
                options=AnalysisOptions(plot_type=plot_type, **options),
            )
        )

    def submit_and_finish(self, plot_type=NativePlotType.LAYOUT, **options):
        self.submit(plot_type, **options)
        snapshot = self.backend.get_analysis()
        for _ in range(10):
            if snapshot.task.state is not TaskState.RUNNING:
                break
            snapshot = self.backend.get_analysis()
        return snapshot

    def async_commands(self) -> list[str]:
        return [
            command[len("AsyncCommand ") :]
            for command in self.session.commands
            if command.startswith("AsyncCommand ")
        ]

    def event_index(self, event: str) -> int:
        return self.session.events.index(event)


class PlotTypeMapping(NativePlotTestCase):
    def test_every_plot_type_maps_to_the_plan_command(self):
        self.assertEqual(NATIVE_PLOT_COMMANDS, EXPECTED_COMMANDS)

    def test_each_plot_type_sends_exactly_its_own_command(self):
        for plot_type, command in EXPECTED_COMMANDS.items():
            with self.subTest(plot_type=plot_type.value):
                self.session.commands.clear()
                snapshot = self.submit_and_finish(plot_type)
                self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
                self.assertEqual(self.async_commands(), [command])
                self.assertEqual(snapshot.native_plot.plot_type, plot_type)

    def test_the_caller_cannot_inject_a_command(self):
        # The plot type is an enum, so a command fragment never parses.
        with self.assertRaises(ValueError):
            AnalysisOptions(plot_type="vie;go;del all")
        with self.assertRaises(ValueError):
            AnalysisOptions(plot_type="layout; go")


class Export(NativePlotTestCase):
    def test_all_plots_release_graphics_before_wavefront_and_numeric_mtf(self):
        before = self.backend.get_lens().model_dump(mode="json")
        paths = []
        for plot_type in NativePlotType:
            snapshot = self.submit_and_finish(plot_type)
            self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED, snapshot.task.error)
            paths.append(snapshot.native_plot.plot_file_path)
            self.assertIsNone(self.backend._pending_native_plot)
        self.assertEqual(len(set(paths)), 5)
        self.assertEqual(self.session.commands.count("gra t"), 5)
        task = self.backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        self.assertEqual(self.backend.get_analysis().wavefront.nrd, 20)
        task = self.backend.run_analysis(AnalysisRequest(
            kind=AnalysisKind.MTF, options=AnalysisOptions(frequencies=[0, 20, 40])))
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        self.assertEqual(self.backend.get_analysis().mtf.frequencies, [0, 20, 40])
        self.assertEqual(self.backend.get_lens().model_dump(mode="json"), before)

    def test_exports_a_valid_png_next_to_the_plot_file(self):
        snapshot = self.submit_and_finish(NativePlotType.SPOT)
        result = snapshot.native_plot
        self.assertIsNotNone(result)
        self.assertEqual(result.plot_type, NativePlotType.SPOT)
        self.assertEqual(result.zoom_position, 1)
        self.assertIsNotNone(result.image)
        png = Path(result.image.path)
        self.assertTrue(png.exists())
        self.assertEqual(png.suffix.lower(), ".png")
        self.assertEqual(result.image.width, 48)
        self.assertEqual(result.image.height, 32)
        data = png.read_bytes()
        self.assertTrue(data.startswith(PNG_SIGNATURE))
        self.assertEqual(base64.b64decode(result.image.base64_data), data)

    def test_keeps_the_neutral_plot_file_and_reports_its_size(self):
        snapshot = self.submit_and_finish()
        result = snapshot.native_plot
        plot_file = Path(result.plot_file_path)
        self.assertTrue(plot_file.exists())
        self.assertEqual(plot_file.suffix, ".PLT")
        self.assertGreater(result.plot_file_bytes, 0)
        self.assertEqual(plot_file.stat().st_size, result.plot_file_bytes)
        # The plot file belongs to the result directory, not to the lens folder.
        self.assertEqual(plot_file.parent, self.working / "results")

    def test_every_export_uses_a_fresh_file_name(self):
        first = self.submit_and_finish().native_plot
        second = self.submit_and_finish().native_plot
        self.assertNotEqual(first.plot_file_path, second.plot_file_path)
        self.assertNotEqual(first.image.path, second.image.path)
        self.assertTrue(Path(first.plot_file_path).exists())
        self.assertTrue(Path(second.plot_file_path).exists())
        # Both runs drew the same plot type, so the unique part cannot be the
        # task id alone: an earlier run must never be overwritten.
        self.assertIn("layout", Path(first.plot_file_path).name)

    def test_the_result_says_code_v_drew_it(self):
        result = self.submit_and_finish().native_plot
        self.assertEqual(result.source.value, "codev")
        self.assertIn("CODE V drew this plot itself", result.warnings[0])
        self.assertIn("vie;lab no;go", result.warnings[0])
        self.assertIn("Command End", result.raw_output or "")

    def test_the_settings_echo_the_plot_type(self):
        task = self.submit(NativePlotType.RAY_ABERRATION)
        self.assertEqual(task.settings.plot_type, NativePlotType.RAY_ABERRATION)
        self.assertIn("CODE V drew this plot", " ".join(task.settings.notes))


class CallOrder(NativePlotTestCase):
    def test_the_plot_name_stays_inside_the_code_v_filespec_limit(self):
        # CODE V truncates a longer filespec, and a truncated plot file can no
        # longer be converted, so the whole path has to stay inside the limit.
        snapshot = self.submit_and_finish()
        plot_file = snapshot.native_plot.plot_file_path
        self.assertLessEqual(len(plot_file), 80, plot_file)
        self.assertTrue(plot_file.endswith(".PLT"), plot_file)
        self.assertTrue(snapshot.native_plot.plot_file_bytes > 0)

    def test_output_is_read_before_the_graphics_file_is_closed_and_converted(self):
        snapshot = self.submit_and_finish()
        result = snapshot.native_plot
        output = self.event_index("get_command_output")
        release = self.event_index("command gra t")
        convert = self.event_index(f"command gcv png {result.plot_file_path}")
        self.assertLess(output, release)
        self.assertLess(release, convert)

    def test_graphics_are_directed_to_the_plot_file_before_the_option_starts(self):
        snapshot = self.submit_and_finish()
        directed = self.event_index(f"command gra {snapshot.native_plot.plot_file_path}")
        started = self.event_index("async_command vie;lab no;go")
        self.assertLess(directed, started)

    def test_no_command_runs_between_the_option_output_and_the_release(self):
        self.submit_and_finish()
        # Everything the backend does after the option finished: the release and
        # the conversion, and nothing else that could touch the output buffer.
        after_option = self.session.events[
            self.event_index("async_command vie;lab no;go") :
        ]
        commands = [event for event in after_option if event.startswith("command ")]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], "command gra t")


class WaitIsNotEnough(NativePlotTestCase):
    """Wait() reporting completion must never be trusted on its own.

    The real engine was observed returning "completed" from Wait while the
    option was still executing. Reading the output or touching the command line
    at that moment destroys the result, so the task has to keep polling.
    """

    session_kwargs = {"async_ticks": 3, "wait_completes_while_executing": True}

    def test_a_premature_completion_keeps_the_task_running(self):
        self.submit()
        first = self.backend.get_analysis()
        self.assertEqual(first.task.state, TaskState.RUNNING)
        self.assertIsNone(first.native_plot)
        self.assertNotIn("command gra t", self.session.events)
        self.assertFalse(any(event.startswith("command gcv") for event in self.session.events))
        self.assertNotIn("get_command_output", self.session.events)

    def test_the_task_finishes_once_the_option_really_stopped(self):
        self.submit()
        snapshot = self.backend.get_analysis()
        for _ in range(10):
            if snapshot.task.state is not TaskState.RUNNING:
                break
            snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(snapshot.native_plot.image)


class ParameterRules(NativePlotTestCase):
    def test_a_missing_plot_type_is_refused(self):
        with self.assertRaises(ParameterError) as caught:
            self.backend.run_analysis(
                AnalysisRequest(kind=AnalysisKind.NATIVE_PLOT, options=AnalysisOptions())
            )
        self.assertIn("plot_type", caught.exception.message)

    def test_selection_settings_are_refused_not_ignored(self):
        for name, value in (
            ("field_numbers", [1]),
            ("wavelength_numbers", [1]),
            ("ray_grid", 5),
            ("frequencies", [10.0]),
            ("azimuth", 45.0),
        ):
            with self.subTest(setting=name):
                with self.assertRaises(ParameterError) as caught:
                    self.submit(NativePlotType.SPOT, **{name: value})
                self.assertIn(name, caught.exception.details["not_accepted"])

    def test_an_explicit_mtf_type_is_refused(self):
        for mtf_type in (MtfType.DIFFRACTION, MtfType.GEOMETRIC):
            with self.subTest(mtf_type=mtf_type.value):
                with self.assertRaises(ParameterError) as caught:
                    self.submit(NativePlotType.SPOT, mtf_type=mtf_type)
                self.assertIn("mtf_type", caught.exception.details["not_accepted"])

    def test_an_unset_mtf_type_is_not_a_rejection(self):
        # The default must stay usable: only a setting the caller really passed
        # is refused, which is what model_fields_set records.
        snapshot = self.submit_and_finish(NativePlotType.SPOT)
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertIsNone(snapshot.task.settings.mtf_type)

    def test_plot_type_outside_native_plot_is_refused(self):
        with self.assertRaises(ParameterError) as caught:
            self.backend.run_analysis(
                AnalysisRequest(
                    kind=AnalysisKind.FIRST_ORDER,
                    options=AnalysisOptions(plot_type=NativePlotType.LAYOUT),
                )
            )
        self.assertIn("native_plot", caught.exception.message)


class MultiZoom(NativePlotTestCase):
    session_kwargs = {"zoom_positions": 2}

    def test_a_multi_zoom_lens_is_unsupported(self):
        with self.assertRaises(UnsupportedError) as caught:
            self.submit(NativePlotType.LAYOUT)
        self.assertEqual(caught.exception.details["zoom_positions"], 2)


class Faults(NativePlotTestCase):
    def test_a_plot_file_without_the_extender_is_still_found(self):
        # The real machine wrote an absolute filespec exactly as it was given,
        # so a session that drops the .PLT extender must not look like a
        # missing plot.
        self.session.plot_file_drops_extender = True
        snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        result = snapshot.native_plot
        self.assertIsNotNone(result)
        plot_file = Path(result.plot_file_path)
        self.assertTrue(plot_file.exists())
        # The extender CODE V left out is restored, because the conversion
        # command resolves the name with .PLT and could not find it otherwise.
        self.assertEqual(plot_file.suffix, ".PLT")
        self.assertGreater(result.plot_file_bytes, 0)
        self.assertTrue(Path(result.image.path).exists())

    def test_a_numbered_plot_file_is_still_found(self):
        # A plot file name that was reused inside one session came back as
        # <name>.1.PLT; that must not be reported as a missing plot either.
        self.session.plot_file_numbered = True
        snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        result = snapshot.native_plot
        self.assertTrue(result.plot_file_path.endswith(".1.PLT"))
        self.assertTrue(Path(result.image.path).exists())

    def test_a_result_directory_without_room_is_refused(self):
        deep = self.working / ("x" * 60)
        backend = ComBackend(working_directory=deep, session=self.session)
        backend.open_lens(str(self.lens_path))
        task = backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.NATIVE_PLOT,
                options=AnalysisOptions(plot_type=NativePlotType.LAYOUT),
            )
        )
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "computation_failed")
        self.assertIn("room", task.error.message)

    def failure(self, plot_type=NativePlotType.LAYOUT):
        snapshot = self.submit_and_finish(plot_type)
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertIsNone(snapshot.native_plot)
        return snapshot.task.error

    def test_a_command_error_is_a_computation_failure(self):
        self.session.plot_command_error = True
        error = self.failure()
        self.assertEqual(error.kind.value, "computation_failed")
        self.assertIn("error", error.message)

    def test_an_empty_command_output_is_a_computation_failure(self):
        self.session.plot_empty_output = True
        error = self.failure()
        self.assertEqual(error.kind.value, "computation_failed")
        self.assertIn("no output", error.message)

    def test_a_missing_plot_file_is_a_computation_failure(self):
        self.session.plot_file_missing = True
        error = self.failure()
        self.assertEqual(error.kind.value, "computation_failed")
        self.assertIn("no neutral plot file", error.message)

    def test_an_empty_plot_file_is_a_computation_failure(self):
        self.session.plot_file_empty = True
        error = self.failure()
        self.assertEqual(error.kind.value, "computation_failed")
        self.assertIn("empty", error.message)

    def test_a_failed_conversion_is_a_computation_failure(self):
        self.session.gcv_fails = True
        error = self.failure()
        self.assertEqual(error.kind.value, "computation_failed")
        self.assertIn("could not be converted", error.message)

    def test_a_corrupt_conversion_is_a_computation_failure(self):
        self.session.gcv_corrupt_png = True
        error = self.failure()
        self.assertEqual(error.kind.value, "computation_failed")
        self.assertIn("not a PNG", error.message)

    def test_a_failed_graphics_reset_invalidates_the_session(self):
        self.session.gra_reset_fails = True
        snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "session_invalid")
        self.assertFalse(self.backend._session_valid)
        self.assertEqual(self.backend.get_status().details["lens_state"], "invalid")
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()

    def test_a_failed_run_leaves_no_pending_plot_behind(self):
        self.session.gcv_fails = True
        self.failure()
        self.assertIsNone(self.backend._pending_native_plot)
        # The session is still usable for another analysis.
        self.assertIsNotNone(self.backend.get_lens())

    def test_a_command_error_returns_the_graphics_output(self):
        # The plot file GRA opened is closed even when the option failed, or
        # every later plot would be appended to it.
        self.session.plot_command_error = True
        self.failure()
        self.assertIn("command gra t", self.session.events)
        self.assertFalse(self.session._graphics_open)
        self.assertTrue(self.backend._session_valid)

    def test_an_empty_output_returns_the_graphics_output(self):
        self.session.plot_empty_output = True
        self.failure()
        self.assertIn("command gra t", self.session.events)
        self.assertFalse(self.session._graphics_open)
        self.assertTrue(self.backend._session_valid)

    def test_a_submission_failure_returns_the_graphics_output(self):
        # GRA succeeded, so the plot file exists; a refused asynchronous command
        # must not leave the graphics destination pointed at it.
        self.session.async_command_fails = True
        task = self.submit()
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "computation_failed")
        self.assertIn("command gra t", self.session.events)
        self.assertFalse(self.session._graphics_open)
        self.assertIsNone(self.backend._pending_native_plot)

    def test_an_older_png_is_never_reported_as_the_new_one(self):
        results = self.backend.result_directory
        results.mkdir(parents=True, exist_ok=True)
        plot_file = results / "cafe1234-layout-codev.PLT"
        stale = plot_file.with_suffix(".png")
        stale.write_bytes(b"an older picture")
        with self.assertRaises(ComputationError) as caught:
            self.backend._locate_native_png(
                plot_file, {stale.name.lower()}, NativePlotType.LAYOUT
            )
        self.assertIn("no PNG file appeared", caught.exception.message)

    def test_a_plot_file_is_never_renamed_onto_an_existing_file(self):
        results = self.backend.result_directory
        results.mkdir(parents=True, exist_ok=True)
        bare = results / "cafe1234-layout-codev"
        bare.write_bytes(b"this plot")
        (results / "cafe1234-layout-codev.PLT").write_bytes(b"an older plot")
        with self.assertRaises(ComputationError) as caught:
            self.backend._canonical_plot_file(bare)
        self.assertIn("already exists", caught.exception.message)


class Cancellation(NativePlotTestCase):
    session_kwargs = {"async_ticks": 5}

    def test_a_confirmed_stop_closes_the_plot_file(self):
        self.submit(NativePlotType.MTF)
        result = self.backend.cancel_analysis()
        self.assertEqual(result.state, TaskState.CANCELLED)
        self.assertIsNone(self.backend._pending_native_plot)
        self.assertLess(
            self.event_index("stop_command"), self.event_index("command gra t")
        )
        self.assertTrue(self.backend._session_valid)
        self.assertIsNotNone(self.backend.get_lens())

    def test_an_unconfirmed_stop_invalidates_the_session(self):
        self.session.stop_takes_effect = False
        self.submit(NativePlotType.MTF)
        result = self.backend.cancel_analysis()
        self.assertEqual(result.state, TaskState.RUNNING)
        self.assertFalse(self.backend._session_valid)

    def test_cancelling_does_not_convert_the_aborted_plot(self):
        self.submit()
        self.backend.cancel_analysis()
        self.assertFalse(any(event.startswith("command gcv") for event in self.session.events))


class OtherAnalysesAreUnchanged(NativePlotTestCase):
    def test_first_order_still_finishes_inside_run_analysis(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertIsNone(task.settings.plot_type)

    def test_a_native_plot_does_not_disturb_the_spot_payload(self):
        self.submit_and_finish()
        self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions())
        )
        for _ in range(5):
            snapshot = self.backend.get_analysis()
            if snapshot.task.state is not TaskState.RUNNING:
                break
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(snapshot.spot_diagram)
        self.assertIsNone(snapshot.native_plot)

    def test_a_native_plot_without_a_session_is_refused(self):
        self.backend.close_session()
        with self.assertRaises(CodeVError):
            self.submit()

    def test_the_drawing_failure_is_not_reported_as_a_drawn_plot(self):
        self.session.plot_command_error = True
        snapshot = self.submit_and_finish()
        self.assertIsNone(snapshot.native_plot)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")


class PollFailures(NativePlotTestCase):
    """A failed poll must not lose the graphics state.

    Wait, IsExecutingCommand and GetCommandOutput can all fail while the option
    is still running and its plot file is still open. The option is stopped and
    the graphics destination is put back before the task is failed; anything
    that cannot be confirmed stops the session instead.
    """

    session_kwargs = {"async_ticks": 5}

    def test_a_poll_failure_stops_the_option_and_returns_the_graphics_output(self):
        self.submit()
        failure = ComputationError("the session stopped answering")
        with unittest.mock.patch.object(self.session, "wait", side_effect=failure):
            snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertIsNone(snapshot.native_plot)
        self.assertIsNone(self.backend._pending_native_plot)
        # The option that was still running is stopped, not abandoned.
        self.assertFalse(self.session._async_running)
        self.assertIn("stop_command", self.session.events)
        self.assertIn("command gra t", self.session.events)
        self.assertFalse(self.session._graphics_open)
        # A stop and a reset that both worked leave a usable session.
        self.assertTrue(self.backend._session_valid)
        self.assertIsNotNone(self.backend.get_lens())

    def test_a_poll_failure_that_cannot_stop_the_option_invalidates_the_session(self):
        self.submit()
        with (
            unittest.mock.patch.object(
                self.session,
                "wait",
                side_effect=SessionInvalidError("the session stopped answering"),
            ),
            unittest.mock.patch.object(
                self.session,
                "stop_command",
                side_effect=SessionInvalidError("the stop was refused"),
            ),
        ):
            snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertIsNone(self.backend._pending_native_plot)
        self.assertFalse(self.backend._session_valid)
        self.assertEqual(self.backend.get_status().details["lens_state"], "invalid")
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()

    def test_a_failure_after_the_graphics_were_returned_releases_them_once(self):
        # The conversion fails after the plot file was already closed, so the
        # recovery path must not stop the session or reset the graphics again.
        self.session.gcv_fails = True
        self.submit_and_finish()
        self.assertEqual(self.session.events.count("command gra t"), 1)
        self.assertNotIn("stop_command", self.session.events)
        self.assertTrue(self.backend._session_valid)


class TruncatedPng(NativePlotTestCase):
    """A picture that was cut short is not an image."""

    def test_a_png_cut_short_is_refused(self):
        self.session.gcv_truncates_png_to = 24
        snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")
        self.assertIsNone(snapshot.native_plot)
        self.assertTrue(Path(snapshot.task.error.details["path"]).exists())

    def test_a_png_with_a_valid_header_but_a_broken_body_is_refused(self):
        canvas_png = plotting.Canvas(48, 32).to_png()
        cases = {
            "cut short": canvas_png[:24],
            "signature only": canvas_png[:8],
            "trailing data": canvas_png + b"junk",
            "text": b"<html>this is not a picture</html>",
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ComputationError):
                    ComBackend._validate_png(
                        data, Path("broken.png"), NativePlotType.LAYOUT
                    )

    def test_a_complete_png_is_accepted(self):
        data = plotting.Canvas(64, 40).to_png()
        self.assertEqual(
            ComBackend._validate_png(data, Path("ok.png"), NativePlotType.LAYOUT),
            (64, 40),
        )

    def test_a_png_whose_pixel_stream_is_corrupt_is_refused(self):
        # Valid chunks and matching checksums, but the compressed picture data
        # cannot be decoded: that must not escape as a raw zlib error.
        tampered = corrupt_png_idat(plotting.Canvas(48, 32).to_png())
        with self.assertRaises(ComputationError) as caught:
            ComBackend._validate_png(tampered, Path("corrupt.png"), NativePlotType.LAYOUT)
        self.assertIn("could not be decompressed", caught.exception.message)

    def test_a_corrupt_pixel_stream_fails_the_task_and_is_not_retried(self):
        self.session.gcv_corrupts_idat = True
        snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")
        self.assertIsNone(snapshot.native_plot)
        self.assertIsNone(self.backend._pending_native_plot)
        again = self.backend.get_analysis()
        self.assertEqual(again.task.state, TaskState.FAILED)
        self.assertEqual(len(self.session.gcv_calls), 1)

    def test_a_png_with_data_after_the_image_stream_is_refused(self):
        # A valid zlib stream followed by extra bytes inside the same IDAT: every
        # checksum still matches, so only the stream check can catch it.
        def extend_last_idat(data: bytes, extra: bytes) -> bytes:
            offset = len(b"\x89PNG\r\n\x1a\n")
            last: int | None = None
            while offset < len(data):
                length = struct.unpack(">I", data[offset : offset + 4])[0]
                if data[offset + 4 : offset + 8] == b"IDAT":
                    last = offset
                offset += 12 + length
            assert last is not None
            length = struct.unpack(">I", data[last : last + 4])[0]
            payload = data[last + 8 : last + 8 + length] + extra
            body = b"IDAT" + payload
            return (
                data[:last]
                + struct.pack(">I", len(payload))
                + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
                + data[last + 12 + length :]
            )

        canvas_png = plotting.Canvas(48, 32).to_png()
        tampered = extend_last_idat(canvas_png, b"extra picture data")
        # The tampered file is still a structurally valid PNG with correct CRCs.
        self.assertEqual(tampered[:8], canvas_png[:8])
        with self.assertRaises(ComputationError) as caught:
            ComBackend._validate_png(tampered, Path("extra.png"), NativePlotType.LAYOUT)
        self.assertIn("after the end of its image stream", caught.exception.message)


class TruncatedCommandOutput(NativePlotTestCase):
    def test_a_truncated_command_output_is_a_failure(self):
        # The buffer is only shrunk after the lens was read: with a buffer that
        # small from the start the lens could not be verified at all.
        self.session.text_buffer_size = 32
        self.session.async_output_override = "Drawing native plot " + "x" * 12
        snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")
        self.assertIn("text buffer", snapshot.task.error.message)
        self.assertTrue(snapshot.task.output_truncated)
        self.assertIsNone(snapshot.native_plot)
        # The plot file that was written is kept for diagnosis, and no
        # conversion is attempted from an unconfirmed drawing.
        plot_files = [
            path
            for path in self.backend.result_directory.glob("*")
            if path.suffix.lower() == ".plt"
        ]
        self.assertEqual(len(plot_files), 1)
        self.assertGreater(plot_files[0].stat().st_size, 0)
        self.assertEqual(self.session.gcv_calls, [])
        # The warning must not claim a verification that never happened: the
        # export stops before the plot file is located or converted.
        warnings = " ".join(snapshot.task.warnings)
        self.assertIn("kept for diagnosis", warnings)
        self.assertNotIn("checked independently", warnings)


class PngReadFailure(NativePlotTestCase):
    def test_a_result_directory_scan_that_fails_is_a_structured_failure(self):
        original = Path.glob

        def flaky(path: Path, pattern: str, **kwargs):
            if pattern == "*.png":
                raise PermissionError("the result directory is not readable")
            return original(path, pattern, **kwargs)

        with unittest.mock.patch.object(Path, "glob", flaky):
            snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")
        self.assertIn("read back", snapshot.task.error.message)
        self.assertIsNone(self.backend._pending_native_plot)

    def test_a_png_that_cannot_be_read_is_a_structured_final_failure(self):
        original = Path.read_bytes

        def flaky(path: Path) -> bytes:
            if path.suffix.lower() == ".png":
                raise PermissionError("the converted file is locked")
            return original(path)

        with unittest.mock.patch.object(Path, "read_bytes", flaky):
            snapshot = self.submit_and_finish()
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "computation_failed")
        self.assertIn("read back", snapshot.task.error.message)
        self.assertEqual(len(self.session.gcv_calls), 1)
        # The task is final, so a later poll must not convert the plot again.
        again = self.backend.get_analysis()
        self.assertEqual(again.task.state, TaskState.FAILED)
        self.assertEqual(len(self.session.gcv_calls), 1)


if __name__ == "__main__":
    unittest.main()
