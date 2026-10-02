"""Tests for the real CODE V backend, driven by a fake session.

The fake session reproduces the CODE V behaviours that the backend has to
survive: unknown database items echoing a stale value, solve and pickup
controlled parameters accepting an edit without applying it, and zoom qualified
edits being ignored for parameters that were never zoomed.
"""

from __future__ import annotations

import unittest.mock
import unittest
from pathlib import Path

from codev_mcp.com_backend import ComBackend
from codev_mcp.checkpoints import compare_snapshots, read_snapshot
from codev_mcp.errors import (
    NotFoundError,
    NotReadyError,
    ParameterError,
    SessionInvalidError,
    UnsupportedError,
)
from codev_mcp.models import (
    AnalysisKind,
    AnalysisRequest,
    ParameterEdit,
    Source,
    SurfaceRole,
    UpdateRequest,
)
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession


class ComBackendTestCase(unittest.TestCase):
    session_kwargs: dict = {}

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("com")
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

    def configure_session(self, session: FakeCodeVSession) -> None:
        """Hook for subclasses that need a different session before opening."""

    def surface(self, number: int):
        return self.backend.get_lens().surfaces[number]


class Status(ComBackendTestCase):
    def test_status_reports_the_real_backend(self):
        status = self.backend.get_status()
        self.assertEqual(status.backend, "com")
        self.assertEqual(status.source, Source.CODEV)
        self.assertEqual(status.codev_version, "10.2;Build (207)")
        self.assertTrue(status.session_open)
        self.assertTrue(status.lens_open)

    def test_analyses_are_reported_with_their_verification_state(self):
        capabilities = {entry.name: entry for entry in self.backend.get_status().capabilities}
        self.assertTrue(capabilities["read_lens"].supported)
        for name in ("first_order", "spot_diagram", "mtf"):
            self.assertIn(name, capabilities)
        # They are only claimed as supported once the real machine acceptance has
        # produced evidence.
        for name in ("first_order", "spot_diagram", "mtf"):
            if not capabilities[name].supported:
                self.assertIn("verification", capabilities[name].note or "")
        self.assertFalse(capabilities["afocal_mtf"].supported)


class OpenLens(ComBackendTestCase):
    def test_open_requires_an_existing_file(self):
        with self.assertRaises(NotFoundError):
            self.backend.open_lens(str(self.root / "missing.len"))

    def test_open_rejects_a_relative_path(self):
        with self.assertRaises(ParameterError):
            self.backend.open_lens("lens\\dbgauss.len")

    def test_open_rejects_command_characters_in_the_path(self):
        with self.assertRaises(ParameterError) as caught:
            self.backend.open_lens(str(self.root / "lens.len; del all"))
        self.assertIn("command structure", caught.exception.message)


    def test_an_ampersand_in_the_path_is_refused(self):
        # Unquoted, "&" continued the command onto the next one (review H3 probe).
        with self.assertRaises(ParameterError) as caught:
            self.backend.save_lens_as(str(self.root / "a&b.len"))
        self.assertIn("command structure", caught.exception.message)

    def test_paths_with_comment_or_macro_characters_are_quoted(self):
        from codev_mcp.safety import command_filespec

        plain = Path(r"C:\out\run-1\lens_2.len")
        self.assertEqual(command_filespec(plain), str(plain))
        for name in ("a!b.len", "a$b.len", "a#b.len", "a@.len", "a%b.len", "a^b.len", "a,b.len",
                     "PROGRA~1.len", "a b.len"):
            with self.subTest(name=name):
                path = Path(r"C:\out") / name
                self.assertEqual(command_filespec(path), f'"{path}"')

    def test_save_as_with_an_exclamation_mark_writes_that_exact_file(self):
        # Unquoted, CODE V read "!" as a comment and saved x!y.len as x.len, past the overwrite check.
        self.backend.open_lens(str(self.lens_path))
        target = self.root / "x!y.len"
        self.backend.save_lens_as(str(target))
        saves = [command for command in self.session.commands if command.startswith("sav ")]
        self.assertIn(f'sav "{target}"', saves)
        self.assertTrue(target.exists())
        self.assertFalse((self.root / "x.len").exists())

    def test_open_rejects_other_suffixes(self):
        other = self.root / "lens.seq"
        other.write_text("x", encoding="utf-8")
        with self.assertRaises(ParameterError):
            self.backend.open_lens(str(other))

    def test_open_without_an_extension_assumes_len(self):
        target = self.root / "noext"
        self.session._saved[str(target.with_suffix(".len")).lower()] = self.session._snapshot()
        (self.root / "noext.len").write_text("x", encoding="utf-8")
        lens = self.backend.open_lens(str(target))
        self.assertEqual(lens.source, Source.CODEV)


