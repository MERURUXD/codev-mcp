"""Design requirement precision and condition checks."""
from __future__ import annotations

import copy
import math
import unittest
from pathlib import Path

from codev_mcp.evaluation import (evaluate, evaluate_conditions, evaluate_spec, thickness_segments,
                                  validate_config)

DATA = Path(__file__).resolve().parent / "data"
# Hand-authored synthetic geometry; not a vendor prescription.
DBGAUSS_ROWS = [
    ("object", None, 1e20, None, None), ("surface", 64.12345, 9.123456, "BSM24_OHARA", 28.0),
    ("surface", 170.76543, 0.456789, None, 26.0), ("surface", 48.12345, 11.23456, "SK1_SCHOTT", 22.0),
    ("surface", None, 4.654321, "F15_SCHOTT", 20.0), ("surface", 31.87654, 13.765432, None, 17.0),
    ("surface", None, 10.345678, None, 16.5), ("surface", -36.12345, 4.654321, "F15_SCHOTT", 17.0),
    ("surface", None, 9.876543, "SK16_SCHOTT", 20.0), ("surface", -47.56789, 0.456789, None, 21.0),
    ("surface", 510.12345, 7.345678, "SK16_SCHOTT", 23.0), ("surface", -70.45678, 61.234567, None, 24.0),
    ("image", None, 0.0, None, 24.9),
]


def dbgauss_lens() -> dict:
    surfaces = [{"number": n, "role": role, "radius": radius, "radius_is_infinite": radius is None,
                 "thickness": thickness, "thickness_is_infinite": thickness >= 1e10, "glass": glass,
                 "semi_aperture": semi, "is_stop": n == 6}
                for n, (role, radius, thickness, glass, semi) in enumerate(DBGAUSS_ROWS)]
    return {"units": "mm", "zoom_positions": 1, "surfaces": surfaces, "stop_surface": 6,
            "aperture": {"kind": "epd", "value": 50.0},
            "fields": [{"number": 1, "x_angle": 0.0, "y_angle": 0.0, "weight": 1.0},
                       {"number": 2, "x_angle": 0.0, "y_angle": 10.0000000023, "weight": 1.0},
                       {"number": 3, "x_angle": 0.0, "y_angle": 14.0000000032, "weight": 1.0}],
            "wavelengths": [{"number": 1, "micrometers": 0.6563, "weight": 1.0, "is_reference": False},
                            {"number": 2, "micrometers": 0.5876, "weight": 1.0, "is_reference": True},
                            {"number": 3, "micrometers": 0.4861, "weight": 1.0, "is_reference": False}],
            "raw_listing": (DATA / "project-owned/compound-lis.txt").read_text(encoding="utf-8")}


def sag(radius, height):
    return 0.0 if radius is None else radius - math.copysign(math.sqrt(radius ** 2 - height ** 2), radius)


def spec_requirement(metric, unit="mm", minimum=None, maximum=None, **conditions):
    return {"id": metric, "metric": metric, "unit": unit, "field": conditions.get("field"),
            "direction": conditions.get("direction"), "frequency": conditions.get("frequency"),
            "minimum": minimum, "maximum": maximum, "required": True}


def requirement(metric="effective_focal_length", minimum=99, maximum=101, **conditions):
    return {"id": "r1", "metric": metric,
            "unit": "ratio" if metric in {"mtf", "f_number", "wavefront_strehl"} else "mm",
            "field": conditions.get("field"), "direction": conditions.get("direction"),
            "frequency": conditions.get("frequency"), "minimum": minimum,
            "maximum": maximum, "required": True}


