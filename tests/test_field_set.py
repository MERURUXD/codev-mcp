"""Field set replacement (E3), image-height normalisation and the PIM solve on new lenses (E4)."""

from __future__ import annotations

import json
import math
import unittest
import unittest.mock
from pathlib import Path

from codev_mcp import fieldset
from codev_mcp.checkpoints import CheckpointVerificationError
from codev_mcp.com_backend import ComBackend
from codev_mcp.errors import ParameterError, UnsupportedError
from codev_mcp.models import (
    CreateLensRequest,
    FieldSetReplacement,
    LensField,
    ParameterEdit,
    StructureRequest,
    UpdateRequest,
)
from codev_mcp.simulated import SimulatedBackend
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession


def current_fields():
    return [
        LensField(number=1, x_angle=0, y_angle=0, weight=1, vux=0, vlx=0, vuy=0, vly=0),
        LensField(number=2, x_angle=0, y_angle=10, weight=2, vux=0.05, vlx=0, vuy=0.2, vly=0.3),
        LensField(number=3, x_angle=0, y_angle=14, weight=3, vux=0, vlx=0, vuy=0.4, vly=0.4),
    ]


def replacement(*angles, **extra):
    return FieldSetReplacement.model_validate(
        {"fields": [{"y_angle": angle, **extra} for angle in angles]}
    )


class ResolveFieldSet(unittest.TestCase):
    def test_kept_fields_keep_weight_and_vignetting_and_new_ones_start_neutral(self):
        target = fieldset.resolve_field_set(replacement(0, 5, 10, 14, 18), current_fields())
        self.assertEqual([item.y_angle for item in target], [0, 5, 10, 14, 18])
        self.assertEqual([item.weight for item in target], [1, 2, 3, 1, 1])
        self.assertEqual([item.vuy for item in target], [0, 0.2, 0.4, 0, 0])
        self.assertEqual(target[1].vux, 0.05)
        self.assertEqual((target[4].vux, target[4].vly), (0, 0))

    def test_given_values_replace_the_kept_ones(self):
        spec = FieldSetReplacement.model_validate({"fields": [
            {"y_angle": 0}, {"y_angle": 8, "weight": 5, "vuy": 0.1, "x_angle": 1}]})
        target = fieldset.resolve_field_set(spec, current_fields())
        self.assertEqual((target[1].weight, target[1].vuy, target[1].x_angle), (5, 0.1, 1))
        self.assertEqual(target[1].vly, 0.3)  # not given: kept

    def test_limits_are_enforced(self):
        for payload in ({"fields": [{"y_angle": 89.5}]}, {"fields": [{"y_angle": 0, "x_angle": -90}]},
                        {"fields": [{"y_angle": 0, "vuy": 1.0}]}, {"fields": [{"y_angle": 0, "weight": -1}]},
                        {"fields": [{"y_angle": float("nan")}]}):
            with self.assertRaises(ParameterError, msg=payload):
                fieldset.resolve_field_set(FieldSetReplacement.model_validate(payload), current_fields())

    def test_the_model_rejects_too_many_fields_unknown_keys_and_an_empty_set(self):
        with self.assertRaises(ValueError):
            replacement(*range(11))
        with self.assertRaises(ValueError):
            FieldSetReplacement.model_validate({"fields": []})
        with self.assertRaises(ValueError):
            FieldSetReplacement.model_validate({"fields": [{"y_angle": 1, "command": "x"}]})

    def test_commands_write_the_angle_vectors_and_only_what_differs(self):
        target = fieldset.resolve_field_set(replacement(0, 5, 10, 14, 18), current_fields())
        self.assertEqual(fieldset.field_set_commands(target, current_fields()),
                         ["XAN 0 0 0 0 0", "YAN 0 5 10 14 18"])
        spec = FieldSetReplacement.model_validate({"fields": [
            {"y_angle": 0}, {"y_angle": 5, "weight": 4}, {"y_angle": 9, "vuy": 0.45},
            {"y_angle": 14, "weight": 2, "vly": 0.1}]})
        commands = fieldset.field_set_commands(
            fieldset.resolve_field_set(spec, current_fields()), current_fields())
        self.assertEqual(commands[2:], ["WTF F2 4", "VUY F3 0.45", "WTF F4 2", "VLY F4 0.1"])

    def test_differences_name_the_field_and_item(self):
        target = fieldset.resolve_field_set(replacement(0, 5), current_fields())
        actual = [LensField(**item.model_dump()) for item in target]
        self.assertEqual(fieldset.field_set_differences(target, actual), [])
        actual[1].weight = 9
        self.assertIn("field 2 weight", fieldset.field_set_differences(target, actual)[0])
        self.assertIn("field count", fieldset.field_set_differences(target, actual[:1])[0])


