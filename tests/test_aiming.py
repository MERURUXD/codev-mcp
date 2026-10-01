"""Tests for the pupil aiming map and the spot grid that uses it (E1)."""

from __future__ import annotations

import math
import unittest
from unittest import mock

from codev_mcp import aiming, plotting
from codev_mcp.com_backend import PUPIL_MAP_TOLERANCE
from codev_mcp.models import AnalysisKind, AnalysisOptions, AnalysisRequest, TaskState
from tests.fake_codev import FakeCodeVSession
from tests.test_com_analyses import AnalysisTestCase


class PupilMapMath(unittest.TestCase):
    def test_the_unit_grid_keeps_the_cells_inside_the_circle(self):
        grid = aiming.unit_pupil_grid(7)
        self.assertEqual(len(grid), 37)
        self.assertTrue(all(u * u + v * v <= 1.0 for u, v in grid))

    def test_the_calibration_nodes_cover_centre_and_two_rings(self):
        nodes = aiming.calibration_nodes()
        self.assertEqual(len(nodes), 17)
        self.assertEqual(nodes[0], (0.0, 0.0))
        radii = sorted({round(math.hypot(u, v), 9) for u, v in nodes})
        self.assertEqual(radii, [0.0, 0.5, 1.0])

    def test_vignetting_scales_each_pupil_edge_by_its_own_factor(self):
        self.assertEqual(aiming.vignetted_pupil(0.5, 1.0, 0.0, 0.0, 0.2, 0.3), (0.5, 0.8))
        self.assertEqual(aiming.vignetted_pupil(0.5, -1.0, 0.0, 0.0, 0.2, 0.3), (0.5, -0.7))
        self.assertEqual(aiming.vignetted_pupil(1.0, 0.0, 0.1, 0.4, 0.0, 0.0), (0.9, 0.0))
        self.assertEqual(aiming.vignetted_pupil(-1.0, 0.0, 0.1, 0.4, 0.0, 0.0), (-0.6, 0.0))
        # A negative factor widens the pupil.
        self.assertAlmostEqual(aiming.vignetted_pupil(0.0, 1.0, 0.0, 0.0, -0.25, 0.0)[1], 1.25)

    @staticmethod
    def _samples(function):
        return [(u, v, *function(u, v)) for u, v in aiming.calibration_nodes()]

    def test_a_cubic_pupil_is_reproduced_exactly(self):
        def launch(u, v):
            return (
                25.0 * u + 3.0 * u * v + 0.4 * v ** 3 + 1.0,
                25.0 * v - 12.0 + 0.7 * u * u + 0.2 * u ** 3,
            )

        fitted = aiming.fit_pupil_map(self._samples(launch))
        self.assertLess(fitted.max_residual, 1e-9)
        self.assertEqual(fitted.nodes, 17)
        for u, v in aiming.unit_pupil_grid(9):
            expected = launch(u, v)
            actual = fitted.launch(u, v)
            self.assertAlmostEqual(actual[0], expected[0], places=8)
            self.assertAlmostEqual(actual[1], expected[1], places=8)

    def test_a_higher_order_pupil_leaves_a_residual(self):
        fitted = aiming.fit_pupil_map(
            self._samples(lambda u, v: (25.0 * u + 10.0 * u ** 5, 25.0 * v))
        )
        self.assertGreater(fitted.max_residual, 1e-3)

    def test_too_few_rays_cannot_fit(self):
        with self.assertRaises(ValueError):
            aiming.fit_pupil_map(self._samples(lambda u, v: (u, v))[:9])

    def test_degenerate_rays_are_reported(self):
        with self.assertRaises(ValueError):
            aiming.fit_pupil_map([(0.5, 0.5, 1.0, 1.0)] * 12)