class EvaluationTests(unittest.TestCase):
    def test_pass_fail_unknown_and_simulation(self):
        req = requirement()
        lens = {"units": "mm"}
        result = {"first_order": {"effective_focal_length": 100, "units": "mm", "precision_note": "string"}}
        self.assertEqual(evaluate([req], result, lens, "codev")["status"], "pass")
        self.assertEqual(evaluate([req], result, lens, "simulated")["status"], "unknown")
        result["first_order"]["effective_focal_length"] = 102
        self.assertEqual(evaluate([req], result, lens, "codev")["status"], "fail")
        result["first_order"]["effective_focal_length"] = float("nan")
        self.assertEqual(evaluate([req], result, lens, "codev")["status"], "unknown")

    def test_printed_threshold_is_unknown(self):
        req = requirement("back_focal_length", minimum=87.7, maximum=None)
        result = {"first_order": {"back_focal_length": 87.7, "units": "mm", "precision_note": "listing"}}
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "unknown")
        result["first_order"]["back_focal_length"] = 87.8
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "pass")
        # OAL is read through EvaluateExpression, so a value on the limit is not a printed value.
        req = requirement("overall_length", minimum=None, maximum=40)
        result = {"first_order": {"overall_length": 40.0, "units": "mm", "precision_note": "string"}}
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "pass")

    def test_missing_mtf_condition_and_unit(self):
        req = requirement("mtf", minimum=0.3, maximum=None,
                          field=2, direction="sagittal", frequency=20)
        result = {"mtf": {"frequencies": [0, 20], "curves": [{"field_number": 1,
                  "sagittal": [1, 0.4]}]}}
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "unknown")
        result["mtf"]["curves"][0]["field_number"] = 2
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "pass")

    def test_invalid_config_conditions(self):
        config = {"schema_version": 1,
                  "analysis": {"kinds": ["mtf"], "fields": [1], "frequencies": [0, 20], "spot_grid": 7},
                  "requirements": [requirement("mtf", minimum=0.5, maximum=None,
                                               field=1, direction="tangential", frequency=21)],
                  "export_indices": []}
        with self.assertRaises(ValueError):
            validate_config(config)
        config["requirements"][0]["frequency"] = 20
        validate_config(config)
        config["requirements"].append(dict(config["requirements"][0]))
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_scan_config_keeps_its_metric_set(self):
        config = {"schema_version": 1,
                  "analysis": {"kinds": ["first_order"], "fields": None, "frequencies": [0], "spot_grid": 7},
                  "requirements": [spec_requirement("center_thickness_min", minimum=1)],
                  "export_indices": []}
        with self.assertRaises(ValueError):
            validate_config(config)
        config["requirements"][0]["source"] = "text"
        config["requirements"][0]["metric"] = "effective_focal_length"
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_spot_precision_follows_printed_diameter(self):
        # 0.00615 mm radius was printed as a 0.0123 mm diameter: +/- 0.000025 in radius.
        req = requirement("spot_rms_radius", minimum=None, maximum=0.00616, field=1)
        result = {"spot-1": {"rms_radius": 0.00615, "units": "mm"}}
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "unknown")
        req["maximum"] = 0.006176
        self.assertEqual(evaluate([req], result, {}, "codev")["status"], "pass")


def dbgauss_lens_with_glass_thickness(thickness):
    """dbgauss with the thickness of every glass-bearing surface set to one value."""
    lens = dbgauss_lens()
    for surface in lens["surfaces"]:
        if surface["glass"]:
            surface["thickness"] = thickness
    return lens


class RoundoffToleranceTests(unittest.TestCase):
    """Values read back with EvaluateExpression are exact only to machine precision."""

    def center(self, thickness, **bounds):
        req = spec_requirement("center_thickness_min", **bounds)
        lens = dbgauss_lens_with_glass_thickness(thickness)
        return evaluate([req], {}, lens, "codev")["requirements"][0]

    def test_a_thickness_pushed_onto_its_bound_reads_as_met(self):
        # A variable driven to its lower bound 0.1 reads back as
        # 0.09999999999999896; the exact comparison used to call that a fail.
        item = self.center(0.09999999999999896, minimum=0.1)
        self.assertEqual(item["status"], "pass")
        self.assertIn("machine tolerance", item["reason"])
        self.assertAlmostEqual(item["tolerance"], 1e-14)

    def test_a_real_violation_still_fails(self):
        self.assertEqual(self.center(0.0999999, minimum=0.1)["status"], "fail")

    def test_a_value_inside_the_bound_has_no_note(self):
        item = self.center(0.2, minimum=0.1)
        self.assertEqual((item["status"], item["reason"]), ("pass", None))

    def test_the_upper_bound_is_relaxed_the_same_way(self):
        req = spec_requirement("center_thickness_max", maximum=3.5)
        lens = dbgauss_lens_with_glass_thickness(3.5000000000000053)
        item = evaluate([req], {}, lens, "codev")["requirements"][0]
        self.assertEqual(item["status"], "pass")
        lens = dbgauss_lens_with_glass_thickness(3.5001)
        self.assertEqual(evaluate([req], {}, lens, "codev")["requirements"][0]["status"], "fail")

    def test_first_order_lengths_carry_the_tolerance(self):
        req = requirement("overall_length", minimum=None, maximum=79.03)
        result = {"first_order": {"overall_length": 79.03000000000005, "units": "mm", "precision_note": "p"}}
        item = evaluate([req], result, {}, "codev")["requirements"][0]
        self.assertEqual(item["status"], "pass")
        self.assertGreater(item["tolerance"], 5e-14)
        req["maximum"] = 75.0399
        self.assertEqual(evaluate([req], result, {}, "codev")["requirements"][0]["status"], "fail")

    def test_the_printed_back_focal_length_keeps_its_symmetric_uncertainty(self):
        req = requirement("back_focal_length", minimum=None, maximum=61.2346)
        result = {"first_order": {"back_focal_length": 61.2346, "units": "mm", "precision_note": "p"}}
        item = evaluate([req], result, {}, "codev")["requirements"][0]
        self.assertEqual(item["tolerance"], 0.0)
        self.assertEqual(item["status"], "unknown")

    def test_a_printed_quantity_gets_no_roundoff_tolerance(self):
        req = requirement("spot_rms_radius", minimum=None, maximum=0.00615, field=1)
        result = {"spot-1": {"rms_radius": 0.00615, "units": "mm"}}
        item = evaluate([req], result, {}, "codev")["requirements"][0]
        self.assertEqual(item["tolerance"], 0.0)


