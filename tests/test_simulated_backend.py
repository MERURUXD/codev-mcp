"""Behaviour tests for the simulated backend.

The simulated backend is what the automated test suite can rely on, so these
tests pin the contract that the real backend has to satisfy later: atomic
batches, refusal of solve controlled parameters, explicit zoom handling, the
analysis state machine and image output that lands on disk.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests import workspace_temp_directory

from codev_mcp.errors import (
    NotReadyError,
    ParameterError,
    SessionInvalidError,
    UnsupportedError,
)
from codev_mcp.models import (
    AnalysisKind,
    AnalysisOptions,
    AnalysisRequest,
    MtfType,
    ParameterEdit,
    Source,
    SurfaceRole,
    TaskState,
    UpdateRequest,
)
from codev_mcp.simulated import SimulatedBackend


class SimulatedBackendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = workspace_temp_directory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.backend = SimulatedBackend(working_directory=self.root / "run")
        self.lens_path = self.root / "dbgauss.len"
        self.lens_path.write_text("! sample lens placeholder\n", encoding="utf-8")

    def open(self, name: str = "dbgauss"):
        path = self.root / f"{name}.len"
        if not path.exists():
            path.write_text("! placeholder\n", encoding="utf-8")
        return self.backend.open_lens(str(path))


class StatusAndSession(SimulatedBackendTestCase):
    def test_status_marks_itself_as_simulated(self):
        status = self.backend.get_status()
        self.assertEqual(status.source, Source.SIMULATED)
        self.assertEqual(status.backend, "simulated")
        self.assertTrue(status.session_open)
        self.assertFalse(status.lens_open)
        self.assertTrue(any("Simulated backend" in warning for warning in status.warnings))

    def test_capabilities_flag_afocal_mtf_as_unsupported(self):
        capabilities = {entry.name: entry for entry in self.backend.get_status().capabilities}
        self.assertTrue(capabilities["mtf"].supported)
        self.assertFalse(capabilities["afocal_mtf"].supported)

    def test_get_lens_before_open_is_not_ready(self):
        with self.assertRaises(NotReadyError):
            self.backend.get_lens()

    def test_close_session_blocks_further_reads(self):
        self.open()
        self.backend.close_session()
        with self.assertRaises(NotReadyError):
            self.backend.get_lens()


class LensReading(SimulatedBackendTestCase):
    def test_open_requires_an_existing_file(self):
        with self.assertRaises(Exception) as caught:
            self.backend.open_lens(str(self.root / "missing.len"))
        self.assertEqual(caught.exception.kind.value, "not_found")

    def test_legacy_selector_returns_synthetic_structure(self):
        lens = self.open()
        self.assertEqual(len(lens.surfaces), 13)
        self.assertEqual(lens.surfaces[0].role, SurfaceRole.OBJECT)
        self.assertEqual(lens.surfaces[0].number, 0)
        self.assertEqual(lens.surfaces[-1].role, SurfaceRole.IMAGE)
        self.assertEqual(lens.stop_surface, 6)
        self.assertEqual(lens.units.value, "mm")
        self.assertEqual(lens.dimension_code, 2)
        self.assertEqual(len(lens.fields), 3)
        self.assertEqual(len(lens.wavelengths), 3)
        self.assertEqual(lens.zoom_positions, 1)
        self.assertEqual(lens.wavelengths[1].number, 2)
        self.assertTrue(lens.wavelengths[1].is_reference)
        self.assertEqual(sum(1 for s in lens.surfaces if s.is_stop), 1)

    def test_infinite_radius_is_kept_as_a_value_not_as_missing_data(self):
        lens = self.open()
        stop = next(surface for surface in lens.surfaces if surface.is_stop)
        self.assertTrue(stop.radius_is_infinite)
        self.assertIsNone(stop.radius)
        self.assertEqual(stop.thickness, 10.345678)

    def test_glass_names_are_reported(self):
        lens = self.open()
        self.assertEqual(lens.surfaces[1].glass, "BSM24_OHARA")


class LensEditing(SimulatedBackendTestCase):
    def test_applies_an_edit_and_reads_it_back(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=3, parameter="thickness", value=12.5)])
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(result.outcomes[0].previous_value, 11.234560)
        self.assertFalse(result.rolled_back)
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.surfaces[3].thickness, 12.5)

    def test_refuses_a_parameter_controlled_by_a_solve(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=11, parameter="thickness", value=10.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("solve", result.outcomes[0].rejected_reason)
        self.assertTrue(result.rolled_back)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[11].thickness, 61.234567)

    def test_a_batch_is_rejected_as_a_whole(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(surface=3, parameter="thickness", value=12.5),
                    ParameterEdit(surface=99, parameter="thickness", value=1.0),
                ]
            )
        )
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertTrue(result.rolled_back)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[3].thickness, 11.234560)
        self.assertEqual(self.backend.get_status().details["committed_revision"], 0)

    def test_multi_zoom_lens_requires_a_zoom_position(self):
        lens = self.open("zoomtriplet")
        self.assertEqual(lens.zoom_positions, 2)
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=3, parameter="thickness", value=25.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("zoom_position", result.outcomes[0].rejected_reason)

        applied = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(surface=3, parameter="thickness", value=25.0, zoom_position=2)]
            )
        )
        self.assertTrue(applied.outcomes[0].applied)

    def test_glass_names_cannot_smuggle_commands(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=3, parameter="glass", value="SK16; del all")])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("glass name", result.outcomes[0].rejected_reason)

    def test_object_surface_radius_is_refused(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=0, parameter="radius", value=10.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("object", result.outcomes[0].rejected_reason)

    def test_unknown_surface_is_reported_as_a_rejected_edit(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=42, parameter="radius", value=1.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("valid surfaces are 0..12", result.outcomes[0].rejected_reason)
        self.assertTrue(result.rolled_back)

    def test_invalid_session_refuses_writes(self):
        self.open()
        self.backend.session_valid = False
        with self.assertRaises(SessionInvalidError):
            self.backend.update_lens(
                UpdateRequest(edits=[ParameterEdit(surface=3, parameter="thickness", value=1.0)])
            )

    def test_edits_system_aperture_in_the_simulated_backend(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="aperture", parameter="value", value=45.0)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().aperture.value, 45.0)

    def test_system_aperture_rejects_value_above_real_backend_limit(self):
        self.open()
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(target="aperture", parameter="value", value=1e9 + 1)
        ]))
        self.assertFalse(result.outcomes[0].applied)

    def test_edits_only_an_existing_explicit_clear_aperture(self):
        self.open("singlet")
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=14.0
                    )
                ]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].apertures[0].radius, 14.0)

        self.open("dbgauss")
        refused = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=14.0
                    )
                ]
            )
        )
        self.assertFalse(refused.outcomes[0].applied)

    def test_ca_no_rejects_a_stored_explicit_circle(self):
        self.open("singlet")
        self.backend.lens.aperture_usage = "default_only"
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(surface=1, parameter="clear_aperture_radius", value=14.0)
        ]))
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("CA NO", result.outcomes[0].rejected_reason)


class FieldAndWavelengthEditing(SimulatedBackendTestCase):
    def test_reports_and_edits_vignetting_factors(self):
        self.open()
        fields = self.backend.get_lens().fields
        self.assertEqual([(f.vuy, f.vly) for f in fields], [(0.0, 0.0), (0.2, 0.3), (0.4, 0.4)])
        self.assertEqual({f.vux for f in fields} | {f.vlx for f in fields}, {0.0})
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(target="field", field=2, parameter="vuy", value=0.25),
            ParameterEdit(target="field", field=3, parameter="vly", value=0.45)]))
        self.assertTrue(all(o.applied for o in result.outcomes), result.warnings)
        fields = self.backend.get_lens().fields
        self.assertEqual([(f.vuy, f.vly) for f in fields], [(0.0, 0.0), (0.25, 0.3), (0.4, 0.45)])
        refused = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(target="field", field=2, parameter="vuy", value=1.0)]))
        self.assertFalse(refused.outcomes[0].applied)
        self.assertIn("-0.99..0.99", refused.outcomes[0].rejected_reason)

    def test_edits_a_field_angle(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="field", field=2, parameter="y_angle", value=12.5)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().fields[1].y_angle, 12.5)
        self.assertAlmostEqual(result.outcomes[0].previous_value, 10.0)

    def test_edits_a_field_weight(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="field", field=2, parameter="weight", value=0.5)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().fields[1].weight, 0.5)

    def test_rejects_an_unknown_field(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="field", field=9, parameter="y_angle", value=5.0)]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("valid fields", result.outcomes[0].rejected_reason)

    def test_edits_a_wavelength(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        target="wavelength", wavelength=1, parameter="micrometers", value=0.65
                    )
                ]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().wavelengths[0].micrometers, 0.65)

    def test_wavelength_weight_must_be_an_integer(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(target="wavelength", wavelength=1, parameter="weight", value=0.5)
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("integer", result.outcomes[0].rejected_reason)

    def test_changes_the_reference_wavelength(self):
        self.open()
        self.assertFalse(self.backend.get_lens().wavelengths[0].is_reference)
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        target="wavelength", wavelength=1, parameter="is_reference", value=1
                    )
                ]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        wavelengths = self.backend.get_lens().wavelengths
        self.assertTrue(wavelengths[0].is_reference)
        self.assertFalse(wavelengths[1].is_reference)

    def test_rejects_an_out_of_range_wavelength(self):
        self.open()
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        target="wavelength", wavelength=1, parameter="micrometers", value=0.0005
                    )
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("0.01", result.outcomes[0].rejected_reason)


class Saving(SimulatedBackendTestCase):
    def test_refuses_to_overwrite_the_source(self):
        self.open()
        with self.assertRaises(ParameterError):
            self.backend.save_lens_as(str(self.lens_path))

    def test_refuses_an_existing_target(self):
        self.open()
        target = self.root / "already.len"
        target.write_text("taken\n", encoding="utf-8")
        with self.assertRaises(ParameterError):
            self.backend.save_lens_as(str(target))

    def test_writes_a_new_file(self):
        self.open()
        target = self.root / "saved.lenz"
        result = self.backend.save_lens_as(str(target))
        self.assertFalse(result.overwritten)
        self.assertTrue(target.exists())
        self.assertGreater(result.bytes_written or 0, 0)
        self.assertIn("IMG", target.read_text(encoding="utf-8"))


class Analyses(SimulatedBackendTestCase):
    def test_first_order_task_advances_one_state_per_poll(self):
        self.open()
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertEqual(task.source, Source.SIMULATED)

        running = self.backend.get_analysis()
        self.assertEqual(running.task.state, TaskState.RUNNING)
        self.assertIsNone(running.first_order)

        finished = self.backend.get_analysis()
        self.assertEqual(finished.task.state, TaskState.SUCCEEDED)
        self.assertAlmostEqual(finished.first_order.effective_focal_length, 100.000123456789)
        self.assertEqual(finished.first_order.units.value, "mm")
        self.assertIn("string", finished.first_order.precision_note)

    def test_second_task_is_refused_while_one_runs(self):
        self.open()
        self.backend.run_analysis(AnalysisRequest(kind=AnalysisKind.FIRST_ORDER))
        with self.assertRaises(ParameterError):
            self.backend.run_analysis(AnalysisRequest(kind=AnalysisKind.FIRST_ORDER))

    def test_spot_diagram_reports_sampling_and_writes_an_image(self):
        self.open()
        self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions(ray_grid=7))
        )
        self.backend.get_analysis()
        snapshot = self.backend.get_analysis()
        spot = snapshot.spot_diagram
        self.assertIsNotNone(spot)
        self.assertTrue(spot.size_is_radius)
        self.assertGreater(spot.statistics_sample_count, spot.plot_sample_count)
        self.assertGreater(spot.rms_radius, 0.0)
        self.assertGreaterEqual(spot.max_radius, spot.rms_radius)
        image_path = Path(spot.image.path)
        self.assertTrue(image_path.exists())
        self.assertEqual(image_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_mtf_needs_a_frequency_grid(self):
        self.open()
        with self.assertRaises(ParameterError):
            self.backend.run_analysis(
                AnalysisRequest(kind=AnalysisKind.MTF, options=AnalysisOptions())
            )

    def test_mtf_returns_a_curve_per_field_and_an_image(self):
        self.open()
        self.backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.MTF,
                options=AnalysisOptions(frequencies=[10.0, 20.0, 40.0, 80.0]),
            )
        )
        self.backend.get_analysis()
        snapshot = self.backend.get_analysis()
        mtf = snapshot.mtf
        self.assertEqual(len(mtf.curves), 3)
        self.assertEqual(mtf.frequencies, [10.0, 20.0, 40.0, 80.0])
        self.assertEqual(len(mtf.curves[0].tangential), 4)
        self.assertTrue(all(0.0 <= value <= 1.0 for value in mtf.curves[0].tangential))
        self.assertTrue(Path(mtf.image.path).exists())

    def test_geometric_mtf_is_reported_as_unsupported(self):
        self.open()
        self.backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.MTF,
                options=AnalysisOptions(frequencies=[10.0], mtf_type=MtfType.GEOMETRIC),
            )
        )
        self.backend.get_analysis()
        with self.assertRaises(UnsupportedError):
            self.backend.get_analysis()

    def test_unknown_field_is_rejected(self):
        self.open()
        with self.assertRaises(ParameterError):
            self.backend.run_analysis(
                AnalysisRequest(
                    kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions(field_numbers=[9])
                )
            )

    def test_cancel_reports_the_task_state(self):
        self.open()
        self.backend.run_analysis(AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM))
        cancelled = self.backend.cancel_analysis()
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        again = self.backend.cancel_analysis()
        self.assertEqual(again.state, TaskState.CANCELLED)
        self.assertEqual(again.task_id, cancelled.task_id)


class NativePlotExport(SimulatedBackendTestCase):
    """The simulated native plot must never look like a CODE V export."""
    def run_plot(self, plot_type: str = "layout", **options):
        self.open()
        self.backend.run_analysis(
            AnalysisRequest(
                kind=AnalysisKind.NATIVE_PLOT,
                options=AnalysisOptions(plot_type=plot_type, **options),
            )
        )
        self.backend.get_analysis()
        return self.backend.get_analysis()

    def test_the_picture_says_it_is_simulated(self):
        snapshot = self.run_plot("spot")
        native = snapshot.native_plot
        self.assertIsNotNone(native)
        self.assertEqual(native.source, Source.SIMULATED)
        self.assertEqual(native.plot_type.value, "spot")
        # There is no neutral plot file, because no CODE V option ran.
        self.assertIsNone(native.plot_file_path)
        self.assertIsNone(native.plot_file_bytes)
        self.assertTrue(
            any("No CODE V option ran" in warning for warning in native.warnings)
        )
        self.assertIn("simulated", native.raw_output)
        image_path = Path(native.image.path)
        self.assertTrue(image_path.exists())
        self.assertEqual(image_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_every_plot_type_is_accepted(self):
        for plot_type in (
            "layout",
            "spot",
            "mtf",
            "ray_aberration",
            "field_aberration",
        ):
            with self.subTest(plot_type=plot_type):
                snapshot = self.run_plot(plot_type)
                self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
                self.assertIsNotNone(snapshot.native_plot.image)

    def test_a_plot_type_is_required(self):
        self.open()
        with self.assertRaises(ParameterError):
            self.backend.run_analysis(
                AnalysisRequest(kind=AnalysisKind.NATIVE_PLOT, options=AnalysisOptions())
            )

    def test_selection_settings_are_refused(self):
        self.open()
        with self.assertRaises(ParameterError) as caught:
            self.backend.run_analysis(
                AnalysisRequest(
                    kind=AnalysisKind.NATIVE_PLOT,
                    options=AnalysisOptions(plot_type="spot", field_numbers=[1]),
                )
            )
        self.assertEqual(caught.exception.details["not_accepted"], ["field_numbers"])

    def test_a_multi_zoom_lens_is_unsupported(self):
        self.open("zoomtriplet")
        with self.assertRaises(UnsupportedError):
            self.backend.run_analysis(
                AnalysisRequest(
                    kind=AnalysisKind.NATIVE_PLOT,
                    options=AnalysisOptions(plot_type="layout"),
                )
            )

    def test_the_capability_is_listed_with_a_simulated_note(self):
        self.open()
        capability = next(
            item
            for item in self.backend.capabilities()
            if item.name == "native_plot_export"
        )
        self.assertTrue(capability.supported)
        self.assertIn("simulated", capability.note)


class SimulatedLensState(SimulatedBackendTestCase):
    """The simulated backend mirrors the lens state fields of the COM backend."""
    def test_status_reports_the_lens_state_of_the_open_lens(self):
        self.open()
        details = self.backend.get_status().details
        self.assertEqual(details["lens_state"], "ready")
        self.assertEqual(details["committed_revision"], 0)
        self.assertTrue(details["lens_id"])
        self.assertIsNone(details["checkpoint_path"])
        self.assertEqual(details["recovery_count"], 0)
        self.assertTrue(details["simulated"])

    def test_a_successful_batch_advances_the_revision(self):
        self.open()
        self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.5)])
        )
        details = self.backend.get_status().details
        self.assertEqual(details["committed_revision"], 1)
        self.assertEqual(details["last_checkpoint"]["revision"], 1)

    def test_closing_the_session_marks_the_lens_invalid(self):
        self.open()
        status = self.backend.close_session()
        self.assertEqual(status.details["lens_state"], "invalid")
        self.assertFalse(status.details["session_valid"])

if __name__ == "__main__":
    unittest.main()