class ReadLens(ComBackendTestCase):
    def test_reads_surfaces_in_native_numbering(self):
        lens = self.backend.get_lens()
        self.assertEqual(lens.surfaces[0].role, SurfaceRole.OBJECT)
        self.assertEqual(lens.surfaces[0].number, 0)
        self.assertEqual(lens.surfaces[-1].role, SurfaceRole.IMAGE)
        self.assertEqual(lens.stop_surface, 1)
        self.assertTrue(lens.surfaces[1].is_stop)
        self.assertEqual(lens.surfaces[1].label, "STO")

    def test_infinite_radius_is_reported_as_infinite_not_as_a_number(self):
        surface = self.surface(0)
        self.assertTrue(surface.radius_is_infinite)
        self.assertIsNone(surface.radius)
        self.assertTrue(surface.thickness_is_infinite)

    def test_finite_values_are_reported_verbatim(self):
        surface = self.surface(1)
        self.assertAlmostEqual(surface.radius, 64.12345678901234)
        self.assertAlmostEqual(surface.thickness, 9.12345599999)

    def test_glass_includes_the_catalog(self):
        self.assertEqual(self.surface(1).glass, "BSM24_OHARA")

    def test_units_fields_and_wavelengths(self):
        lens = self.backend.get_lens()
        self.assertEqual(lens.units.value, "mm")
        self.assertEqual(lens.dimension_code, 2)
        self.assertEqual(len(lens.fields), 2)
        self.assertAlmostEqual(lens.fields[1].y_angle, 10.0)
        self.assertEqual(len(lens.wavelengths), 2)
        self.assertAlmostEqual(lens.wavelengths[0].micrometers, 0.6563)
        self.assertTrue(lens.wavelengths[1].is_reference)

    def test_raw_listing_is_kept(self):
        self.assertIn("INFINITE CONJUGATES", self.backend.get_lens().raw_listing or "")

    def test_zoom_position_is_validated(self):
        with self.assertRaises(ParameterError):
            self.backend.get_lens(zoom_position=3)

    def test_cross_check_warns_when_the_listing_disagrees(self):
        self.session.surfaces[1]["radius"] = 58.0
        lens = self.backend.get_lens()
        self.assertTrue(any("differs from" in warning for warning in lens.warnings))