class DistortionTests(unittest.TestCase):
    def setUp(self):
        self.lens = dbgauss_lens()
        self.lens["fields"][1]["y_angle"] = 3
        self.lens["fields"][2]["y_angle"] = 6
        self.lens["wavelengths"][1]["micrometers"] = 0.55
        self.results = {"native-field_aberration": {
            "raw_output": (DATA / "project-owned/fie.txt").read_text(encoding="utf-8")}}

    def check(self, maximum, lens=None, results=None):
        req = spec_requirement("distortion_max_abs", "percent", maximum=maximum)
        return evaluate([req], results or self.results, lens or self.lens, "codev")["requirements"][0]

    def test_largest_sample_and_printed_boundary(self):
        item = self.check(1.0)
        self.assertEqual(item["status"], "pass")
        self.assertAlmostEqual(item["value"], 0.06442)
        self.assertAlmostEqual(item["uncertainty"], 5e-6)
        self.assertEqual(item["details"]["full_field_percent"], -0.06442)
        self.assertEqual(len(item["details"]["rows"]), 11)
        self.assertEqual(self.check(0.06442)["status"], "unknown")
        self.assertEqual(self.check(0.0644)["status"], "fail")

    def test_unverifiable_tables_are_unknown(self):
        lens = copy.deepcopy(self.lens)
        lens["wavelengths"][1]["is_reference"] = False
        lens["wavelengths"][0]["is_reference"] = True
        self.assertIn("wavelength", self.check(1.0, lens=lens)["reason"])
        lens = copy.deepcopy(self.lens)
        lens["fields"][2]["y_angle"] = 15.0
        self.assertIn("full-field", self.check(1.0, lens=lens)["reason"])
        lens = copy.deepcopy(self.lens)
        lens["fields"][1]["x_angle"] = 1.0
        self.assertIn("Y-only", self.check(1.0, lens=lens)["reason"])
        text = self.results["native-field_aberration"]["raw_output"]
        table = text[text.index("    WAVELENGTH"):text.index("    Units of focus")]
        doubled = text.replace("    Units of focus", table + "    Units of focus", 1)
        for raw in (None, text.replace("        0.50", "        0.55"), doubled,
                    text.replace("Command End:", "")):
            with self.subTest(raw=None if raw is None else len(raw)):
                item = self.check(1.0, results={"native-field_aberration": {"raw_output": raw}})
                self.assertEqual(item["status"], "unknown")


