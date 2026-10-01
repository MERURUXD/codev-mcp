"""Typed-edit CLI (glass swap): transaction checks, reopen, output and execution record."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codev_mcp.edit import native_command, read_edits, run_edit
from tests.test_scale import lens_payload

EDITS = {"schema_version": 1, "kind": "lens_edits", "name": "cdgm-swap", "reason": "nearest CDGM glasses",
         "edits": [{"target": "surface", "surface": 4, "parameter": "glass", "value": "HF1_CDGM"},
                   {"target": "surface", "surface": 7, "parameter": "glass", "value": "HF1_CDGM"}]}


class EditClient:
    refuse = False

    def __init__(self, *_args):
        self.lens = None
        self.revision = 0

    def call(self, name, arguments=None, timeout=None):
        arguments = arguments or {}
        if name == "open_lens":
            text = Path(arguments["path"]).read_text(encoding="utf-8")
            self.lens = json.loads(text) if text.startswith("{") else lens_payload()
            return copy.deepcopy(self.lens), []
        if name == "get_lens":
            return copy.deepcopy(self.lens), []
        if name == "get_status":
            return {"details": {"lens_id": "lens", "committed_revision": self.revision}}, []
        if name == "update_lens":
            edits = arguments["request"]["edits"]
            if self.refuse:
                return {"rolled_back": True, "session_valid": True, "warnings": ["glass not found"],
                        "outcomes": [{"applied": False} for _ in edits]}, []
            for item in edits:
                self.lens["surfaces"][item["surface"]]["glass"] = item["value"]
            self.lens["surfaces"][11]["thickness"] = 63.0
            self.revision += 1
            return {"rolled_back": False, "session_valid": True, "outcomes": [{"applied": True} for _ in edits],
                    "warnings": ["CODE V re-derived the solved parameters surface 11 thickness"]}, []
        if name == "run_analysis":
            self.request = arguments["request"]
            return {"task_id": "t"}, []
        if name == "get_analysis":
            return {"source": "codev", "task": {"task_id": "t", "kind": "first_order", "state": "succeeded",
                                                "source": "codev", "created_at": "now",
                                                "settings": self.request["options"]},
                    "first_order": {"source": "codev", "units": "mm", "zoom_position": 1,
                                    "effective_focal_length": 100.0 + self.revision, "f_number": 2.0,
                                    "overall_length": 75.0, "image_distance": 63.0, "raw_output": "items",
                                    "precision_note": "string"}}, []
        if name == "save_lens_as":
            Path(arguments["path"]).write_text(json.dumps(self.lens), encoding="utf-8")
            return {"path": arguments["path"], "overwritten": False}, []
        raise AssertionError(name)

    def close(self):
        return {"returncode": 0, "close_session": {"session_open": False, "details": {"cleanup_confirmed": True}}}


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "start.len"
        self.source.write_text("! lens\n", encoding="utf-8")
        self.edits = self.root / "edits.json"
        self.edits.write_text(json.dumps(EDITS), encoding="utf-8")

    def run_with(self, client=EditClient):
        with patch("codev_mcp.edit.ROOT", self.root):
            return run_edit(self.source, self.edits, self.root / "edited.len", output_dir=self.root / "out",
                            client_factory=client)

    def test_glass_swap_is_recorded_and_saved(self):
        bundle, manifest = self.run_with()
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertTrue((self.root / "edited.len").is_file())
        self.assertEqual(manifest["transaction"]["revision_after"], 1)
        self.assertIn("re-derived", manifest["transaction"]["warnings"][0])
        record = json.loads((bundle / "execution-record.json").read_text(encoding="utf-8"))
        step = record["steps"][0]
        self.assertEqual(step["native_commands"], ["GLA S4 HF1_CDGM", "GLA S7 HF1_CDGM"])
        self.assertEqual(step["parameters"]["reason"], "nearest CDGM glasses")
        self.assertEqual(step["outputs"][0]["sha256"], manifest["output"]["sha256"])

    def test_refused_transaction_leaves_no_output(self):
        _, manifest = self.run_with(type("Refusing", (EditClient,), {"refuse": True}))
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("rolled back", manifest["error"])
        self.assertFalse((self.root / "edited.len").exists())

    def test_edit_file_validation_and_commands(self):
        for change in ({"kind": "other"}, {"edits": []}, {"edits": [{"target": "surface", "parameter": "radius", "value": 1}]},
                       {"command": "in macro"}):
            path = self.root / "bad.json"
            path.write_text(json.dumps({**EDITS, **change}), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_edits(path)
        lens = lens_payload()
        self.assertEqual(native_command({"target": "field", "field": 2, "parameter": "weight", "value": 0.5}, lens), "WTF F2 0.5")
        self.assertEqual(native_command({"target": "field", "field": 3, "parameter": "vuy", "value": 0.3}, lens), "VUY F3 0.3")
        self.assertEqual(native_command({"target": "wavelength", "wavelength": 1, "parameter": "micrometers", "value": 0.5876}, lens), "WL W1 587.6")
        self.assertEqual(native_command({"target": "aperture", "parameter": "value", "value": 25}, lens), "EPD 25")
        # The recorded command matches what update_lens sends: REF takes the value, CIR keeps the label.
        self.assertEqual(native_command({"target": "wavelength", "wavelength": 1, "parameter": "is_reference", "value": 3}, lens), "REF 3")
        lens["surfaces"][1]["apertures"] = [{"kind": "clear", "shape": "circular", "radius": 20.0, "label": "A"}]
        self.assertEqual(native_command({"target": "surface", "surface": 1, "parameter": "clear_aperture_radius", "value": 18}, lens),
                         "CIR S1 CLR L'A' 18")

    def test_a_field_set_file_is_recorded_with_its_native_commands(self):
        class FieldSetClient(EditClient):
            def call(self, name, arguments=None, timeout=None):
                if name == "update_lens":
                    spec = arguments["request"]["field_set"]
                    self.lens["fields"] = [
                        {"number": number, "x_angle": item.get("x_angle", 0.0), "y_angle": item["y_angle"],
                         "weight": item.get("weight", 1.0), "vux": 0.0, "vlx": 0.0,
                         "vuy": item.get("vuy", 0.0), "vly": 0.0}
                        for number, item in enumerate(spec["fields"], 1)]
                    self.revision += 1
                    return {"rolled_back": False, "session_valid": True, "outcomes": [],
                            "field_set": {"applied": True, "fields": self.lens["fields"]},
                            "warnings": ["The field set was replaced."]}, []
                return super().call(name, arguments, timeout)

        self.edits.write_text(json.dumps({
            "schema_version": 1, "kind": "lens_edits", "name": "five-fields",
            "field_set": {"fields": [{"y_angle": 0}, {"y_angle": 5}, {"y_angle": 10}, {"y_angle": 14},
                                     {"y_angle": 18, "weight": 0.5}]}}), encoding="utf-8")
        bundle, manifest = self.run_with(FieldSetClient)
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertTrue(manifest["transaction"]["field_set"]["applied"])
        record = json.loads((bundle / "execution-record.json").read_text(encoding="utf-8"))
        step = record["steps"][0]
        self.assertEqual(step["native_commands"], ["XAN 0 0 0 0 0", "YAN 0 5 10 14 18", "WTF F5 0.5"])
        self.assertEqual(step["parameters"]["field_set"]["fields"][4]["weight"], 0.5)

    def test_a_refused_field_set_leaves_no_output(self):
        class Refusing(EditClient):
            def call(self, name, arguments=None, timeout=None):
                if name == "update_lens":
                    return {"rolled_back": True, "session_valid": True, "outcomes": [],
                            "field_set": {"applied": False, "rejected_reason": "single-zoom only"},
                            "warnings": ["No field was changed"]}, []
                return super().call(name, arguments, timeout)

        self.edits.write_text(json.dumps({
            "schema_version": 1, "kind": "lens_edits", "name": "x", "field_set": {"fields": [{"y_angle": 0}]}}),
            encoding="utf-8")
        _, manifest = self.run_with(Refusing)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("rolled back", manifest["error"])
        self.assertFalse((self.root / "edited.len").exists())

    def test_edits_and_a_field_set_are_exclusive_and_the_field_set_is_validated(self):
        fields = {"fields": [{"y_angle": 0}]}
        for change in ({"field_set": fields}, {"edits": None},
                       {"edits": None, "field_set": {"fields": [{"y_angle": 0, "command": "x"}]}},
                       {"edits": None, "field_set": {"fields": []}}):
            payload = {key: value for key, value in {**EDITS, **change}.items() if value is not None}
            path = self.root / "bad-field-set.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_edits(path)
        path = self.root / "good-field-set.json"
        path.write_text(json.dumps({"schema_version": 1, "kind": "lens_edits", "name": "n",
                                    "field_set": fields}), encoding="utf-8")
        payload, _ = read_edits(path)
        self.assertEqual(payload["edits"], [])
        self.assertEqual(payload["field_set"]["fields"][0]["x_angle"], 0.0)

    def test_multi_zoom_lens_is_refused(self):
        lens = lens_payload()
        lens["zoom_positions"] = 2
        self.source.write_text(json.dumps(lens), encoding="utf-8")
        _, manifest = self.run_with()
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("single-zoom", manifest["error"])
        self.assertFalse((self.root / "edited.len").exists())


if __name__ == "__main__":
    unittest.main()
