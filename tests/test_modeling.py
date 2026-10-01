"""Typed M3 command construction and native-number mapping."""

import unittest

from pydantic import ValidationError

from codev_mcp.errors import ParameterError, UnsupportedError
from codev_mcp.modeling import create_commands, plan_structure
from codev_mcp.models import CreateLensRequest, StructureRequest


class CreateCommands(unittest.TestCase):
    def request(self, **changes):
        payload = dict(
            aperture_value=10,
            wavelengths_nm=[550],
            fields=[{"x_angle": 0, "y_angle": 0}],
            surfaces=[
                {"radius": 50, "thickness": 5, "glass": "BK7_SCHOTT"},
                {"radius": -50, "thickness": 20, "glass": None},
            ],
            stop_surface=1,
        )
        payload.update(changes)
        return CreateLensRequest.model_validate(payload)

    def test_commands_are_literal_and_radius_mode_is_explicit(self):
        self.assertEqual(create_commands(self.request()), [
            "len", "rdm", "dim M", "wl 550", "epd 10", "xan 0", "yan 0",
            "ins si 50 5 BK7_SCHOTT", "ins si -50 20", "sto s1",
        ])

    def test_glass_injection_and_nonfinite_numbers_are_refused(self):
        with self.assertRaises(ParameterError):
            create_commands(self.request(surfaces=[
                {"radius": 50, "thickness": 5, "glass": "BK7;DEL S1"},
                {"radius": -50, "thickness": 20},
            ]))
        with self.assertRaises(ParameterError):
            create_commands(self.request(aperture_value=float("nan")))

    def test_invalid_structure_is_rejected_by_the_model(self):
        with self.assertRaises(ValidationError):
            self.request(surfaces=[{"radius": 50, "thickness": 5, "glass": "BK7"}])
        with self.assertRaises(ValidationError):
            self.request(surfaces=[
                {"radius": 50, "thickness": 5},
                {"radius": -50, "thickness": 20, "glass": "BK7"},
            ])

    def test_na_outside_verified_write_range_is_refused(self):
        with self.assertRaises(UnsupportedError):
            create_commands(self.request(aperture_kind="na", aperture_value=1.1))

    def test_native_normalization_inputs_are_rejected_before_commands(self):
        with self.assertRaisesRegex(ParameterError, "fully qualified"):
            create_commands(self.request(surfaces=[
                {"radius": 50, "thickness": 5, "glass": "BK7"},
                {"radius": -50, "thickness": 20},
            ]))
        with self.assertRaisesRegex(ParameterError, "strictly descending"):
            create_commands(self.request(wavelengths_nm=[550, 650]))
        with self.assertRaisesRegex(ParameterError, "strictly descending"):
            create_commands(self.request(wavelengths_nm=[550, 550]))


class StructureMapping(unittest.TestCase):
    def request(self, operations):
        return StructureRequest.model_validate({"operations": operations})

    def test_insert_delete_and_stop_use_the_current_numbering(self):
        steps = plan_structure(self.request([
            {"kind": "insert_sphere", "before_surface": 2,
             "radius": None, "thickness": 1, "glass": None},
            {"kind": "delete_surface", "surface": 3},
            {"kind": "set_stop", "surface": 2},
        ]), surface_count=4, stop_surface=1)
        self.assertEqual(steps[0].commands, ["ins s2 0 1"])
        self.assertEqual(steps[0].result.old_to_new, {0: 0, 1: 1, 2: 3, 3: 4})
        self.assertEqual(steps[0].surface_count, 5)
        self.assertEqual(steps[1].result.old_to_new, {0: 0, 1: 1, 2: 2, 4: 3})
        self.assertEqual(steps[1].result.stop_surface, 1)
        self.assertEqual(steps[2].result.stop_surface, 2)

    def test_deleting_the_stop_requires_explicit_replacement(self):
        with self.assertRaises(ParameterError):
            plan_structure(self.request([{"kind": "delete_surface", "surface": 1}]),
                           surface_count=4, stop_surface=1)
        steps = plan_structure(self.request([{"kind": "delete_surface", "surface": 1,
                                              "new_stop_surface": 1}]),
                               surface_count=4, stop_surface=1)
        self.assertEqual(steps[0].commands, ["del s1", "sto s1"])

    def test_object_image_and_extra_command_fields_are_refused(self):
        with self.assertRaises(ParameterError):
            plan_structure(self.request([{"kind": "delete_surface", "surface": 3}]),
                           surface_count=4, stop_surface=1)
        with self.assertRaises(ValidationError):
            self.request([{"kind": "set_stop", "surface": 2, "command": "DEL S1"}])

    def test_insert_requires_catalog_qualified_glass(self):
        with self.assertRaisesRegex(ParameterError, "fully qualified"):
            plan_structure(self.request([
                {"kind": "insert_sphere", "before_surface": 2,
                 "radius": 20, "thickness": 1, "glass": "BK7"},
            ]), surface_count=4, stop_surface=1)