class ThicknessTests(unittest.TestCase):
    def test_segments_skip_dummy_stop_and_image_distance(self):
        segments, reason = thickness_segments(dbgauss_lens())
        self.assertIsNone(reason)
        self.assertEqual([s["surfaces"] for s in segments if s["medium"] == "glass"],
                         [[1, 2], [3, 4], [4, 5], [7, 8], [8, 9], [10, 11]])
        air = [s for s in segments if s["medium"] == "air"]
        self.assertEqual([s["surfaces"] for s in air], [[2, 3], [5, 7], [9, 10]])
        self.assertAlmostEqual(air[1]["center"], 13.765432 + 10.345678)
        first = segments[0]
        self.assertEqual(first["edge_height"], 28.0)
        self.assertAlmostEqual(first["edge"], 9.123456 + sag(170.76543, 28.0) - sag(64.12345, 28.0))

    def test_thickness_judgements(self):
        lens = dbgauss_lens()
        reqs = [spec_requirement("center_thickness_min", minimum=4.654321),
                spec_requirement("center_thickness_max", maximum=11.0),
                spec_requirement("edge_thickness_min", minimum=1.0),
                spec_requirement("air_center_thickness_min", minimum=0.5),
                spec_requirement("air_edge_thickness_min", minimum=0.5)]
        items = {i["metric"]: i for i in evaluate(reqs, {}, lens, "codev")["requirements"]}
        self.assertEqual(items["center_thickness_min"]["status"], "pass")
        self.assertEqual(items["center_thickness_max"]["status"], "fail")
        self.assertEqual(items["air_center_thickness_min"]["status"], "fail")
        self.assertEqual(items["edge_thickness_min"]["value_source"], "service_calculated")
        expected = 7.345678 + sag(-70.45678, 24.0) - sag(510.12345, 24.0)
        self.assertAlmostEqual(items["edge_thickness_min"]["value"], expected)
        self.assertEqual(items["edge_thickness_min"]["status"], "pass")
        self.assertEqual(items["air_edge_thickness_min"]["status"], "pass")

    def test_unconfirmed_geometry_is_unknown(self):
        req = spec_requirement("edge_thickness_min", minimum=1.0)
        center = spec_requirement("center_thickness_min", minimum=1.0)
        lens = dbgauss_lens()
        lens["raw_listing"] = lens["raw_listing"].replace("    5:        31.87654       13.765432         ",
                                                          "    5:        31.87654       13.765432  ASP    ")
        items = evaluate([req, center], {}, lens, "codev")["requirements"]
        self.assertEqual([i["status"] for i in items], ["unknown", "pass"])
        self.assertIn("special surface", items[0]["reason"])
        for change, reason in (({"semi_aperture": None}, "surfaces 1-2: semi-aperture unavailable"),
                               ({"semi_aperture": 80.0}, "surfaces 1-2: edge height 80 exceeds |R| 64.1235 of surface 1")):
            lens = dbgauss_lens()
            lens["surfaces"][1].update(change)
            item = evaluate([req], {}, lens, "codev")["requirements"][0]
            self.assertEqual((item["status"], item["reason"]), ("unknown", reason))
            self.assertEqual(len(item["details"]), 6)
        lens = dbgauss_lens()
        lens["surfaces"][3]["glass"] = "REFL"
        self.assertEqual(evaluate([center], {}, lens, "codev")["status"], "unknown")
        self.assertEqual(evaluate([center], {}, dbgauss_lens(), "simulated")["status"], "unknown")