class EditLens(ComBackendTestCase):
    def test_applies_a_thickness_edit_and_reads_it_back(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.5)])
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertFalse(result.rolled_back)
        self.assertAlmostEqual(self.surface(1).thickness, 9.5)
        self.assertTrue(any(command.startswith("THI") for command in self.session.commands))
        self.assertTrue(result.restore_point)
        self.assertTrue(any(command.startswith("sav ") for command in self.session.commands))

    def test_rejects_a_solve_controlled_parameter_before_sending_a_command(self):
        self.session.solves = {1: "PIM"}
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=5.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("PIM", result.outcomes[0].rejected_reason)
        self.assertFalse(any(command.startswith("THI") for command in self.session.commands))
        self.assertAlmostEqual(self.surface(1).thickness, 9.12345599999)

    def test_detects_a_silently_ignored_edit_and_rolls_back(self):
        self.session.pickups = {1: "PIC"}
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="radius", value=100.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertTrue(result.rolled_back)
        self.assertIn("did not change", result.outcomes[0].rejected_reason)
        self.assertAlmostEqual(self.surface(1).radius, 64.12345678901234)

    def test_a_batch_is_all_or_nothing(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(surface=1, parameter="thickness", value=9.5),
                    ParameterEdit(surface=99, parameter="thickness", value=1.0),
                ]
            )
        )
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertTrue(result.rolled_back)
        self.assertTrue(result.restore_point is None)
        self.assertAlmostEqual(self.surface(1).thickness, 9.12345599999)

    def test_a_plane_can_be_given_a_radius_and_only_that_changes(self):
        self.session.surfaces[2]["radius"] = None
        self.session.listing = self.session._build_listing()
        self.backend.open_lens(str(self.lens_path))
        self.assertTrue(self.surface(2).radius_is_infinite)
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=2, parameter="radius", value=-60.0)])
        )
        self.assertTrue(result.outcomes[0].applied, result.outcomes[0].rejected_reason)
        self.assertFalse(result.rolled_back)
        self.assertFalse(self.surface(2).radius_is_infinite)
        self.assertAlmostEqual(self.surface(2).radius, -60.0)

    def test_a_thickness_edit_cannot_turn_a_plane_curved(self):
        self.session.surfaces[2]["radius"] = None
        self.session.listing = self.session._build_listing()
        self.backend.open_lens(str(self.lens_path))
        original = self.session.command
        # CODE V would not do this; the guard must still see the second change.
        self.session.command = lambda text, *a, **k: (
            self.session.surfaces[2].update(radius=-60.0) if text.startswith("THI") else None,
            original(text, *a, **k))[1]
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.5)])
        )
        self.assertTrue(result.rolled_back)
        self.assertTrue(self.surface(2).radius_is_infinite)

    def test_refuses_an_object_surface_radius(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=0, parameter="radius", value=42.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("object", result.outcomes[0].rejected_reason)

    def test_refuses_aperture_parameters_for_now(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="semi_aperture", value=12.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("read only", result.outcomes[0].rejected_reason)

    def test_refuses_to_create_a_surface_aperture_from_a_default(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=12.0
                    )
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("exactly one explicit aperture", result.outcomes[0].rejected_reason)

    def test_glass_names_are_validated_before_the_command_is_built(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="glass", value="SK16; del all")])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("glass name", result.outcomes[0].rejected_reason)
        self.assertFalse(any("del all" in command for command in self.session.commands))

    def test_applies_a_glass_edit(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="glass", value="SK16_SCHOTT")])
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertEqual(self.surface(1).glass.split("_")[0], "SK16")

    def test_edits_a_field_angle(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(target="field", field=2, parameter="y_angle", value=12.5)])
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().fields[1].y_angle, 12.5)

    def test_edits_a_wavelength(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="wavelength", wavelength=1, parameter="micrometers", value=0.65)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertAlmostEqual(self.backend.get_lens().wavelengths[0].micrometers, 0.65)
        self.assertTrue(any(command.startswith("WL W1 650") for command in self.session.commands))

    def test_rejects_a_fractional_wavelength_weight(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="wavelength", wavelength=1, parameter="weight", value=0.5)]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("integer", result.outcomes[0].rejected_reason)

    def test_changes_the_reference_wavelength(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="wavelength", wavelength=1, parameter="is_reference", value=1)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(self.backend.get_lens().wavelengths[0].is_reference)

    def test_rejects_out_of_range_wavelengths(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="wavelength", wavelength=1, parameter="micrometers", value=0.0005)]
            )
        )
        self.assertFalse(result.outcomes[0].applied)

    def test_rejects_an_unknown_wavelength_number(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="wavelength", wavelength=9, parameter="micrometers", value=0.5)]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("valid wavelengths", result.outcomes[0].rejected_reason)

    def test_marks_the_session_invalid_when_the_recovery_fails(self):
        self.session.pickups = {1: "PIC"}
        original_load = self.session._load

        def failing_load(snapshot):  # noqa: ANN001
            raise RuntimeError("recovery unavailable")

        self.session._load = failing_load
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="radius", value=100.0)])
        )
        self.session._load = original_load
        self.assertFalse(result.rolled_back)
        self.assertFalse(result.session_valid)
        with self.assertRaises(SessionInvalidError):
            self.backend.update_lens(
                UpdateRequest(edits=[ParameterEdit(surface=1, parameter="radius", value=101.0)])
            )


