"""D4 start-point scaling: plan, transaction checks and the reopened result."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codev_mcp.models import LensData
from codev_mcp.scale import native_command, plan_scaling, run_scale
from tests.test_evaluation import dbgauss_lens

EFL, IMAGE_DISTANCE = 100.000123456789, 61.234567


def lens_payload() -> dict:
    lens = dbgauss_lens()
    lens.update(source="codev", dimension_code=2, zoom_position=1, aperture_usage="default_only",
                aperture={"kind": "epd", "value": 50.0, "units": "mm"})
    for surface in lens["surfaces"]:
        surface.update(apertures=[], aperture_data_complete=True)
    for item in lens["fields"]:
        item.update(vux=0.0, vlx=0.0, vuy=0.0, vly=0.0)
    return LensData.model_validate(lens).model_dump(mode="json")


class ScalingClient:
    """An ideal lens: every length scales with the pupil, PIM follows the focus."""

    efl_error = 0.0
    roll_back = False
    confirm_cleanup = True

    def __init__(self, work, backend, timeout, logs):
        self.lens = None
        self.revision = 0
        self.task = None

    def scale(self):
        return self.lens["aperture"]["value"] / 50.0

    def call(self, name, arguments=None, timeout=None):
        arguments = arguments or {}
        if name == "open_lens":
            path = Path(arguments["path"])
            text = path.read_text(encoding="utf-8")
            self.lens = json.loads(text) if text.startswith("{") else lens_payload()
            return copy.deepcopy(self.lens), []
        if name == "get_lens":
            return copy.deepcopy(self.lens), []
        if name == "get_status":
            return {"details": {"lens_id": "lens", "committed_revision": self.revision}}, []
        if name == "update_lens":
            edits = arguments["request"]["edits"]
            if self.roll_back:
                return {"source": "codev", "rolled_back": True, "session_valid": True,
                        "outcomes": [{"applied": False} for _ in edits], "warnings": ["refused"]}, []
            for edit in edits:
                if edit["target"] == "aperture":
                    self.lens["aperture"]["value"] = float(f"{edit['value']:.12g}")
                else:
                    surface = self.lens["surfaces"][edit["surface"]]
                    surface[edit["parameter"]] = float(f"{edit['value']:.12g}")
            self.lens["surfaces"][11]["thickness"] = IMAGE_DISTANCE * self.scale()
            self.revision += 1
            return {"source": "codev", "rolled_back": False, "session_valid": True, "restore_point": "rp",
                    "outcomes": [{"applied": True} for _ in edits],
                    "warnings": ["CODE V re-derived the solved parameters: surface 11 thickness"]}, []
        if name == "run_analysis":
            self.task = arguments["request"]
            return {"task_id": "t1"}, []
        if name == "get_analysis":
            options = self.task["options"]
            k = self.scale()
            return {"source": "codev", "task": {
                "task_id": "t1", "kind": "first_order", "state": "succeeded", "source": "codev",
                "created_at": "now", "settings": options},
                "first_order": {"source": "codev", "units": "mm", "zoom_position": 1,
                                "effective_focal_length": EFL * k * (1 + (self.efl_error if self.revision else 0.0)),
                                "back_focal_length": 61.2346 * k, "f_number": 2.0000097,
                                "overall_length": 81.9134486 * k, "image_distance": IMAGE_DISTANCE * k,
                                "entrance_pupil_diameter": 50.0 * k, "raw_output": "items",
                                "precision_note": "string"}}, []
        if name == "save_lens_as":
            Path(arguments["path"]).write_text(json.dumps(self.lens), encoding="utf-8")
            return {"source": "codev", "path": arguments["path"], "overwritten": False}, []
        raise AssertionError(name)

    def close(self):
        return {"returncode": 0, "close_session": {"session_open": False,
                "details": {"cleanup_confirmed": self.confirm_cleanup}}}


class PlanTests(unittest.TestCase):
    def test_dbgauss_plan_leaves_pim_and_invariants(self):
        lens = lens_payload()
        edits, left = plan_scaling(lens, 0.5)
        self.assertEqual(len(edits), 19)
        commands = [native_command(item, lens) for item in edits]
        self.assertEqual(commands[:2], ["RDY S1 32.061725", "THI S1 4.561728"])
        self.assertEqual(commands[-1], "EPD 25")
        self.assertNotIn("THI S11", " ".join(commands))
        self.assertEqual([item.get("control") for item in left[:2]], ["infinite", "PIM"])

    def test_refuses_what_a_uniform_typed_scale_cannot_describe(self):
        cases = {
            "only native relation": lambda lens: lens.update(raw_listing=lens["raw_listing"].replace(
                "    PIM\n", "    PIM\n    CUY S5 UMY 0.1\n")),
            "pickups": lambda lens: lens.update(raw_listing=lens["raw_listing"].replace(
                "No pickups defined in system", "PICKUPS\n PIK RDY S1 RDY S3 1 0")),
            "special surface": lambda lens: lens.update(raw_listing=lens["raw_listing"].replace(
                "    5:        31.87654       13.765432         ", "    5:        31.87654       13.765432  ASP    ")),
            "single-zoom": lambda lens: lens.update(zoom_positions=2),
            "angle fields": lambda lens: lens.update(raw_listing=lens["raw_listing"].replace(
                "   XAN        0.00000", "   YOB        0.00000").replace("   YAN  ", "   XOB  ")),
            "curved image": lambda lens: lens["surfaces"][12].update(radius=-200.0, radius_is_infinite=False),
            "centred clear circle": lambda lens: lens["surfaces"][3]["apertures"].append(
                {"kind": "obscuration", "shape": "circular", "radius": 3.0, "x_decenter": 0.0,
                 "y_decenter": 0.0, "rotation_degrees": 0.0, "or_with_previous": False}),
            "LIS listing is missing": lambda lens: lens.update(raw_listing=None),
        }
        for message, change in cases.items():
            lens = lens_payload()
            change(lens)
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                plan_scaling(lens, 0.5)
        with self.assertRaises(ValueError):
            plan_scaling(lens_payload(), 0.0)

    def test_clear_aperture_and_labels_follow_the_scale(self):
        lens = lens_payload()
        lens["aperture_usage"] = "user_and_default"
        lens["surfaces"][1]["apertures"] = [{"kind": "clear", "shape": "circular", "label": "A", "radius": 28.0,
                                             "x_decenter": 0.0, "y_decenter": 0.0, "rotation_degrees": 0.0,
                                             "or_with_previous": False}]
        edits, _ = plan_scaling(lens, 2.0)
        aperture = next(item for item in edits if item["parameter"] == "clear_aperture_radius")
        self.assertEqual(native_command(aperture, lens), "CIR S1 CLR L'A' 56")
        lens["aperture"] = {"kind": "fno", "value": 2.0}
        edits, left = plan_scaling(lens, 2.0)
        self.assertFalse([item for item in edits if item["target"] == "aperture"])
        self.assertIn("scale invariant", next(item["reason"] for item in left if item.get("target") == "aperture"))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "dbgauss.len"
        self.source.write_text("! placeholder lens\n", encoding="utf-8")
        self.output = self.root / "scaled.len"

    def run_with(self, client=ScalingClient, **kwargs):
        with patch("codev_mcp.scale.ROOT", self.root):
            return run_scale(self.source, self.output, backend="com", output_dir=self.root / "out",
                             client_factory=client, **kwargs)

    def test_scales_to_the_target_and_records_the_commands(self):
        before = self.source.read_bytes()
        bundle, manifest = self.run_with(target_efl=50.0)
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertAlmostEqual(manifest["plan"]["factor"], 50.0 / EFL)
        self.assertTrue(manifest["efl_check"]["within_tolerance"])
        coupled = manifest["transaction"]["solve_coupled"]
        self.assertEqual((coupled[0]["surface"], coupled[0]["control"]), (11, "PIM"))
        self.assertAlmostEqual(coupled[0]["after"], manifest["first_order"]["image_distance"]["after"])
        self.assertEqual(self.output.read_bytes(), (bundle / "scaled.len").read_bytes())
        self.assertEqual(self.source.read_bytes(), before)
        record = json.loads((bundle / "execution-record.json").read_text(encoding="utf-8"))
        step = record["steps"][0]
        self.assertEqual((record["kind"], step["action"], step["status"]), ("execution_record", "scale", "succeeded"))
        self.assertEqual(len(step["native_commands"]), 19)
        self.assertEqual(step["outputs"][0]["sha256"], manifest["output"]["sha256"])
        self.assertIn("求解联动", (bundle / "report.md").read_text(encoding="utf-8"))

    def test_existing_output_and_bad_requests_are_refused_before_starting(self):
        self.output.write_text("taken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "new .len"):
            self.run_with(target_efl=50.0)
        self.output.unlink()
        for kwargs in ({}, {"target_efl": 50.0, "factor": 2.0}, {"factor": -1.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_with(**kwargs)
        self.assertFalse((self.root / "out").exists())

    def test_failures_leave_no_output(self):
        clients = {
            "rolled back": type("Refusing", (ScalingClient,), {"roll_back": True}),
            "deviates": type("Drifting", (ScalingClient,), {"efl_error": 1e-6}),
            "cleanup": type("Leaking", (ScalingClient,), {"confirm_cleanup": False}),
        }
        for message, client in clients.items():
            with self.subTest(message):
                bundle, manifest = self.run_with(client=client, factor=2.0)
                self.assertEqual(manifest["status"], "failed")
                self.assertIn(message, manifest["error"])
                self.assertFalse(self.output.exists())
                record = json.loads((bundle / "execution-record.json").read_text(encoding="utf-8"))
                self.assertEqual(record["steps"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
