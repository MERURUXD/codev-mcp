"""Nominal WAV fixture, parsing failures and backend contract."""

from __future__ import annotations

import unittest
from pathlib import Path

from codev_mcp.com_backend import ComBackend
from codev_mcp.errors import ParameterError, SessionInvalidError
from codev_mcp.listing import parse_nominal_wavefront
from codev_mcp.models import AnalysisKind, AnalysisOptions, AnalysisRequest, TaskState
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession

FIXTURE = Path(__file__).parent / "data" / "project-owned" / "wavefront.txt"


class WavefrontTests(unittest.TestCase):
    def test_real_listing_and_missing_data(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        parsed = parse_nominal_wavefront(raw, 3)
        self.assertEqual(parsed["rays"], [948, 936, 930])
        self.assertEqual(parsed["values"][0], (1.660776, 0.0))
        self.assertEqual(parsed["field_coordinates_deg"], [(0.0, 0.0), (0.0, 3.0), (0.0, 6.0)])
        self.assertEqual(parsed["weighted"], (2.305742, 0.0))
        for broken in (raw.replace("Command End:", ""),
                       raw.replace("WEIGHTED RMS", "WEIGHTED MIS"),
                       raw.replace("1.660776", "NaN"),
                       raw.replace("948     936     930", "948     936")):
            with self.subTest(broken=broken[-60:]):
                with self.assertRaises(ValueError):
                    parse_nominal_wavefront(broken, 3)

    def test_backend_uses_nominal_focus_and_keeps_lens(self):
        temp = workspace_temp_directory("wavefront")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "dbgauss.len"
        source.write_text("fake lens", encoding="utf-8")
        session = FakeCodeVSession()
        backend = ComBackend(working_directory=root / "run", session=session)
        before = backend.open_lens(str(source)).model_dump(mode="json")
        task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        self.assertIn("wav;nom yes;bes no;nrd 20;go", session.commands)
        result = backend.get_analysis().wavefront
        self.assertEqual([f.field_number for f in result.fields], [1, 2])
        self.assertIsNone(result.rms_equivalent_wavelength_nm)
        self.assertEqual(backend.get_lens().model_dump(mode="json"), before)
        with self.assertRaises(ParameterError):
            backend.run_analysis(AnalysisRequest(
                kind=AnalysisKind.WAVEFRONT, options=AnalysisOptions(field_numbers=[1])))

    def test_incomplete_native_output_fails_without_result(self):
        temp = workspace_temp_directory("wavefront-failure")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "dbgauss.len"
        source.write_text("fake lens", encoding="utf-8")
        session = FakeCodeVSession()
        session._wavefront_listing = lambda: "W A V E F R O N T   A N A L Y S I S\r\nCommand End:\r\n"
        backend = ComBackend(working_directory=root / "run", session=session)
        backend.open_lens(str(source))
        task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "computation_failed")
        self.assertIsNone(backend.get_analysis().wavefront)

    def test_truncated_output_is_not_reported_as_success(self):
        temp = workspace_temp_directory("wavefront-truncated")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "dbgauss.len"
        source.write_text("fake lens", encoding="utf-8")
        session = FakeCodeVSession()
        backend = ComBackend(working_directory=root / "run", session=session)
        backend.open_lens(str(source))
        original_truncated = session.output_is_truncated
        session.output_is_truncated = lambda text: (
            "W A V E F R O N T" in text or original_truncated(text)
        )
        task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertTrue(task.output_truncated)
        self.assertIsNone(backend.get_analysis().wavefront)

    def test_mismatched_field_coordinates_and_order_fail(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        for changed in (raw.replace("Y   0.71   10.00", "Y   0.71   99.00"),
                        raw.replace("X   0.00    0.00", "X   0.00    2.00", 1),
                        raw.replace("Y   0.71   10.00", "Y   1.00   14.00")
                           .replace("Y   1.00   14.00     1.563994", "Y   0.71   10.00     1.563994")):
            temp = workspace_temp_directory("wavefront-coordinates")
            self.addCleanup(temp.cleanup)
            root = Path(temp.name)
            source = root / "dbgauss.len"
            source.write_text("fake lens", encoding="utf-8")
            session = FakeCodeVSession(fields=[
                {"x": 0.0, "y": 0.0, "weight": 1.0},
                {"x": 0.0, "y": 10.0, "weight": 1.0},
                {"x": 0.0, "y": 14.0, "weight": 1.0},
            ], wavelengths=[
                {"nm": 656.3, "weight": 1.0},
                {"nm": 587.6, "weight": 1.0},
                {"nm": 486.1, "weight": 1.0},
            ])
            session._wavefront_listing = lambda output=changed: output
            backend = ComBackend(working_directory=root / "run", session=session)
            backend.open_lens(str(source))
            task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
            self.assertEqual(task.state, TaskState.FAILED)
            self.assertEqual(task.error.kind.value, "computation_failed")
            self.assertIsNone(backend.get_analysis().wavefront)

    def test_native_wav_edit_invalidates_session_and_blocks_save(self):
        temp = workspace_temp_directory("wavefront-edit")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "dbgauss.len"
        source.write_text("fake lens", encoding="utf-8")
        session = FakeCodeVSession()
        backend = ComBackend(working_directory=root / "run", session=session)
        backend.open_lens(str(source))
        original = session._wavefront_listing
        def mutating_wavefront():
            session.surfaces[2]["thickness"] = 5.25
            return original()
        session._wavefront_listing = mutating_wavefront
        task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "session_invalid")
        self.assertIn("WAV changed", task.error.message)
        self.assertIn("W A V E F R O N T", task.raw_output or "")
        with self.assertRaises(SessionInvalidError):
            backend.save_lens_as(str(root / "untrusted.len"))

    def test_unreadable_post_wav_state_blocks_further_writes(self):
        temp = workspace_temp_directory("wavefront-unreadable")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "dbgauss.len"
        source.write_text("fake lens", encoding="utf-8")
        session = FakeCodeVSession()
        backend = ComBackend(working_directory=root / "run", session=session)
        backend.open_lens(str(source))
        original_listing = session._wavefront_listing
        original_evaluate = session.evaluate
        def unreadable_after_wav():
            def fail_thickness(item):
                if item == "(THI S2)":
                    raise RuntimeError("injected read failure")
                return original_evaluate(item)
            session.evaluate = fail_thickness
            return original_listing()
        session._wavefront_listing = unreadable_after_wav
        task = backend.run_analysis(AnalysisRequest(kind=AnalysisKind.WAVEFRONT))
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "session_invalid")
        with self.assertRaises(SessionInvalidError):
            backend.save_lens_as(str(root / "untrusted.len"))


if __name__ == "__main__":
    unittest.main()