class UpdateRequestShape(unittest.TestCase):
    def test_edits_and_a_field_set_cannot_be_mixed(self):
        with self.assertRaises(ValueError):
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="thickness", value=1)],
                          field_set=replacement(0))

    def test_an_empty_update_is_refused(self):
        with self.assertRaises(ValueError):
            UpdateRequest()

class ImageHeightNormalisation(unittest.TestCase):
    def test_relative_fields_follow_the_tangent_of_the_maximum_angle(self):
        angles = fieldset.relative_field_angles(23.5, [0, 0.5, 0.7, 0.85, 1])
        self.assertAlmostEqual(angles[0], 0)
        self.assertAlmostEqual(angles[4], 23.5)
        self.assertAlmostEqual(angles[1], math.degrees(math.atan(0.5 * math.tan(math.radians(23.5)))))
        # The linear reading differs by about half a degree at 0.5.
        self.assertGreater(abs(angles[1] - 11.75), 0.2)
        heights = fieldset.relative_heights(23.5, angles)
        for want, got in zip([0, 0.5, 0.7, 0.85, 1], heights):
            self.assertAlmostEqual(want, got)

    def test_the_angle_mode_is_linear(self):
        self.assertEqual(fieldset.relative_field_angles(20, [0, 0.5, 1], "angle"), [0, 10, 20])

    def test_bad_input_is_refused(self):
        for args in ((0, [0.5]), (90, [0.5]), (20, []), (20, [1.5]), (20, [0.5], "cube"),
                     (20, [0.1] * 11)):
            with self.assertRaises(ParameterError, msg=args):
                fieldset.relative_field_angles(*args)

    def test_the_report_names_the_convention_and_feeds_update_lens(self):
        report = fieldset.normalisation_report(23.5, [0, 0.5, 1], weights=[1, 2, 1])
        self.assertIn("atan(relative * tan", report["convention"])
        self.assertEqual([item["weight"] for item in report["field_set"]["fields"]], [1, 2, 1])
        FieldSetReplacement.model_validate(report["field_set"])
        self.assertEqual(report["linear_angle_for_comparison"], [0, 11.75, 23.5])
        with self.assertRaises(ParameterError):
            fieldset.normalisation_report(23.5, [0, 1], weights=[1])

    def test_the_command_line_writes_a_new_file_and_refuses_to_overwrite(self):
        temp = workspace_temp_directory("fieldset-cli")
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "fields.json"
        with unittest.mock.patch("sys.stdout"), unittest.mock.patch("sys.stderr"):
            self.assertEqual(fieldset.main(["--max-angle", "23.5", "--relative", "0", "1",
                                            "--output", str(target)]), 0)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["field_set"]["fields"]), 2)
            self.assertEqual(fieldset.main(["--max-angle", "23.5", "--relative", "0", "1",
                                            "--output", str(target)]), 1)
            self.assertEqual(fieldset.main(["--max-angle", "95", "--relative", "1"]), 1)