class ApertureEditing(ComBackendTestCase):
    session_kwargs = {
        "surface_apertures": {
            1: {"kind": "clear", "shape": "circular", "label": None, "radius": 20.0}
        }
    }

    def test_reads_native_and_derived_system_aperture_values(self):
        lens = self.backend.get_lens()
        self.assertEqual(lens.aperture.kind, "epd")
        self.assertEqual(lens.aperture.definition_source, "listing")
        self.assertAlmostEqual(lens.aperture.value, 50.0)
        self.assertAlmostEqual(lens.aperture.derived_epd, 50.0)
        self.assertEqual(lens.aperture.units.value, "mm")

    def test_edits_the_existing_system_aperture_type(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="aperture", parameter="value", value=45.0)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(any(command.startswith("EPD 45") for command in self.session.commands))
        self.assertAlmostEqual(self.backend.get_lens().aperture.value, 45.0)

    def test_rejects_nonpositive_aperture_values_before_a_command(self):
        command_count = len(self.session.commands)
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="aperture", parameter="value", value=0.0)]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("at least 1e-12", result.outcomes[0].rejected_reason)
        self.assertEqual(len(self.session.commands), command_count)

    def test_truncated_aperture_state_is_visible_and_refuses_writes(self):
        self.session.text_buffer_size = len(self.session.listing) + 1
        lens = self.backend.get_lens()
        self.assertEqual(lens.aperture_usage, "unknown")
        self.assertTrue(all(not item.aperture_data_complete for item in lens.surfaces))
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(target="aperture", parameter="value", value=45.0)]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("incomplete or truncated", result.outcomes[0].rejected_reason)

    def test_edits_an_existing_centered_circular_clear_aperture(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=18.25
                    )
                ]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(any(command.startswith("CIR S1 CLR 18.25") for command in self.session.commands))
        surface = self.backend.get_lens().surfaces[1]
        self.assertAlmostEqual(surface.apertures[0].radius, 18.25)
        self.assertAlmostEqual(surface.semi_aperture, 18.25)

    def test_aperture_and_thickness_share_one_atomic_batch(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(target="aperture", parameter="value", value=48.0),
                    ParameterEdit(surface=1, parameter="thickness", value=9.5),
                ]
            )
        )
        self.assertTrue(all(outcome.applied for outcome in result.outcomes))
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.aperture.value, 48.0)
        self.assertAlmostEqual(lens.surfaces[1].thickness, 9.5)

    def test_readback_mismatch_rolls_back_the_clear_aperture(self):
        original = self.session._run_command

        def ignore_clear(command):  # noqa: ANN001
            if command.startswith("CIR S1 CLR"):
                return "Command End:\r\n"
            return original(command)

        self.session._run_command = ignore_clear
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=17.0
                    )
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertTrue(result.rolled_back)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].apertures[0].radius, 20.0)


class LabeledApertureEditing(ComBackendTestCase):
    session_kwargs = {
        "surface_apertures": {
            1: {"kind": "clear", "shape": "circular", "label": "ABC", "radius": 14.0}
        }
    }

    def test_labeled_clear_aperture_commits_with_its_label(self):
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(surface=1, parameter="clear_aperture_radius", value=13.5)
        ]))
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(result.session_valid)
        self.assertTrue(any(command.startswith("CIR S1 CLR L'ABC' 13.5")
                            for command in self.session.commands))
        aperture = self.backend.get_lens().surfaces[1].apertures[0]
        self.assertEqual(aperture.label, "ABC")
        self.assertAlmostEqual(aperture.radius, 13.5)

    def test_labeled_clear_aperture_rolls_back_after_a_later_failure(self):
        original = self.backend._confirm

        def reject_thickness(plan, session):
            if plan.edit.parameter == "thickness":
                return False, "mismatch"
            return original(plan, session)

        self.backend._confirm = reject_thickness
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(surface=1, parameter="clear_aperture_radius", value=13.5),
            ParameterEdit(surface=2, parameter="thickness", value=7.0),
        ]))
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertTrue(result.rolled_back)
        self.assertTrue(result.session_valid)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].apertures[0].radius, 14.0)


