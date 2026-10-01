"""Tests for the Phase D analyses, driven by the fake session.

The fake session answers the documented analysis calls (MTF_1FLD, RAYTRA) and
produces the SPO annotation text, so the call sequence, the parsing, the model
population and the cross-checks can all be exercised without CODE V.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from codev_mcp.com_backend import ComBackend
from codev_mcp.errors import ComputationError, ParameterError, UnsupportedError
from codev_mcp.models import (
    AnalysisKind,
    AnalysisOptions,
    AnalysisRequest,
    MtfType,
    Source,
    TaskState,
)
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession


class AnalysisTestCase(unittest.TestCase):
    session_kwargs: dict = {}

    def configure_session(self, session: FakeCodeVSession) -> None:
        """Hook for subclasses that need a different fake session."""

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("analysis")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.working = self.root / "run"
        self.lens_path = self.root / "dbgauss.len"
        self.lens_path.write_text("! lens placeholder\n", encoding="utf-8")
        self.session = FakeCodeVSession(**self.session_kwargs)
        self.configure_session(self.session)
        self.session.listing = self.session._build_listing()
        self.backend = ComBackend(working_directory=self.working, session=self.session)
        self.backend.open_lens(str(self.lens_path))


class FirstOrder(AnalysisTestCase):
    def run_task(self, **options):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions(**options))
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        return self.backend.get_analysis().first_order

    def test_reports_the_database_items(self):
        result = self.run_task()
        self.assertAlmostEqual(result.effective_focal_length, 100.0)
        self.assertAlmostEqual(result.f_number, 2.0)
        self.assertAlmostEqual(result.image_distance, 63.14)
        self.assertAlmostEqual(result.overall_length, 75.04)
        self.assertAlmostEqual(result.entrance_pupil_diameter, 50.0)
        self.assertAlmostEqual(result.exit_pupil_diameter, 57.38)
        self.assertAlmostEqual(result.entrance_pupil_distance, 55.87)
        self.assertAlmostEqual(result.exit_pupil_distance, -51.61)

    def test_reports_the_listing_values_separately(self):
        result = self.run_task()
        self.assertAlmostEqual(result.back_focal_length, 61.2346)
        self.assertAlmostEqual(result.front_focal_length, -29.1234)
        self.assertAlmostEqual(result.paraxial_image_height, 23.4567)

    def test_keeps_the_raw_output_and_the_precision_note(self):
        result = self.run_task()
        self.assertIn("(EFY Z1)", result.raw_output or "")
        self.assertIn("INFINITE CONJUGATES", result.raw_output or "")
        self.assertIn("sixteen", result.precision_note)
        self.assertIn("ENP and EXP are pupil distances", result.precision_note)

    def test_zoom_position_is_reported(self):
        result = self.run_task(zoom_position=1)
        self.assertEqual(result.zoom_position, 1)
        with self.assertRaises(ParameterError):
            self.run_task(zoom_position=4)

    def test_task_metadata_is_recorded(self):
        task = self.backend.run_analysis(AnalysisRequest(kind=AnalysisKind.FIRST_ORDER))
        self.assertEqual(task.kind, AnalysisKind.FIRST_ORDER)
        self.assertTrue(task.task_id.startswith("codev-"))
        self.assertEqual(task.source, Source.CODEV)
        self.assertIsNotNone(task.settings)
        self.assertEqual(task.settings.notes[0], "The service never refocuses: the lens is analysed as stored.")

    def test_an_afocal_system_is_flagged(self):
        self.session.first_order["AFC"] = 1.0
        result = self.run_task()
        self.assertTrue(any("afocal" in warning for warning in result.warnings))


class SpotDiagram(AnalysisTestCase):
    def run_task(self, **options):
        # The spot diagram runs as an asynchronous option, so the result is read
        # back through get_analysis.
        self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions(**options))
        )
        snapshot = self.backend.get_analysis()
        return snapshot.task, snapshot.spot_diagram

    def test_native_statistics_are_reported_as_radii(self):
        task, spot = self.run_task()
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        self.assertTrue(spot.size_is_radius)
        # The fake models a blur disk of radius spot_blur and prints diameters.
        self.assertAlmostEqual(spot.rms_radius, 0.004 / (2 ** 0.5), places=6)
        self.assertAlmostEqual(spot.max_radius, 0.004, places=6)

    def test_sampling_is_recorded_separately(self):
        _, spot = self.run_task(ray_grid=7)
        self.assertGreater(spot.plot_sample_count, 0)
        self.assertGreater(spot.statistics_sample_count, spot.plot_sample_count)
        # The native statistics sample count comes from the option listing.
        self.assertEqual(spot.statistics_sample_count, 1123)

    def test_reports_the_centroid_from_the_option(self):
        _, spot = self.run_task()
        self.assertEqual(spot.centroid_x, 0.0)
        self.assertEqual(spot.centroid_y, 0.0)

    def test_writes_an_image_and_keeps_the_raw_output(self):
        _, spot = self.run_task()
        self.assertIsNotNone(spot.image)
        self.assertEqual(Path(spot.image.path).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("Minimum RMS spot diameter", spot.raw_output or "")

    def test_the_statistics_warning_names_the_native_values(self):
        _, spot = self.run_task()
        self.assertTrue(any("diameters" in warning for warning in spot.warnings))
        self.assertFalse(any("differs from the native" in warning for warning in spot.warnings))

    def test_a_mismatch_between_the_grid_and_the_option_is_reported(self):
        self.session.spot_statistics_scale = 3.0
        _, spot = self.run_task()
        self.assertTrue(any("differs from the native" in warning for warning in spot.warnings))

    def test_one_field_per_task(self):
        with self.assertRaises(ParameterError) as caught:
            self.run_task(field_numbers=[1, 2])
        self.assertIn("one field", caught.exception.message)

    def test_an_unknown_field_is_rejected(self):
        with self.assertRaises(ParameterError):
            self.run_task(field_numbers=[9])

    def test_without_a_field_the_first_one_is_used_and_noted(self):
        task, spot = self.run_task()
        self.assertEqual(spot.field_number, 1)
        self.assertTrue(any("field 1 was used" in note for note in task.settings.notes))

    def test_a_listing_without_annotations_is_an_error(self):
        original = self.session._spot_statistics_listing
        self.session._spot_statistics_listing = lambda: "FIELD\r\nPOSITION\r\nCommand End:\r\n"
        try:
            task, _ = self.run_task()
        finally:
            self.session._spot_statistics_listing = original
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "computation_failed")
        self.assertIn("annotations", task.error.message)

    def test_the_pupil_grid_uses_the_entrance_pupil(self):
        self.run_task(ray_grid=7)
        self.assertGreater(len(self.session.raytra_calls), 0)
        # The first ray sits on the first tangent plane, offset from the pupil
        # point by the object space direction times the pupil distance.
        x, y, direction_x, direction_y = self.session.raytra_calls[0]
        self.assertAlmostEqual(direction_x, 0.0, places=9)
        self.assertAlmostEqual(direction_y, 0.0, places=9)
        self.assertLessEqual(abs(x), self.session.first_order["EPD"] / 2.0 + 1e-9)


class MultiFieldSpot(AnalysisTestCase):
    session_kwargs = {"spot_blur": 0.006}

    def configure_session(self, session: FakeCodeVSession) -> None:
        session.fields = [
            {"x": 0.0, "y": 0.0, "weight": 1.0},
            {"x": 0.0, "y": 10.0, "weight": 1.0},
            {"x": 0.0, "y": 14.0, "weight": 1.0},
        ]

    def test_field_two_uses_its_own_ray_direction(self):
        import math

        self.backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions(field_numbers=[2])
            )
        )
        snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED, snapshot.task.error)
        spot = snapshot.spot_diagram
        self.assertEqual(spot.field_number, 2)
        x, y, _, direction_y = self.session.raytra_calls[0]
        self.assertAlmostEqual(direction_y, math.tan(math.radians(10.0)))
        self.assertNotAlmostEqual(y, 0.0)

class Mtf(AnalysisTestCase):
    def run_task(self, **options):
        options.setdefault("frequencies", [10.0, 20.0, 40.0])
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.MTF, options=AnalysisOptions(**options))
        )
        return task, self.backend.get_analysis().mtf

    def test_returns_a_curve_per_field_with_tangential_and_sagittal(self):
        task, mtf = self.run_task()
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        self.assertEqual(len(mtf.curves), len(self.session.fields))
        self.assertEqual(mtf.frequencies, [10.0, 20.0, 40.0])
        self.assertEqual(len(mtf.curves[0].tangential), 3)
        self.assertEqual(len(mtf.curves[0].sagittal), 3)
        self.assertEqual(mtf.frequency_unit, "cycles/mm")
        self.assertAlmostEqual(mtf.azimuth, 0.0)

    def test_calls_codev_for_both_azimuths(self):
        self.run_task()
        azimuths = {call[3] for call in self.session.mtf_calls}
        self.assertEqual(azimuths, {0.0, 90.0})

    def test_writes_an_image_and_keeps_the_raw_table(self):
        _, mtf = self.run_task()
        self.assertIsNotNone(mtf.image)
        self.assertEqual(Path(mtf.image.path).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("MTF_1FLD", mtf.raw_output or "")
        self.assertIn("0deg", mtf.raw_output or "")

    def test_requires_a_frequency_grid(self):
        with self.assertRaises(ParameterError):
            self.backend.run_analysis(
                AnalysisRequest(kind=AnalysisKind.MTF, options=AnalysisOptions())
            )

    def test_geometric_mtf_is_refused(self):
        with self.assertRaises(UnsupportedError):
            self.backend.run_analysis(
                AnalysisRequest(
                    kind=AnalysisKind.MTF,
                    options=AnalysisOptions(frequencies=[10.0], mtf_type=MtfType.GEOMETRIC),
                )
            )

    def test_a_failed_calculation_is_reported(self):
        self.session.mtf_failure = True
        task, mtf = self.run_task()
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIsNone(mtf)
        self.assertIn("failed calculation", task.error.message)
        self.assertEqual(task.error.details["azimuth"], 0.0)


class AfocalMtf(AnalysisTestCase):
    def configure_session(self, session: FakeCodeVSession) -> None:
        session.first_order["AFC"] = 1.0

    def test_an_afocal_system_is_unsupported(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.MTF, options=AnalysisOptions(frequencies=[10.0]))
        )
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "unsupported")
        self.assertIn("afocal", task.error.message)


class TaskLifecycle(AnalysisTestCase):
    def test_cancel_returns_the_finished_task(self):
        self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        cancelled = self.backend.cancel_analysis()
        self.assertIsNotNone(cancelled)
        self.assertEqual(cancelled.state, TaskState.SUCCEEDED)

    def test_cancel_without_a_task_returns_none(self):
        self.assertIsNone(self.backend.cancel_analysis())

    def test_get_analysis_without_a_task_is_empty(self):
        snapshot = self.backend.get_analysis()
        self.assertIsNone(snapshot.task)
        self.assertIsNone(snapshot.first_order)

if __name__ == "__main__":
    unittest.main()
