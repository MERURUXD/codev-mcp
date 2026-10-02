"""Bounded model construction and structural rollback against the fake COM session."""

from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from codev_mcp.com_backend import ComBackend
from codev_mcp.errors import ParameterError, SessionInvalidError, UnsupportedError
from codev_mcp.models import CreateLensRequest, StructureRequest
from codev_mcp.modeling import plan_structure, structure_differences
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession


CREATE = CreateLensRequest.model_validate({
    "aperture_value": 12,
    "wavelengths_nm": [550],
    "fields": [{"x_angle": 0, "y_angle": 0}],
    "surfaces": [
        {"radius": 40, "thickness": 5, "glass": "BK7_SCHOTT"},
        {"radius": -40, "thickness": 10},
    ],
    "stop_surface": 1,
})


class ComModeling(unittest.TestCase):
    def setUp(self) -> None:
        temp = workspace_temp_directory("com-modeling")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = FakeCodeVSession()
        self.backend = ComBackend(working_directory=self.root, session=self.session)
        guard = patch.object(self.backend, "_new_session",
                             side_effect=AssertionError("unexpected real COM session"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_create_insert_delete_and_stop_commit_one_revision(self):
        lens = self.backend.create_lens(CREATE)
        self.assertEqual(len(lens.surfaces), 4)
        self.assertEqual(self.backend.get_status().details["committed_revision"], 0)
        result = self.backend.edit_lens_structure(StructureRequest.model_validate({
            "operations": [
                {"kind": "insert_sphere", "before_surface": 2,
                 "radius": None, "thickness": 1},
                {"kind": "delete_surface", "surface": 3},
                {"kind": "set_stop", "surface": 2},
            ],
        }))
        self.assertTrue(result.applied, result.warnings)
        self.assertEqual(result.lens.stop_surface, 2)
        self.assertEqual(len(result.lens.surfaces), 4)
        self.assertEqual(result.steps[0].old_to_new[2], 3)
        self.assertEqual(self.backend.get_status().details["committed_revision"], 1)
        self.assertTrue(Path(self.backend.get_status().details["checkpoint_path"]).exists())

    def test_structure_mapping_rejects_changed_variable_controls(self):
        self.backend.create_lens(CREATE)
        before = self.backend._current_checkpoint().snapshot
        after = deepcopy(before)
        after.variable_controls["1"]["CCY"] = "0"
        step = plan_structure(StructureRequest.model_validate({
            "operations": [{"kind": "set_stop", "surface": 1}],
        }), surface_count=before.surface_count, stop_surface=before.stop_surface)[0]
        self.assertIn("surface 1 variable controls",
                      structure_differences(before, after, step))

    def test_committed_create_never_performs_a_third_read(self):
        original = self.backend._read_lens
        reads = 0

        def read_twice_only():
            nonlocal reads
            reads += 1
            if reads > 2:
                raise RuntimeError("post-commit read failed")
            return original()

        with patch.object(self.backend, "_read_lens", side_effect=read_twice_only):
            lens = self.backend.create_lens(CREATE)
        self.assertEqual(reads, 2)
        self.assertEqual(len(lens.surfaces), 4)
        details = self.backend.get_status().details
        self.assertEqual(details["committed_revision"], 0)
        self.assertEqual(details["lens_state"], "ready")
        self.assertTrue(Path(details["checkpoint_path"]).exists())

    def test_normalization_inputs_rejected_before_com_and_lens_identity(self):
        for change in (
            {"surfaces": [{"radius": 40, "thickness": 5, "glass": "BK7"},
                          {"radius": -40, "thickness": 10}]},
            {"wavelengths_nm": [550, 650]},
        ):
            with self.subTest(change=change):
                request = CreateLensRequest.model_validate({**CREATE.model_dump(), **change})
                with self.assertRaises(ParameterError):
                    self.backend.create_lens(request)
                self.assertEqual(self.session.commands, [])
                self.assertIsNone(self.backend.get_status().details["lens_id"])

    def test_mid_batch_fault_restores_exact_starting_state(self):
        self.backend.create_lens(CREATE)
        before = self.backend.get_lens()
        counter = 0

        def fail_second(name: str) -> None:
            nonlocal counter
            if name == "before_structure_command":
                counter += 1
                if counter == 2:
                    raise RuntimeError("injected second command failure")

        self.backend.fault_hook = fail_second
        result = self.backend.edit_lens_structure(StructureRequest.model_validate({
            "operations": [
                {"kind": "insert_sphere", "before_surface": 2,
                 "radius": 25, "thickness": 1},
                {"kind": "set_stop", "surface": 2},
            ],
        }))
        self.assertFalse(result.applied)
        self.assertTrue(result.rolled_back, result.warnings)
        self.assertTrue(result.session_valid)
        self.assertEqual(self.backend.get_status().details["committed_revision"], 0)
        self.assertEqual(self.backend.get_lens().surfaces, before.surfaces)

    def test_external_lens_is_not_trusted_for_structure(self):
        source = self.root / "input.len"
        source.write_text("fake lens", encoding="utf-8")
        self.backend.open_lens(str(source))
        with self.assertRaises(UnsupportedError):
            self.backend.edit_lens_structure(StructureRequest.model_validate({
                "operations": [{"kind": "set_stop", "surface": 1}],
            }))

    def test_create_fails_closed_on_checkpoint_failure(self):
        def fail_publish(name: str) -> None:
            if name == "before_model_publish":
                raise RuntimeError("injected checkpoint failure")

        self.backend.fault_hook = fail_publish
        with self.assertRaises(Exception):
            self.backend.create_lens(CREATE)
        # create_lens only runs on an empty session, so the failed one is dropped
        # and the service is empty again instead of invalid (review M8).
        details = self.backend.get_status().details
        self.assertTrue(details["session_valid"])
        self.assertEqual(details["lens_state"], "empty")
        self.assertFalse(self.session.started)
        self.backend.fault_hook = None
        replacement = FakeCodeVSession()
        with patch.object(self.backend, "_new_session", return_value=replacement):
            lens = self.backend.create_lens(CREATE)
        self.assertEqual(self.backend.get_status().details["committed_revision"], 0)
        self.assertTrue(lens.surfaces)

    def test_failed_restore_invalidates_session(self):
        self.backend.create_lens(CREATE)
        count = 0

        def fail_second(name: str) -> None:
            nonlocal count
            if name == "before_structure_command":
                count += 1
                if count == 2:
                    raise RuntimeError("injected write failure")

        self.backend.fault_hook = fail_second
        original = self.session.command

        def fail_restore(text: str, *args, **kwargs):
            if text.lower().startswith("res ") and "restore-points" in text.lower():
                return "Error: restore blocked\r\n"
            return original(text, *args, **kwargs)

        with patch.object(self.session, "command", side_effect=fail_restore):
            result = self.backend.edit_lens_structure(StructureRequest.model_validate({
                "operations": [
                    {"kind": "insert_sphere", "before_surface": 2,
                     "radius": None, "thickness": 1},
                    {"kind": "set_stop", "surface": 2},
                ],
            }))
        self.assertFalse(result.applied)
        self.assertFalse(result.rolled_back)
        self.assertFalse(result.session_valid)
        with self.assertRaises(SessionInvalidError):
            self.backend.edit_lens_structure(StructureRequest.model_validate({
                "operations": [{"kind": "set_stop", "surface": 1}],
            }))


if __name__ == "__main__":
    unittest.main()