class ComplexApertureEditing(ComBackendTestCase):
    def configure_session(self, session: FakeCodeVSession) -> None:
        session.surface_apertures = {
            1: {"kind": "obscuration", "shape": "circular", "label": None, "radius": 3.0}
        }
        session.listing = session._build_listing()

    def test_obscuration_remains_read_only(self):
        lens = self.backend.get_lens()
        self.assertEqual(lens.surfaces[1].apertures[0].kind, "obscuration")
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=2.0
                    )
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("centered circular clear", result.outcomes[0].rejected_reason)


class DefaultOnlyStoredAperture(ComBackendTestCase):
    def configure_session(self, session: FakeCodeVSession) -> None:
        session.surface_apertures = {1: {"kind": "clear", "shape": "circular", "radius": 10.0}}

    def test_ca_no_preserves_but_cannot_edit_explicit_cir(self):
        self.session.listing = self.session.listing.replace("    CA\r\n", "    CA NO\r\n")
        self.session._custom_listing = True
        self.assertEqual(self.backend.get_lens().aperture_usage, "default_only")
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(surface=1, parameter="clear_aperture_radius", value=12.0)
        ]))
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("CA NO", result.outcomes[0].rejected_reason)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].apertures[0].radius, 10.0)


class ZoomApertureReading(ComBackendTestCase):
    session_kwargs = {"zoom_positions": 3}

    def configure_session(self, session: FakeCodeVSession) -> None:
        session.surface_apertures = {1: {"kind": "clear", "shape": "circular", "radius": 10.0}}

    def test_each_zoom_and_checkpoint_uses_its_native_aperture_value(self):
        shared = self.backend._current_checkpoint().snapshot
        base = self.session.listing
        self.session.listing = base.replace(
            " ZOOM DATA\r\n",
            " ZOOM DATA\r\n    CIR S1 10.00000 20.00000 30.00000\r\n",
        )
        self.session._custom_listing = True
        for zoom, value in enumerate((10, 20, 30), 1):
            self.assertAlmostEqual(
                self.backend.get_lens(zoom_position=zoom).surfaces[1].apertures[0].radius,
                value,
            )
        before = read_snapshot(self.session, self.backend.get_lens())
        self.assertEqual(
            [zoom.surfaces[1].apertures[0].radius for zoom in before.zooms],
            [10, 20, 30],
        )
        self.assertTrue(any("zoom 2 explicit apertures" in item["where"]
                            for item in compare_snapshots(shared, before)))
        self.session.listing = self.session.listing.replace(
            "CIR S1 10.00000 20.00000 30.00000",
            "CIR S1 10.00000 20.00000 31.00000",
        )
        after = read_snapshot(self.session, self.backend.get_lens())
        self.assertTrue(any("zoom 3 explicit apertures" in item["where"]
                            for item in compare_snapshots(before, after)))


class IncompleteApertureEditing(ComBackendTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session.listing = self.session.listing.replace(
            "REFRACTIVE INDICES",
            "APERTURE DATA/EDGE DEFINITIONS\r\n   CIG S1 10 1 2 0\r\n\r\nREFRACTIVE INDICES",
        )
        self.session._custom_listing = True

    def test_unknown_native_aperture_definition_fails_closed(self):
        lens = self.backend.get_lens()
        self.assertFalse(lens.surfaces[1].aperture_data_complete)
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="clear_aperture_radius", value=12.0
                    )
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("incomplete or unsupported", result.outcomes[0].rejected_reason)

    def test_a_failed_solve_probe_warns_instead_of_faking_absence(self):
        """The solve pre-check going dark must be visible, not silent success."""
        # This case exercises solve diagnostics on a fully checkpointable lens;
        # the class fixture's unknown aperture must not mask that behavior.
        self.session.listing = self.session._build_listing()
        self.session._custom_listing = False
        original_evaluate = self.session.evaluate

        def failing_probe(item):  # noqa: ANN001
            if "TYP SOL" in item:
                raise SessionInvalidError("probe unavailable")
            return original_evaluate(item)

        self.session.evaluate = failing_probe
        try:
            result = self.backend.update_lens(
                UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.5)])
            )
        finally:
            self.session.evaluate = original_evaluate
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(any("solve probe" in warning for warning in result.warnings))
        self.assertTrue(any(command.startswith("THI") for command in self.session.commands))