class FieldSetCase(unittest.TestCase):
    session_kwargs: dict = {}

    def setUp(self) -> None:
        temp = workspace_temp_directory("field-set")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lens_path = self.root / "dbgauss.len"
        self.lens_path.write_text("! lens placeholder\n", encoding="utf-8")
        self.session = FakeCodeVSession(
            fields=[
                {"x": 0.0, "y": 0.0, "weight": 1.0},
                {"x": 0.0, "y": 10.0, "weight": 2.0, "vuy": 0.2, "vly": 0.3},
                {"x": 0.0, "y": 14.0, "weight": 3.0, "vuy": 0.4, "vly": 0.4},
            ],
            solves={3: "PIM"},
            **self.session_kwargs,
        )
        self.session.listing = self.session._build_listing()
        self.backend = ComBackend(working_directory=self.root / "run", session=self.session)
        guard = unittest.mock.patch.object(
            self.backend, "_new_session", side_effect=AssertionError("a test tried to start CODE V"))
        guard.start()
        self.addCleanup(guard.stop)
        self.backend.open_lens(str(self.lens_path))

    def revision(self) -> int:
        return self.backend.get_status().details["committed_revision"]


class ComFieldSet(FieldSetCase):
    def test_three_fields_become_five_and_the_solve_stays(self):
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5, 10, 14, 18)))
        self.assertTrue(result.field_set.applied, result.warnings)
        self.assertEqual(len(result.field_set.previous_fields), 3)
        self.assertEqual([item.y_angle for item in result.field_set.fields], [0, 5, 10, 14, 18])
        self.assertEqual([item.weight for item in result.field_set.fields], [1, 2, 3, 1, 1])
        self.assertEqual([item.vuy for item in result.field_set.fields], [0, 0.2, 0.4, 0, 0])
        self.assertEqual(self.revision(), 1)
        lens = self.backend.get_lens()
        self.assertEqual(len(lens.fields), 5)
        self.assertEqual(self.session.solves, {3: "PIM"})
        self.assertEqual(self.backend._last_known_snapshot.solves, self.backend._current_checkpoint().snapshot.solves)
        self.assertIn("XAN 0 0 0 0 0; YAN 0 5 10 14 18", result.warnings[-1])

    def test_the_committed_revision_restores_to_the_five_field_lens(self):
        self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5, 10, 14, 18)))
        self.session.engine_dead = True  # the next call rebuilds from the checkpoint
        fresh = FakeCodeVSession(fields=[{"x": 0.0, "y": 0.0, "weight": 1.0}])
        fresh.listing = fresh._build_listing()
        with unittest.mock.patch.object(self.backend, "_new_session", return_value=fresh):
            lens = self.backend.get_lens()
        self.assertEqual([item.y_angle for item in lens.fields], [0, 5, 10, 14, 18])

    def test_a_shorter_set_drops_the_tail(self):
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 12)))
        self.assertTrue(result.field_set.applied, result.warnings)
        self.assertEqual(len(self.backend.get_lens().fields), 2)
        self.assertEqual(self.backend.get_lens().fields[1].vuy, 0.2)

    def test_given_weights_and_factors_are_written_and_read_back(self):
        spec = FieldSetReplacement.model_validate({"fields": [
            {"y_angle": 0}, {"y_angle": 10, "weight": 6, "vuy": 0.25},
            {"y_angle": 14}, {"y_angle": 20, "weight": 2, "vly": 0.5}]})
        result = self.backend.update_lens(UpdateRequest(field_set=spec))
        self.assertTrue(result.field_set.applied, result.warnings)
        lens = self.backend.get_lens()
        self.assertEqual((lens.fields[1].weight, lens.fields[1].vuy, lens.fields[1].vly), (6, 0.25, 0.3))
        self.assertEqual((lens.fields[3].weight, lens.fields[3].vly), (2, 0.5))

    def test_a_failure_in_the_middle_rolls_everything_back(self):
        calls = {"count": 0}

        def fail_second(name):
            if name == "before_field_set_command":
                calls["count"] += 1
                if calls["count"] == 2:
                    raise RuntimeError("engine hiccup")

        self.backend.fault_hook = fail_second
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5, 10, 14, 18)))
        self.assertFalse(result.field_set.applied)
        self.assertTrue(result.rolled_back)
        self.assertIn("engine hiccup", result.field_set.rejected_reason)
        self.assertEqual(self.revision(), 0)
        lens = self.backend.get_lens()
        self.assertEqual([item.y_angle for item in lens.fields], [0, 10, 14])
        self.assertEqual(self.session.solves, {3: "PIM"})

    def test_a_write_that_did_not_take_effect_is_caught_and_rolled_back(self):
        original = self.session.command

        def ignore_weights(text, *args, **kwargs):
            if text.startswith("WTF"):
                return "Command End:\r\n"
            return original(text, *args, **kwargs)

        spec = FieldSetReplacement.model_validate({"fields": [
            {"y_angle": 0}, {"y_angle": 10, "weight": 6}]})
        with unittest.mock.patch.object(self.session, "command", ignore_weights):
            result = self.backend.update_lens(UpdateRequest(field_set=spec))
        self.assertFalse(result.field_set.applied)
        self.assertTrue(result.rolled_back)
        self.assertIn("field 2 weight", result.field_set.rejected_reason)
        self.assertEqual(self.revision(), 0)
        self.assertEqual(len(self.backend.get_lens().fields), 3)

    def test_an_unexpected_change_elsewhere_fails_the_replacement(self):
        original = self.session.command

        def also_move_a_thickness(text, *args, **kwargs):
            output = original(text, *args, **kwargs)
            if text.startswith("YAN"):
                original("THI S1 9.75")
            return output

        with unittest.mock.patch.object(self.session, "command", also_move_a_thickness):
            result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5, 10, 14)))
        self.assertFalse(result.field_set.applied)
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.revision(), 0)

    def test_an_out_of_range_angle_is_refused_before_any_command(self):
        before = len(self.session.commands)
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 95)))
        self.assertFalse(result.field_set.applied)
        self.assertTrue(result.rolled_back)
        self.assertIn("y_angle", result.field_set.rejected_reason)
        self.assertEqual(self.revision(), 0)
        self.assertEqual(len(self.session.commands), before)

    def test_a_lens_with_object_height_fields_is_refused(self):
        with unittest.mock.patch.object(self.backend, "_field_kind", return_value="object_height"):
            result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5)))
        self.assertFalse(result.field_set.applied)
        self.assertIn("object_height", result.field_set.rejected_reason)

