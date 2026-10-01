"""Typed AUT spec: validation, command construction and synthetic output parsing."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from codev_mcp.aut_spec import (aut_commands, constraint_commands, expand_field_ramp, judge_constraints,
                                legacy_spec, lens_change_commands, open_commands, parse_aut_listing,
                                parse_variable_table, read_aut_spec, restore_commands, validate_aut_spec)

EVIDENCE = Path(__file__).parent / "data" / "project-owned"


def spec() -> dict:
    return {"schema_version": 1, "kind": "aut_spec", "name": "demo", "wall_seconds": 600, "stages": [
        {"name": "mono",
         "lens_changes": [{"target": "wavelength", "wavelength": 1, "parameter": "weight", "value": 0},
                          {"target": "field", "field": 2, "parameter": "weight", "value": 0.5}],
         "variables": [{"surface": 1, "parameter": "radius", "lower": 20, "upper": 90},
                       {"surface": 2, "parameter": "thickness", "lower": 0.1}],
         "constraints": [{"operand": "EFL", "relation": "=", "value": 50},
                         {"operand": "DIY", "field": 3, "relation": ">", "value": -0.005},
                         {"operand": "DIY", "field": 3, "relation": "<", "value": 0.005},
                         {"operand": "OAL", "relation": "<", "value": 40},
                         {"operand": "ET", "surface": 1, "relation": ">", "value": 1}],
         "general_constraints": {"MNT": 1, "MNE": 0.5},
         "error_function": {"MXC": 50, "MNC": 5, "TAR": 0, "IMP": 0.01}}]}


class SpecValidationTests(unittest.TestCase):
    def test_valid_spec_and_legacy_conversion(self):
        validate_aut_spec(spec())
        legacy = legacy_spec(3, "thickness", 1, 9, 0.5, 4, 90)
        validate_aut_spec(legacy)
        self.assertEqual(legacy["stages"][0]["error_function"], {"MXC": 4, "MNC": 1, "TAR": 0.5})

    def test_rejections(self):
        cases = [
            (lambda s: s["stages"][0]["constraints"].append({"operand": "TT", "relation": "<", "value": 1}), "operand"),
            (lambda s: s["stages"][0]["constraints"].append({"operand": "EFL", "relation": "<", "value": 60}), "Conflicting"),
            (lambda s: s["stages"][0]["constraints"].append({"operand": "DIY", "field": 3, "relation": "<", "value": 1}), "duplicate"),
            (lambda s: s["stages"][0]["constraints"][1].pop("field"), "field"),
            (lambda s: s["stages"][0]["variables"].append({"surface": 1, "parameter": "radius"}), "Duplicate variable"),
            (lambda s: s["stages"][0]["variables"][0].update(lower=95), "lower bound"),
            (lambda s: s["stages"][0]["error_function"].update(MXC=0), "MXC"),
            (lambda s: s["stages"][0]["error_function"].update(MNC=60), "MNC"),
            (lambda s: s["stages"][0]["error_function"].update(ERR="USR"), "error_function"),
            (lambda s: s["stages"][0]["lens_changes"][0].update(value=0.5), "integer weight"),
            (lambda s: s["stages"][0]["lens_changes"].append({"target": "surface", "parameter": "weight"}), "field or wavelength"),
            (lambda s: s["stages"][0]["general_constraints"].update(MXA=60), "general_constraints"),
            (lambda s: s["stages"][0].update(command="in macro"), "allowed keys"),
            (lambda s: s["stages"].append(copy.deepcopy(s["stages"][0])), "Duplicate stage"),
            (lambda s: s.update(wall_seconds=4000), "wall_seconds"),
            (lambda s: s["stages"][0].update(name="bad name;"), "Stage names"),
        ]
        for change, message in cases:
            value = spec()
            change(value)
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                validate_aut_spec(value)


class FieldChangeTests(unittest.TestCase):
    def stage(self, *changes):
        value = spec()
        value["stages"][0]["lens_changes"] = list(changes)
        return value

    def test_field_data_changes_become_typed_commands(self):
        value = self.stage(
            {"target": "field", "field": 2, "parameter": "y_angle", "value": 12.5},
            {"target": "field", "field": 2, "parameter": "x_angle", "value": 0},
            {"target": "field", "field": 3, "parameter": "vuy", "value": 0.25},
            {"target": "field", "field": 3, "parameter": "vlx", "value": -0.1},
            {"target": "field", "field": 3, "parameter": "weight", "value": 2},
            {"target": "wavelength", "wavelength": 2, "parameter": "weight", "value": 3})
        validate_aut_spec(value)
        self.assertEqual(lens_change_commands(value["stages"][0]),
                         ["yan f2 12.5", "xan f2 0", "vuy f3 0.25", "vlx f3 -0.1", "wtf f3 2", "wtw w2 3"])

    def test_bad_field_changes_are_refused(self):
        cases = [
            ({"target": "field", "field": 2, "parameter": "y_angle", "value": 95}, "y_angle"),
            ({"target": "field", "field": 2, "parameter": "vuy", "value": 1.0}, "vuy"),
            ({"target": "field", "field": 2, "parameter": "weight", "value": -1}, "weight"),
            ({"target": "field", "field": 0, "parameter": "y_angle", "value": 5}, "field >= 1"),
            ({"target": "field", "field": 2, "parameter": "radius", "value": 5}, "A field change edits"),
            ({"target": "field", "field": 2, "parameter": "y_angle", "value": 5, "zoom": 1}, "y_angle"),
            ({"target": "wavelength", "wavelength": 1, "parameter": "y_angle", "value": 5}, "wavelength weights"),
            ({"target": "field", "field": 2, "parameter": "y_angle", "value": float("nan")}, "y_angle"),
        ]
        for change, message in cases:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                validate_aut_spec(self.stage(change))


RAMP = {"schema_version": 1, "kind": "aut_spec", "name": "grow", "wall_seconds": 600,
        "stages": [{"name": "focus", "variables": [{"surface": 2, "parameter": "thickness", "lower": 0.1}],
                    "error_function": {"MXC": 3}}],
        "field_ramp": {"name": "ramp", "steps": [
            {"fields": [{"field": 2, "y_angle": 8}, {"field": 3, "y_angle": 10, "weight": 1.5}]},
            {"fields": [{"field": 2, "y_angle": 10}, {"field": 3, "y_angle": 14, "vuy": 0.2}]}],
            "stage": {"variables": [{"surface": 1, "parameter": "radius"}],
                      "lens_changes": [{"target": "wavelength", "wavelength": 1, "parameter": "weight", "value": 2}],
                      "error_function": {"MXC": 30}}}}


class FieldRampTests(unittest.TestCase):
    def test_a_ramp_expands_into_ordinary_stages(self):
        original = copy.deepcopy(RAMP)
        expanded = expand_field_ramp(RAMP)
        self.assertEqual(RAMP, original)  # the input is not modified
        validate_aut_spec(expanded)
        self.assertNotIn("field_ramp", expanded)
        self.assertEqual([s["name"] for s in expanded["stages"]], ["focus", "ramp-1", "ramp-2"])
        self.assertEqual([s.get("ramp_step") for s in expanded["stages"]], [None, 1, 2])
        self.assertEqual(lens_change_commands(expanded["stages"][1]),
                         ["yan f2 8", "yan f3 10", "wtf f3 1.5", "wtw w1 2"])
        self.assertEqual(lens_change_commands(expanded["stages"][2]),
                         ["yan f2 10", "yan f3 14", "vuy f3 0.2", "wtw w1 2"])
        self.assertEqual(expanded["stages"][1]["variables"], expanded["stages"][2]["variables"])
        self.assertIsNot(expanded["stages"][1]["variables"], expanded["stages"][2]["variables"])

    def test_then_stages_run_after_the_last_step(self):
        value = copy.deepcopy(RAMP)
        value["field_ramp"]["then"] = [{"name": "polish", "variables": [{"surface": 1, "parameter": "radius"}],
                                        "error_function": {"MXC": 60, "IMP": 0.001}}]
        expanded = expand_field_ramp(value)
        validate_aut_spec(expanded)
        self.assertEqual([s["name"] for s in expanded["stages"]], ["focus", "ramp-1", "ramp-2", "polish"])
        self.assertNotIn("ramp_step", expanded["stages"][-1])
        value["field_ramp"]["then"] = "polish"
        with self.assertRaisesRegex(ValueError, "then"):
            expand_field_ramp(value)
        value["field_ramp"]["then"] = [{"name": "polish"}]
        with self.assertRaisesRegex(ValueError, "Each stage needs"):
            validate_aut_spec(expand_field_ramp(value))

    def test_a_spec_without_a_ramp_is_returned_unchanged(self):
        value = spec()
        self.assertIs(expand_field_ramp(value), value)

    def test_a_ramp_alone_is_a_valid_spec(self):
        value = copy.deepcopy(RAMP)
        del value["stages"]
        self.assertEqual(len(expand_field_ramp(value)["stages"]), 2)

    def test_the_stage_limit_covers_the_expanded_stages(self):
        value = copy.deepcopy(RAMP)
        value["stages"] = value["stages"] * 1
        value["field_ramp"]["steps"] = [{"fields": [{"field": 2, "y_angle": 1 + n}]} for n in range(12)]
        value["stages"] += [dict(value["stages"][0], name=f"extra-{n}") for n in range(8)]
        with self.assertRaisesRegex(ValueError, "stages"):
            validate_aut_spec(expand_field_ramp(value))

    def test_ramp_rejections(self):
        cases = [
            (lambda r: r["field_ramp"].update(steps=[]), "1 to"),
            (lambda r: r["field_ramp"].update(steps=[{"fields": [{"field": 2, "y_angle": 1}]}] * 13), "1 to"),
            (lambda r: r["field_ramp"]["steps"][0]["fields"].append({"field": 2, "y_angle": 3}), "twice"),
            (lambda r: r["field_ramp"]["steps"][0]["fields"].append({"field": 4}), "at least one"),
            (lambda r: r["field_ramp"]["steps"][0]["fields"][0].update(command="in x"), "entry is a field"),
            (lambda r: r["field_ramp"]["steps"][0].update(macro="x"), "1 to 10 fields"),
            (lambda r: r["field_ramp"]["stage"].update(name="own"), "template"),
            (lambda r: r["field_ramp"]["stage"].update(ramp_step=3), "template"),
            (lambda r: r["field_ramp"].update(name="bad name;"), "name"),
            (lambda r: r["field_ramp"].update(extra=1), "steps and stage"),
            (lambda r: r["field_ramp"].pop("stage"), "steps and stage"),
            (lambda r: r["field_ramp"]["steps"][0]["fields"][0].update(y_angle=95), "y_angle"),
            (lambda r: r["field_ramp"]["stage"].update(variables=[]), "1 to 60 variables"),
        ]
        for change, message in cases:
            value = copy.deepcopy(RAMP)
            change(value)
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                validate_aut_spec(expand_field_ramp(value))

    def test_a_spec_file_with_a_ramp_is_read_unexpanded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ramp.json"
            path.write_text(json.dumps(RAMP), encoding="utf-8")
            value, digest = read_aut_spec(path)
            self.assertIn("field_ramp", value)
            self.assertEqual(len(digest), 64)
            bad = copy.deepcopy(RAMP)
            bad["field_ramp"]["steps"] = []
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_aut_spec(path)

    def test_the_stage_validator_still_rejects_a_raw_ramp(self):
        with self.assertRaisesRegex(ValueError, "expanded"):
            validate_aut_spec(RAMP)


class CommandTests(unittest.TestCase):
    def test_commands_are_built_from_typed_fields(self):
        stage = spec()["stages"][0]
        self.assertEqual(lens_change_commands(stage), ["wtw w1 0", "wtf f2 0.5"])
        self.assertEqual(open_commands(stage), ["frz s0..i", "ccy s1 0", "thc s2 0"])
        commands = aut_commands(stage, 13, 600)
        self.assertEqual(commands[:6], ["aut", "err cdv", "mxc 50", "mnc 5", "tim 10", "tar 0"])
        for expected in ("imp 0.01", "vli y", "rdy s1 > 20 < 90", "thi s2 > 0.1", "efl = 50",
                         "diy f3 > -0.005 < 0.005", "oal s1..11 < 40", "et s1 > 1", "mnt 1", "mne 0.5"):
            self.assertIn(expected, commands)
        self.assertEqual(constraint_commands([{"operand": "OAL", "relation": "<", "value": 20, "surfaces": [3, 5]}], 13),
                         ["oal s3..5 < 20"])

    def test_restore_only_changed_numeric_codes(self):
        controls = {"0": {"CCY": "100", "THC": "100"}, "1": {"CCY": "0", "THC": "100"},
                    "2": {"CCY": "100", "THC": "0"}, "11": {"CCY": "0", "THC": "PIM"},
                    "12": {"CCY": "100", "THC": "0"}}
        stage = {"variables": [{"surface": 1, "parameter": "radius"}, {"surface": 2, "parameter": "radius"}]}
        self.assertEqual(restore_commands(controls, stage), ["ccy s2 100", "thc s2 0", "ccy s11 0", "thc s12 0"])


class ListingTests(unittest.TestCase):
    def test_variable_table_accepts_composite_bending_rows(self):
        text = ("VARIABLE LIST\n        NO      PARAMETERS *\n         1      CUY S1       CUY S2\n"
                "         2      CUY S2\n         3      THI S2\n    * Multiple entries on a line are composite\n"
                "     3 VARIABLES\n  4 THI S9\n")
        self.assertEqual(parse_variable_table(text), {"CUY S1", "CUY S2", "THI S2"})
        with self.assertRaises(ValueError):
            parse_variable_table("no list")

    def test_real_constrained_output_and_judgement(self):
        parsed = parse_aut_listing((EVIDENCE / "E3-constraints-terminal.txt").read_text(encoding="utf-8"))
        self.assertEqual((parsed["completion"], parsed["final_cycle"]), ("Maximum cycle limit reached", 3))
        self.assertAlmostEqual(parsed["initial_error"], 200.12345678)
        self.assertAlmostEqual(parsed["final_error"], 100.23456789)
        stage = {"constraints": [{"operand": "EFL", "relation": "=", "value": 100},
                                 {"operand": "OAL", "relation": "<", "value": 76, "surfaces": [1, 11]},
                                 {"operand": "IMD", "relation": ">", "value": 60},
                                 {"operand": "DIY", "field": 3, "relation": "<", "value": 0.005},
                                 {"operand": "ET", "surface": 1, "relation": ">", "value": 1.5},
                                 {"operand": "CT", "surface": 2, "relation": ">", "value": 1}]}
        items = {item["label"] + item["relation"]: item for item in judge_constraints(stage, parsed, 13)}
        self.assertEqual(items["EFL="]["status"], "violated")
        self.assertAlmostEqual(items["EFL="]["diff"], -0.03)
        self.assertEqual(items["OAL S1..11<"]["status"], "satisfied")
        self.assertEqual(items["IMD>"]["status"], "satisfied")
        self.assertEqual(items["DIY F3<"]["status"], "satisfied")
        self.assertEqual(items["ET S1>"]["value"], 2.3)
        self.assertIn("not printed", items["CT S2>"]["reason"])

    def test_general_and_frozen_names_from_the_redirected_tail(self):
        parsed = parse_aut_listing((EVIDENCE / "E6-300-forced-quiet-tail.txt").read_text(encoding="utf-8"))
        self.assertEqual(parsed["active_general"], ["Mn CT S2"])
        self.assertEqual(parsed["frozen_violations"], ["Mn ET S1", "Mn ET S3"])
        stage = {"constraints": [{"operand": "EFL", "relation": "=", "value": 100}]}
        self.assertEqual(judge_constraints(stage, parsed, 13)[0]["status"], "satisfied")

    def test_incomplete_or_failed_output_is_refused(self):
        text = (EVIDENCE / "E3-constraints-terminal.txt").read_text(encoding="utf-8")
        for broken in (text.replace("Normal AUTO Completion", "Do Next Cycle?"), "",
                       text + "\nError:   invalid command\n", text.replace("ERR. F.", "ERR F")):
            with self.subTest(size=len(broken)), self.assertRaises(ValueError):
                parse_aut_listing(broken)

    def test_printed_precision_and_tolerance(self):
        row = " IMD                    >   6.00000E+01   6.00000E+01  -4.000E-05"
        text = f"CYCLE NUMBER 1:\n ERR. F. = 1.0\n Inactive Constraints:\n{row}\n Normal AUTO Completion - done\n"
        parsed = parse_aut_listing(text)
        stage = {"constraints": [{"operand": "IMD", "relation": ">", "value": 60}]}
        self.assertEqual(judge_constraints(stage, parsed, 13)[0]["status"], "satisfied")
        stage["constraints"][0]["tolerance"] = 1e-6
        self.assertEqual(judge_constraints(stage, parsed, 13)[0]["status"], "violated")
        parsed["constraint_rows"][0].update(diff=-1.0000005e-6, printed={**parsed["constraint_rows"][0]["printed"], "diff": "-1.000E-06"})
        self.assertEqual(judge_constraints(stage, parsed, 13)[0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