class MultiZoom(ComBackendTestCase):
    session_kwargs = {"zoom_positions": 3}

    def configure_session(self, session: FakeCodeVSession) -> None:
        session.zoom_surface(1, "thickness", {1: 8.0, 2: 12.0, 3: 20.0})

    def test_requires_a_zoom_position(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=10.0)])
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("zoom_position", result.outcomes[0].rejected_reason)

    def test_system_aperture_editing_is_refused_for_multi_zoom(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        target="aperture", parameter="value", value=45.0, zoom_position=2
                    )
                ]
            )
        )
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("single-zoom", result.outcomes[0].rejected_reason)

    def test_edits_only_the_requested_zoom_position(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(surface=1, parameter="thickness", value=13.0, zoom_position=2)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(any(" Z2 " in command for command in self.session.commands))
        values = self.session.surfaces[1]["zoom"]["thickness"]
        self.assertAlmostEqual(values[1], 8.0)
        self.assertAlmostEqual(values[2], 13.0)
        self.assertAlmostEqual(values[3], 20.0)

    def test_warns_when_the_parameter_is_shared_across_zoom_positions(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[ParameterEdit(surface=2, parameter="thickness", value=60.0, zoom_position=2)]
            )
        )
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(any("not zoomed" in warning for warning in result.warnings))


class SaveLens(ComBackendTestCase):
    def test_saves_to_a_new_file(self):
        target = self.root / "saved.len"
        result = self.backend.save_lens_as(str(target))
        self.assertFalse(result.overwritten)
        self.assertTrue(target.exists())
        self.assertGreater(result.bytes_written or 0, 0)

    def test_refuses_to_overwrite_the_source(self):
        with self.assertRaises(ParameterError):
            self.backend.save_lens_as(str(self.lens_path))

    def test_refuses_an_existing_target(self):
        target = self.root / "taken.len"
        target.write_text("taken", encoding="utf-8")
        with self.assertRaises(ParameterError):
            self.backend.save_lens_as(str(target))

    def test_refuses_a_path_with_command_characters(self):
        with self.assertRaises(ParameterError):
            self.backend.save_lens_as(str(self.root / "bad.len; del all"))


class SessionLifecycle(ComBackendTestCase):
    def test_close_session_stops_codev(self):
        status = self.backend.close_session()
        self.assertFalse(status.session_open)
        self.assertFalse(self.session.started)
        # A closed service holds no trusted lens any more, so the refusal names
        # the session format rather than a missing lens.
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()
        status = self.backend.get_status()
        self.assertFalse(status.session_open)
        self.assertEqual(status.details["lens_state"], "invalid")

    def test_a_session_that_cannot_start_is_reported_not_raised_from_status(self):
        def broken():
            raise RuntimeError("no COM object")

        backend = ComBackend(working_directory=self.working, session_factory=broken)
        status = backend.get_status()
        self.assertFalse(status.ready)
        self.assertFalse(status.session_open)
        self.assertTrue(status.warnings)
        with self.assertRaises(Exception):
            backend.open_lens(str(self.lens_path))