class ComFieldSetZoom(FieldSetCase):
    session_kwargs = {"zoom_positions": 2}

    def test_a_multi_zoom_lens_is_refused(self):
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5)))
        self.assertFalse(result.field_set.applied)
        self.assertIn("single-zoom", result.field_set.rejected_reason)
        self.assertEqual(self.revision(), 0)


class SimulatedFieldSet(unittest.TestCase):
    def setUp(self) -> None:
        temp = workspace_temp_directory("sim-field-set")
        self.addCleanup(temp.cleanup)
        self.backend = SimulatedBackend(working_directory=temp.name)
        lens_path = Path(temp.name) / "dbgauss.len"
        lens_path.write_text("! lens placeholder\n", encoding="utf-8")
        self.backend.open_lens(str(lens_path))

    def test_the_simulated_backend_applies_the_same_rules(self):
        before = self.backend.get_lens()
        self.assertEqual(len(before.fields), 3)
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 5, 10, 14, 18)))
        self.assertTrue(result.field_set.applied)
        self.assertEqual(result.source.value, "simulated")
        lens = self.backend.get_lens()
        self.assertEqual([item.y_angle for item in lens.fields], [0, 5, 10, 14, 18])
        self.assertEqual([item.vuy for item in lens.fields][:3], [item.vuy for item in before.fields])
        self.assertEqual(lens.fields[4].weight, 1)

    def test_the_simulated_backend_refuses_the_same_input(self):
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 95)))
        self.assertFalse(result.field_set.applied)
        self.assertTrue(result.rolled_back)


PIM_REQUEST = CreateLensRequest.model_validate({
    "aperture_value": 12,
    "wavelengths_nm": [550],
    "fields": [{"x_angle": 0, "y_angle": 0}],
    "surfaces": [
        {"radius": 40, "thickness": 5, "glass": "BK7_SCHOTT"},
        {"radius": -40, "thickness": 30},
    ],
    "stop_surface": 1,
    "image_solve": "pim",
})