class SpecJudgementTests(unittest.TestCase):
    def spec(self):
        return {"system": {}, "requirements": [], "criteria": []}

    def results(self, rms=0.05, strehl=0.85, radius=0.001):
        return {"first_order": {"f_number": 2.0, "units": "mm", "precision_note": "string"},
                "wavefront": {"fields": [{"field_number": n, "rms_waves": rms, "strehl": strehl}
                                         for n in (1, 2, 3)], "precision_note": "printed"},
                **{f"spot-{n}": {"rms_radius": radius, "max_radius": 2 * radius, "units": "mm"}
                   for n in (1, 2, 3)}}

    def test_marechal_preset(self):
        spec = self.spec()
        spec["criteria"] = [{"preset": "marechal", "required": True, "source": "Born & Wolf"}]
        judged = evaluate_spec(spec, self.results(), dbgauss_lens(), "codev")
        self.assertEqual(judged["status"], "pass")
        self.assertEqual(len(judged["criteria"]), 6)
        self.assertEqual(judged["criteria"][0]["threshold_source"], "preset:marechal")
        self.assertEqual(evaluate_spec(spec, self.results(rms=0.08), dbgauss_lens(), "codev")["status"], "fail")
        self.assertEqual(evaluate_spec(spec, self.results(rms=0.07), dbgauss_lens(), "codev")["status"], "unknown")
        spec["criteria"][0]["required"] = False
        self.assertEqual(evaluate_spec(spec, self.results(rms=0.08), dbgauss_lens(), "codev")["status"], "unknown")

    def test_airy_preset_uses_reference_wavelength_and_units(self):
        spec = self.spec()
        spec["criteria"] = [{"preset": "airy_spot", "statistic": "rms", "required": True}]
        airy = 2.44 * 0.5876e-3 * 2.0
        judged = evaluate_spec(spec, self.results(radius=0.001), dbgauss_lens(), "codev")
        item = judged["criteria"][0]
        self.assertAlmostEqual(item["airy_diameter"], airy)
        self.assertAlmostEqual(item["value"], 0.002)
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["threshold_source"], "service_calculated:airy_diameter")
        self.assertEqual(evaluate_spec(spec, self.results(radius=0.00155), dbgauss_lens(), "codev")["status"], "fail")
        spec["criteria"][0]["statistic"] = "geometric_max"
        self.assertEqual(evaluate_spec(spec, self.results(radius=0.001), dbgauss_lens(), "codev")["status"], "fail")
        lens = dbgauss_lens()
        lens["surfaces"][0].update(thickness=500.0, thickness_is_infinite=False)
        self.assertIn("finite object", evaluate_spec(spec, self.results(), lens, "codev")["criteria"][0]["reason"])
        lens = dbgauss_lens()
        lens["units"] = "cm"
        results = self.results(radius=0.0001)
        for n in (1, 2, 3):
            results[f"spot-{n}"]["units"] = "cm"
        spec["criteria"][0]["statistic"] = "rms"
        item = evaluate_spec(spec, results, lens, "codev")["criteria"][0]
        self.assertAlmostEqual(item["airy_diameter"], airy / 10)
        self.assertEqual(item["status"], "pass")

    def test_conditions(self):
        lens = dbgauss_lens()
        system = {"aperture": {"kind": "epd", "value": 50},
                  "fields": {"values": [{"y_angle": 0, "weight": 1}, {"y_angle": 10}, {"y_angle": 14}]},
                  "wavelengths": {"values": [{"nm": 656.3}, {"nm": 587.6}, {"nm": 486.1}], "reference": 2},
                  "glass_catalogs": {"allowed": ["schott", "OHARA"]}}
        self.assertEqual([i["status"] for i in evaluate_conditions(system, lens, "codev")], ["pass"] * 4)
        changed = copy.deepcopy(system)
        changed["aperture"] = {"kind": "fno", "value": 2}
        changed["fields"]["values"][2]["y_angle"] = 14.01
        changed["wavelengths"]["reference"] = 1
        changed["glass_catalogs"]["allowed"] = ["SCHOTT"]
        items = evaluate_conditions(changed, lens, "codev")
        self.assertEqual([i["status"] for i in items], ["unknown", "fail", "fail", "fail"])
        self.assertIn("BSM24_OHARA", items[3]["reason"])
        lens["surfaces"][1]["glass"] = "BSM24"
        self.assertEqual(evaluate_conditions(system, lens, "codev")[3]["status"], "unknown")
        pending = evaluate_conditions({"aperture": {"pending": True, "source": "待补"}}, lens, "codev")[0]
        self.assertEqual((pending["status"], pending["pending"]), ("unknown", True))
        self.assertIn("待补", pending["reason"])
        self.assertEqual({i["status"] for i in evaluate_conditions(system, lens, "simulated")}, {"unknown"})

    def test_overall_status_and_pending(self):
        spec = self.spec()
        spec["requirements"] = [
            spec_requirement("f_number", "ratio", 1.9, 2.1),
            dict(spec_requirement("effective_focal_length"), id="pending", pending=True, source="待补")]
        judged = evaluate_spec(spec, self.results(), dbgauss_lens(), "codev")
        self.assertEqual([i["status"] for i in judged["requirements"]], ["pass", "unknown"])
        self.assertIn("待补", judged["requirements"][1]["reason"])
        self.assertEqual(judged["status"], "unknown")
        spec["requirements"][1]["required"] = False
        self.assertEqual(evaluate_spec(spec, self.results(), dbgauss_lens(), "codev")["status"], "pass")
        oal = spec_requirement("overall_length", maximum=80)
        judged = evaluate_spec(dict(spec, requirements=[oal]), {"first_order": {
            "overall_length": 79.03, "units": "mm", "precision_note": "string"}}, dbgauss_lens(), "codev")
        self.assertIn("image distance is not included", judged["requirements"][0]["precision"])


if __name__ == "__main__":
    unittest.main()