class SessionRecovery(ComBackendTestCase):
    """The engine dies at random on this machine; the backend has to survive it."""
    def make_backend(self, sessions: list, **kwargs) -> ComBackend:
        factory = lambda: sessions.pop(0)  # noqa: E731 - the queue is consumed
        backend = ComBackend(
            working_directory=self.working, session_factory=factory, **kwargs
        )
        backend.start_attempts = kwargs.get("start_attempts", 1)
        return backend

    def test_a_transient_start_failure_is_retried(self):
        class Broken:
            def start(self):
                raise RuntimeError("engine crashed on startup")

            def stop(self):
                pass

        attempts = [Broken(), Broken(), self.session]
        backend = self.make_backend(attempts, start_attempts=3)
        backend.start_attempts = 3
        # The retry delay would make the test slow; the failure is immediate.
        with unittest.mock.patch("codev_mcp.com_backend.time.sleep"):
            session = backend._get_session()
        self.assertIs(session, self.session)

    def test_start_failure_is_reported_after_the_last_attempt(self):
        class Broken:
            def start(self):
                raise RuntimeError("engine crashed on startup")

            def stop(self):
                pass

        backend = self.make_backend([Broken(), Broken()], start_attempts=2)
        backend.start_attempts = 2
        with unittest.mock.patch("codev_mcp.com_backend.time.sleep"):
            with self.assertRaises(RuntimeError):
                backend._get_session()
        self.assertIn("engine crashed", backend._start_error or "")

    def test_a_failed_start_is_refused_for_a_cooldown_then_tried_again(self):
        """A failed start used to be refused for good while the worker lived on (review L4)."""
        class Broken:
            def start(self):
                raise RuntimeError("licence server unavailable")

            def stop(self):
                pass

        backend = self.make_backend([Broken(), self.session], start_attempts=1)
        with self.assertRaises(RuntimeError):
            backend._get_session()
        with self.assertRaises(SessionInvalidError) as caught:
            backend._get_session()
        self.assertGreater(caught.exception.details["retry_after_seconds"], 0)
        self.assertEqual(backend.session_start_seconds, 0.0)
        backend._start_failed_at -= backend.start_retry_cooldown + 1  # the cooldown is over
        self.assertGreater(backend.session_start_seconds, 0.0)
        self.assertIs(backend._get_session(), self.session)
        self.assertIsNone(backend._start_error)

    def test_a_working_directory_held_by_another_service_is_not_retried(self):
        from codev_mcp.com_session import WorkingDirectoryInUseError

        class Refused:
            def start(self):
                raise WorkingDirectoryInUseError("Another codev-mcp session is using this working directory.",
                                                 details={"reason": "working_directory_in_use"})

            def stop(self):
                pass

        backend = self.make_backend([Refused(), self.session], start_attempts=3)
        backend.start_attempts = 3
        with unittest.mock.patch("codev_mcp.com_backend.time.sleep") as sleep:
            with self.assertRaises(WorkingDirectoryInUseError) as caught:
                backend.open_lens(str(self.lens_path))
        sleep.assert_not_called()
        info = caught.exception.to_info()  # reaches the client as not_ready with its details, not session_invalid
        self.assertEqual(info.kind.value, "not_ready")
        self.assertEqual(info.details["reason"], "working_directory_in_use")
        self.assertIsNone(backend._start_error)
        # Once the other service has released the directory, the next call starts normally.
        backend.open_lens(str(self.lens_path))
        self.assertIs(backend._session, self.session)

    def test_a_dead_session_is_replaced_by_a_fresh_one(self):
        replacement = FakeCodeVSession()
        self.configure_session(replacement)
        replacement.listing = replacement._build_listing()
        backend = self.backend
        backend.open_lens(str(self.lens_path))

        backend._session.engine_dead = True
        with unittest.mock.patch.object(
            backend, "_new_session", return_value=replacement
        ):
            session = backend._get_session()

        self.assertIs(session, replacement)
        self.assertEqual(backend.session_restarts, 1)
        self.assertEqual(backend.get_status().details["session_restarts"], 1)

    def test_the_open_lens_is_restored_from_its_checkpoint_after_a_rebuild(self):
        """The next lens call has to bring back the committed checkpoint."""
        replacement = FakeCodeVSession()
        self.configure_session(replacement)
        replacement.listing = replacement._build_listing()
        backend = self.backend
        backend.open_lens(str(self.lens_path))

        # A successful edit is what a rebuild must not lose.
        backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.5)])
        )
        backend._session.engine_dead = True
        revision_before = backend.get_status().details["committed_revision"]
        with unittest.mock.patch.object(backend, "_new_session", return_value=replacement):
            lens = backend.get_lens()
        details = backend.get_status().details
        self.assertGreater(revision_before, 0)
        self.assertIn("revision-000001.len", str(details["checkpoint_path"]))

        self.assertTrue(
            any("revision-000001.len" in text for text in replacement.commands),
            f"the committed checkpoint was not restored: {replacement.commands}",
        )
        self.assertTrue(
            any(text.startswith("res ") for text in replacement.commands),
            f"no lens file was loaded into the rebuilt session: {replacement.commands}",
        )
        self.assertAlmostEqual(lens.surfaces[1].thickness, 9.5)
        self.assertEqual(backend.get_status().details["lens_state"], "ready")
        self.assertEqual(backend.get_status().details["recovery_count"], 1)

    def test_a_rebuild_without_an_open_lens_does_not_reload(self):
        replacement = FakeCodeVSession()
        self.configure_session(replacement)
        replacement.listing = replacement._build_listing()
        backend = ComBackend(working_directory=self.working, session=replacement)
        backend._session.engine_dead = True
        with unittest.mock.patch.object(backend, "_new_session", return_value=replacement):
            with self.assertRaises(SessionInvalidError):
                backend.get_lens()
        self.assertFalse(any(text.startswith("res ") for text in replacement.commands))
        self.assertEqual(backend.get_status().details["lens_state"], "empty")