class CreateWithPimSolve(unittest.TestCase):
    def setUp(self) -> None:
        temp = workspace_temp_directory("create-pim")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = FakeCodeVSession()
        self.backend = ComBackend(working_directory=self.root, session=self.session)
        guard = unittest.mock.patch.object(
            self.backend, "_new_session", side_effect=AssertionError("a test tried to start CODE V"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_the_command_list_ends_with_the_solve(self):
        from codev_mcp.modeling import create_commands

        self.assertEqual(create_commands(PIM_REQUEST)[-1], "pim yes")
        plain = PIM_REQUEST.model_copy(update={"image_solve": None})
        self.assertNotIn("pim yes", create_commands(plain))

    def test_a_created_lens_carries_the_solve_in_its_checkpoint(self):
        self.backend.create_lens(PIM_REQUEST)
        self.assertEqual(self.session.solves, {2: "PIM"})
        snapshot = self.backend._current_checkpoint().snapshot
        self.assertEqual(snapshot.solves, ["PIM"])
        self.assertIn("PIM", snapshot.variable_controls["2"].values())
        self.assertEqual(self.backend.get_status().details["committed_revision"], 0)

    def test_the_solved_thickness_cannot_be_edited_afterwards(self):
        self.backend.create_lens(PIM_REQUEST)
        result = self.backend.update_lens(UpdateRequest(
            edits=[ParameterEdit(surface=2, parameter="thickness", value=25)]))
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("PIM", result.outcomes[0].rejected_reason)

    def test_the_first_thickness_and_the_field_set_stay_editable(self):
        self.backend.create_lens(PIM_REQUEST)
        result = self.backend.update_lens(UpdateRequest(
            edits=[ParameterEdit(surface=1, parameter="thickness", value=6)]))
        self.assertTrue(result.outcomes[0].applied, result.warnings)
        result = self.backend.update_lens(UpdateRequest(field_set=replacement(0, 3, 6)))
        self.assertTrue(result.field_set.applied, result.warnings)
        self.assertEqual(self.session.solves, {2: "PIM"})

    def test_structure_editing_stays_limited_to_lenses_without_solves(self):
        self.backend.create_lens(PIM_REQUEST)
        with self.assertRaises(UnsupportedError):
            self.backend.edit_lens_structure(StructureRequest.model_validate({
                "operations": [{"kind": "set_stop", "surface": 1}]}))

    def test_a_solve_that_did_not_take_effect_fails_the_creation(self):
        original = self.session.command_raw

        def ignore_pim(text):
            if text.strip().lower().startswith("pim"):
                return "Error:   Invalid data\r\nCommand End:\r\n"
            return original(text)

        with unittest.mock.patch.object(self.session, "command_raw", ignore_pim):
            with self.assertRaises(CheckpointVerificationError) as caught:
                self.backend.create_lens(PIM_REQUEST)
        self.assertIn("solve", str(caught.exception.details))

    def test_a_request_without_the_solve_is_unchanged(self):
        plain = PIM_REQUEST.model_copy(update={"image_solve": None})
        self.backend.create_lens(plain)
        self.assertEqual(self.session.solves, {})

    def test_an_unknown_solve_name_is_refused_by_the_model(self):
        with self.assertRaises(ValueError):
            CreateLensRequest.model_validate({**PIM_REQUEST.model_dump(), "image_solve": "cuy"})


class SimulatedCreateWithPimSolve(unittest.TestCase):
    def test_the_simulated_lens_records_the_solve_and_says_it_did_not_calculate(self):
        temp = workspace_temp_directory("sim-create-pim")
        self.addCleanup(temp.cleanup)
        backend = SimulatedBackend(working_directory=temp.name)
        lens = backend.create_lens(PIM_REQUEST)
        self.assertTrue(any("PIM" in warning for warning in lens.warnings))
        result = backend.update_lens(UpdateRequest(
            edits=[ParameterEdit(surface=2, parameter="thickness", value=25)]))
        self.assertFalse(result.outcomes[0].applied)
        self.assertIn("PIM", result.outcomes[0].rejected_reason)


if __name__ == "__main__":
    unittest.main()
