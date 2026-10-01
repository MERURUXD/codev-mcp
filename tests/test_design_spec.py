"""Design spec format: the public example and rejected shapes."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from codev_mcp.compare import KINDS
from codev_mcp.spec import ANALYSES, check_lens, read_spec, validate_spec

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "design" / "design-spec-dbgauss.json"


def example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def requirement(ident: str) -> tuple[dict, dict]:
    spec = example()
    return spec, next(r for r in spec["requirements"] if r["id"] == ident)


class DesignSpecTests(unittest.TestCase):
    def test_public_example_is_valid_and_hashed(self):
        spec, digest = read_spec(EXAMPLE)
        self.assertTrue(spec["demonstration"])
        self.assertEqual(len(digest), 64)
        self.assertEqual(ANALYSES, KINDS)
        text = EXAMPLE.read_text(encoding="utf-8")
        for private in ("ucas", "course", "作业", ":\\\\"):
            self.assertNotIn(private, text)

    def test_wrong_units(self):
        spec = example()
        spec["units"] = "m"
        with self.assertRaisesRegex(ValueError, "units"):
            validate_spec(spec)
        spec, item = requirement("oal")
        item["unit"] = "cm"
        with self.assertRaisesRegex(ValueError, "declared units"):
            validate_spec(spec)
        spec, item = requirement("distortion")
        item["unit"] = "ratio"
        with self.assertRaisesRegex(ValueError, "unit is incorrect"):
            validate_spec(spec)
        spec, item = requirement("mtf-axis-10")
        item["unit"] = "percent"
        with self.assertRaises(ValueError):
            validate_spec(spec)

    def test_duplicate_and_reserved_ids(self):
        spec = example()
        spec["requirements"].append(copy.deepcopy(spec["requirements"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_spec(spec)
        spec, item = requirement("efl")
        item["id"] = "preset.efl"
        with self.assertRaisesRegex(ValueError, "reserved"):
            validate_spec(spec)
        spec = example()
        spec["criteria"].append({"preset": "marechal", "required": True})
        with self.assertRaisesRegex(ValueError, "Duplicate criterion"):
            validate_spec(spec)

    def test_missing_required_fields(self):
        for key in ("units", "evaluation", "requirements", "name"):
            spec = example()
            del spec[key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "missing"):
                validate_spec(spec)
        spec, item = requirement("efl")
        del item["required"]
        with self.assertRaisesRegex(ValueError, "explicit ID"):
            validate_spec(spec)
        spec, item = requirement("efl")
        item["minimum"] = item["maximum"] = None
        with self.assertRaisesRegex(ValueError, "bound"):
            validate_spec(spec)
        spec = example()
        del spec["system"]["wavelengths"]["reference"]
        with self.assertRaisesRegex(ValueError, "reference"):
            validate_spec(spec)
        spec = example()
        spec["criteria"][1].pop("statistic")
        with self.assertRaisesRegex(ValueError, "statistic"):
            validate_spec(spec)

    def test_pending_entries(self):
        spec, item = requirement("mtf-edge-50")
        item["minimum"] = 0.2
        with self.assertRaisesRegex(ValueError, "pending"):
            validate_spec(spec)
        spec = example()
        spec["system"]["aperture"] = {"pending": True, "source": "待补"}
        validate_spec(spec)
        spec["system"]["aperture"]["value"] = 50
        with self.assertRaisesRegex(ValueError, "pending"):
            validate_spec(spec)

    def test_conditions_must_match_selected_analyses(self):
        spec = example()
        spec["evaluation"]["analyses"].remove("native_plot")
        with self.assertRaisesRegex(ValueError, "Distortion"):
            validate_spec(spec)
        spec = example()
        spec["evaluation"]["analyses"].remove("wavefront")
        with self.assertRaisesRegex(ValueError, "marechal"):
            validate_spec(spec)
        spec, item = requirement("mtf-axis-10")
        item["frequency"] = 15
        with self.assertRaisesRegex(ValueError, "MTF"):
            validate_spec(spec)
        spec, item = requirement("glass-edge-min")
        item["field"] = 1
        with self.assertRaisesRegex(ValueError, "Thickness"):
            validate_spec(spec)
        spec = example()
        spec["evaluation"]["mtf_frequencies"] = [10, 0]
        with self.assertRaises(ValueError):
            validate_spec(spec)

    def test_unknown_keys_and_bad_json(self):
        spec = example()
        spec["thresholds"] = {}
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            validate_spec(spec)
        spec = example()
        spec["system"]["fields"]["values"][0]["z_angle"] = 1
        with self.assertRaises(ValueError):
            validate_spec(spec)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_bytes(b"\xff{")
            with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
                read_spec(path)

    def test_lens_checks_before_analysis(self):
        spec = example()
        lens = {"units": "mm", "zoom_positions": 1, "fields": [{"number": n} for n in (1, 2, 3)]}
        check_lens(spec, lens)
        with self.assertRaisesRegex(ValueError, "units"):
            check_lens(spec, dict(lens, units="inch"))
        with self.assertRaisesRegex(ValueError, "single-zoom"):
            check_lens(spec, dict(lens, zoom_positions=2))
        with self.assertRaisesRegex(ValueError, "do not exist"):
            check_lens(spec, dict(lens, fields=[{"number": 1}]))


if __name__ == "__main__":
    unittest.main()