class CommittedDrift(ComBackendTestCase):
    """A batch never folds a lens that left its committed revision into the next one (review M6)."""

    def test_an_update_on_a_lens_that_drifted_is_refused(self):
        self.backend.open_lens(str(self.lens_path))
        self.session.surfaces[2]["thickness"] = 7.5  # changed behind the service's back
        sent = len(self.session.commands)
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.0)]))
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("committed state", result.outcomes[0].rejected_reason)
        self.assertFalse(result.session_valid)
        self.assertFalse([command for command in self.session.commands[sent:]
                          if command.lower().startswith("thi")])
        self.assertEqual(self.backend.get_status().details["lens_state"], "invalid")

    def test_a_field_set_on_a_lens_that_drifted_is_refused(self):
        from codev_mcp.models import FieldSetReplacement

        self.backend.open_lens(str(self.lens_path))
        self.session.surfaces[2]["thickness"] = 7.5
        with self.assertRaises(SessionInvalidError):
            self.backend.update_lens(UpdateRequest(field_set=FieldSetReplacement(
                fields=[{"y_angle": 0.0}, {"y_angle": 5.0}])))
        self.assertEqual(self.backend.get_status().details["lens_state"], "invalid")

    def test_an_unchanged_lens_still_updates(self):
        self.backend.open_lens(str(self.lens_path))
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=9.0)]))
        self.assertTrue(result.outcomes[0].applied)
        self.assertTrue(result.session_valid)


class SnapshotReadErrors(ComBackendTestCase):
    def test_an_empty_value_from_codev_is_a_checkpoint_error(self):
        """normalise_number raised a bare ValueError, which surfaced as internal (review L9)."""
        from codev_mcp.checkpoints import CheckpointError

        self.backend.open_lens(str(self.lens_path))
        original = self.session._evaluate_body
        self.session._evaluate_body = lambda body: "" if body.startswith("THI S1") else original(body)
        with self.assertRaises(CheckpointError) as caught:
            read_snapshot(self.session, self.backend._require_lens(), self.backend._listing)
        self.assertIn("not a number", caught.exception.details["error"])

class SessionStartBudget(ComBackendTestCase):
    """The worker tells the client how long a call that starts CODE V may take (review H1)."""

    def test_the_budget_is_reported_until_the_session_runs(self):
        from codev_mcp.com_session import session_start_budget_seconds

        backend = ComBackend(working_directory=self.working, session_factory=lambda: self.session)
        self.assertEqual(backend.session_start_seconds, session_start_budget_seconds())
        backend._get_session()
        self.assertEqual(backend.session_start_seconds, 0.0)
        backend.close_session()
        self.assertEqual(backend.session_start_seconds, 0.0)  # a closed service never starts again

    def test_a_dead_engine_brings_the_budget_back(self):
        backend = ComBackend(working_directory=self.working, session=self.session)
        self.assertEqual(backend.session_start_seconds, 0.0)
        self.session.engine_dead = True
        self.assertGreater(backend.session_start_seconds, 0.0)

    def test_the_budget_covers_a_lock_wait_and_every_attempt(self):
        from codev_mcp import com_session

        budget = com_session.session_start_budget_seconds()
        self.assertGreaterEqual(
            budget,
            com_session.STARTUP_LOCK_WAIT_SECONDS
            + com_session.START_ATTEMPTS * com_session.STARTUP_TIMEOUT_MS / 1000,
        )

    def test_a_backend_with_a_session_factory_leaves_leftovers_alone(self):
        backend = ComBackend(working_directory=self.working, session_factory=lambda: self.session)
        self.assertEqual(backend.release_leftovers(), [])


if __name__ == "__main__":
    unittest.main()