class SpotGridAiming(AnalysisTestCase):
    """The plotted grid follows the real pupil instead of the paraxial one."""

    fields = [
        {"x": 0.0, "y": 0.0, "weight": 1.0},
        {"x": 0.0, "y": 25.0, "weight": 1.0},
        {"x": 0.0, "y": 36.0, "weight": 1.0, "vuy": 0.2, "vly": 0.3},
    ]

    def configure_session(self, session: FakeCodeVSession) -> None:
        session.fields = [dict(field) for field in self.fields]
        # A large field: the real pupil is far from the paraxial one.
        session.pupil_shift = 45.0
        session.pupil_cubic = 0.06

    def run_spot(self, field: int, **options):
        self.backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.SPOT_DIAGRAM,
                options=AnalysisOptions(field_numbers=[field], **options),
            )
        )
        snapshot = self.backend.get_analysis()
        return snapshot.task, snapshot.spot_diagram

    def test_every_grid_ray_of_a_large_field_reaches_the_image(self):
        task, spot = self.run_spot(2, ray_grid=7)
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)
        waves = len(self.session.wavelengths)
        self.assertEqual(spot.plot_sample_count, 37 * waves)
        self.assertFalse(any("blocked" in warning for warning in spot.warnings), spot.warnings)

    def test_the_paraxial_launch_would_have_been_blocked(self):
        # What the fake models: a launch from the paraxial pupil (no aiming)
        # misses the real pupil and is stopped by the aperture.
        session = self.session
        dy = math.tan(math.radians(25.0))
        epd, enp = session.first_order["EPD"], session.first_order["ENP"]
        status, _ = session.raytra(1, 1, 1, [0.0, -0.9 * epd / 2 + dy * enp, 0.0, dy])
        self.assertEqual(status, -6.0)

    def test_the_calibration_uses_seventeen_aimed_rays_per_wavelength(self):
        self.run_spot(2)
        waves = len(self.session.wavelengths)
        self.assertEqual(len(self.session.rayrsi_calls), 17 * waves)
        zoom, wavelength, field, inputs = self.session.rayrsi_calls[0]
        self.assertEqual((zoom, wavelength, field), (1, 1, 2))
        # Relative pupil coordinates only; the field is selected by number.
        self.assertEqual(inputs[2:], [0.0, 0.0])
        self.assertEqual(
            sorted({call[1] for call in self.session.rayrsi_calls}), list(range(1, waves + 1))
        )

    def test_the_grid_rays_carry_the_field_direction(self):
        self.run_spot(2)
        dy = math.tan(math.radians(25.0))
        for _, _, direction_x, direction_y in self.session.raytra_calls:
            self.assertAlmostEqual(direction_x, 0.0)
            self.assertAlmostEqual(direction_y, dy)

    def test_the_pupil_is_scaled_by_the_vignetting_factors(self):
        _, spot = self.run_spot(3)
        vs = [call[3][1] for call in self.session.rayrsi_calls]
        self.assertAlmostEqual(max(vs), 1.0 - 0.2)
        self.assertAlmostEqual(min(vs), -(1.0 - 0.3))
        # With the scaled pupil no grid ray is blocked.
        self.assertFalse(any("blocked" in warning for warning in spot.warnings), spot.warnings)

    def test_blocked_grid_rays_are_reported_and_left_out(self):
        self.session.aperture_limit = 0.8
        _, spot = self.run_spot(1, ray_grid=7)
        self.assertTrue(any("blocked" in warning for warning in spot.warnings), spot.warnings)
        waves = len(self.session.wavelengths)
        self.assertLess(spot.plot_sample_count, 37 * waves)
        self.assertGreater(spot.plot_sample_count, 0)

    def test_a_pupil_the_fit_cannot_follow_is_flagged(self):
        self.session.pupil_cubic = 0.0
        self.session.pupil_quintic = 0.5
        _, spot = self.run_spot(2)
        self.assertTrue(any("aiming map" in warning for warning in spot.warnings), spot.warnings)

    def test_a_well_fitted_pupil_gives_no_aiming_warning(self):
        _, spot = self.run_spot(2)
        self.assertFalse(any("aiming map" in warning for warning in spot.warnings), spot.warnings)

    def test_rays_that_cannot_be_aimed_fail_the_task(self):
        self.session.rayrsi_status = lambda u, v: 4.0
        task, spot = self.run_spot(2)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error.kind.value, "computation_failed")
        self.assertIn("could not be aimed", task.error.message)
        self.assertIsNone(spot)

    def test_a_few_failed_calibration_rays_are_tolerated(self):
        # One node out of seventeen fails: the fit still has enough rays.
        self.session.rayrsi_status = lambda u, v: 4.0 if (u, v) == (1.0, 0.0) else 0.0
        task, _ = self.run_spot(2)
        self.assertEqual(task.state, TaskState.SUCCEEDED, task.error)

    def test_the_plot_is_drawn_about_the_grid_centroid(self):
        # An off axis field lands far from the axis; the picture must not be
        # scaled to that distance, or the spot shrinks to a dot.
        self.session.image_offset = (0.0, 72.4)
        seen = {}
        real = plotting.scatter_plot

        def spy(points, **kwargs):
            seen["points"] = points
            return real(points, **kwargs)

        with mock.patch.object(plotting, "scatter_plot", spy):
            _, spot = self.run_spot(2)
        points = seen["points"]
        self.assertEqual(len(points), spot.plot_sample_count)
        self.assertAlmostEqual(sum(p[1] for p in points) / len(points), 0.0, places=9)
        self.assertLess(max(abs(p[1]) for p in points), 1.0)

    def spy_plot(self, field=2):
        seen = {}
        real = plotting.scatter_plot

        def spy(points, **kwargs):
            seen.update(kwargs)
            return real(points, **kwargs)

        with mock.patch.object(plotting, "scatter_plot", spy):
            _, spot = self.run_spot(field)
        return seen, spot

    def test_the_redrawn_plot_carries_the_airy_disk(self):
        seen, spot = self.spy_plot()
        reference = self.session.wavelengths[self.session.reference - 1]
        f_number = self.session.first_order["FNO"]
        expected = 1.22 * reference["nm"] / 1e6 * f_number
        self.assertAlmostEqual(seen["airy_radius"], expected, places=12)
        note = next(w for w in spot.warnings if "green circle" in w)
        self.assertIn("service calculated", note)
        self.assertIn("native 100% spot radius", note)

    def test_the_airy_disk_is_left_out_for_a_finite_object(self):
        self.session.surfaces[0]["thickness"] = 500.0
        self.session.listing = self.session._build_listing()
        self.backend.open_lens(str(self.lens_path))
        seen, spot = self.spy_plot()
        self.assertIsNone(seen["airy_radius"])
        self.assertTrue(any("finite distance" in w for w in spot.warnings), spot.warnings)

    def test_the_tolerance_is_a_fraction_of_the_pupil_radius(self):
        self.assertLess(PUPIL_MAP_TOLERANCE, 0.05)


if __name__ == "__main__":
    unittest.main()
