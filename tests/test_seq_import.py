"""Sequence import (E4): whitelist parsing, whole-file refusal, typed plan and the verified run."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codev_mcp import seq_import
from codev_mcp.com_backend import ComBackend
from codev_mcp.models import CreateLensRequest, FieldSetReplacement, UpdateRequest
from codev_mcp.seq_import import (
    SeqRejected,
    compare_lenses,
    describe,
    parse_seq,
    plan_import,
    run_import,
    verify_against_sequence,
)
from tests.fake_codev import FakeCodeVSession

DATA = Path(__file__).parent / "data" / "project-owned"

MINIMAL = """RDM;LEN "VERSION: 10.2"
TITLE 'Singlet'
EPD 20
DIM M
WL 656.3 587.6 486.1
REF 2
WTW 1 2 1
XAN 0 0
YAN 0 5
WTF 1 1
SO 0.0 0.1e11
S 50 4 BK7_SCHOTT
  STO
S -50 30
  PIM
SI 0.0 0.0
GO
"""


def variant(old: str, new: str, text: str = MINIMAL) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def reasons(text: str) -> list[str]:
    with unittest.TestCase().assertRaises(SeqRejected) as caught:
        parse_seq(text)
    return [item["reason"] for item in caught.exception.problems]


class ParseProjectSequences(unittest.TestCase):
    def test_a_wrl_export_of_dbgauss_is_read_completely(self):
        parsed = parse_seq((DATA / "compound.seq").read_text(encoding="utf-8"))
        self.assertEqual((parsed.units, parsed.aperture_kind, parsed.aperture_value), ("M", "epd", 50.0))
        self.assertEqual(len(parsed.surfaces), 11)
        self.assertEqual(parsed.stop_surface, 6)
        self.assertEqual(parsed.image_solve, "pim")
        self.assertIsNone(parsed.surfaces[3].radius)  # "S 0.0 ..." is a plane
        self.assertEqual(parsed.surfaces[0].glass, "BK7_SCHOTT")
        self.assertEqual(parsed.fields["YAN"], [0.0, 10.0000000023, 14.0000000032])
        self.assertEqual(parsed.fields["VUY"], [0.0, 0.2, 0.4])
        self.assertNotIn("VUX", parsed.fields)
        self.assertEqual(parsed.reference, 2)
        counts = seq_import.summarise_not_imported(parsed.not_imported)
        self.assertEqual(counts, {"LEN": 1, "TITLE": 1, "INI": 1, "CCY": 8, "THC": 5})

    def test_wideang_reads_and_has_no_vignetting_to_apply(self):
        parsed = parse_seq((DATA / "wide.seq").read_text(encoding="utf-8"))
        self.assertEqual(len(parsed.surfaces), 13)
        self.assertEqual(parsed.fields["YAN"], [0.0, 25.2, 36.0])
        self.assertIsNone(plan_import(parsed).field_set)

    def test_cooke_is_refused_with_every_offending_line(self):
        with self.assertRaises(SeqRejected) as caught:
            parse_seq((DATA / "refused.seq").read_text(encoding="utf-8"))
        problems = caught.exception.problems
        self.assertEqual([item["text"] for item in problems if "CIR" in item["text"]], ["CIR 7.0", "CIR 6.5"])
        self.assertTrue(any("defocused" in item["reason"] for item in problems))
        self.assertTrue(all(item["line"] > 0 for item in problems))
        self.assertIn("refused", str(caught.exception))


class ParseRules(unittest.TestCase):
    def test_comments_blank_lines_case_and_continuations_are_accepted(self):
        text = variant("WL 656.3 587.6 486.1", "! a comment\n\nwl 656.3 &\n   587.6 486.1")
        self.assertEqual(parse_seq(text).wavelengths_nm, [656.3, 587.6, 486.1])

    def test_a_second_command_on_one_line_is_checked_like_any_other(self):
        for injected in ("TITLE 'x'; IN evil.seq", "EPD 20; OUT file", "RDM; IN macro"):
            with self.subTest(injected=injected):
                self.assertTrue(any("not a lens data command" in r for r in reasons(
                    variant("TITLE 'Singlet'", injected))))

    def test_a_semicolon_inside_a_quoted_string_is_text(self):
        parsed = parse_seq(variant("TITLE 'Singlet'", "TITLE 'a; IN b'"))
        self.assertIn("TITLE", [item["keyword"] for item in parsed.not_imported])

    def test_every_non_whitelisted_command_refuses_the_file(self):
        for command in ("IN macro.seq", "CIR S1 CLR 5", "ASP", "K -1", "PIK RDY S2 1", "ZOO 2", "SPS", "GLA S1 X",
                        "AUT", "OUT T", "CUY S1 0.1", "THI S1 5", "SLV", "BOGUS 1"):
            with self.subTest(command=command):
                self.assertTrue(any("not a lens data command" in r or "only accepted" in r
                                    for r in reasons(variant("GO", command + "\nGO"))))

    def test_surface_details_that_are_not_supported_are_refused(self):
        cases = {
            "S 50 4 BK7": "NAME_CATALOG",
            "S 50": "radius, a thickness",
            "S 50 abc": "not a number",
            "S 50 4 BK7_SCHOTT extra": "radius, a thickness",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertTrue(any(expected in r for r in reasons(variant("S 50 4 BK7_SCHOTT", line))))

    def test_object_and_image_conditions(self):
        self.assertTrue(any("object at infinity" in r for r in reasons(variant("SO 0.0 0.1e11", "SO 0.0 500"))))
        self.assertTrue(any("object at infinity" in r for r in reasons(variant("SO 0.0 0.1e11", "SO 5.0 0.1e11"))))
        self.assertTrue(any("zero thickness" in r for r in reasons(variant("SI 0.0 0.0", "SI 0.0 0.5"))))
        self.assertTrue(any("SI (the image surface) is missing" in r for r in reasons(variant("SI 0.0 0.0\n", ""))))

    def test_structure_rules(self):
        self.assertTrue(any("strictly descending" in r for r in reasons(variant("486.1", "700"))))
        self.assertTrue(any("XAN has 3" in r or "has 3 values" in r for r in reasons(variant("XAN 0 0", "XAN 0 0 0"))))
        self.assertTrue(any("exactly one STO" in r for r in reasons(variant("  STO\n", ""))))
        self.assertTrue(any("exactly one STO" in r for r in reasons(variant("  PIM", "  STO\n  PIM"))))
        self.assertTrue(any("last ordinary" in r for r in reasons(variant("  STO\n", "  STO\n  PIM\n"))))
        self.assertTrue(any("more than once" in r for r in reasons(variant("DIM M", "DIM M\nDIM M"))))
        self.assertTrue(any("defined more than once" in r for r in reasons(variant("EPD 20", "EPD 20\nFNO 4"))))
        self.assertTrue(any("REF names" in r for r in reasons(variant("REF 2", "REF 7"))))
        self.assertTrue(any("one weight per wavelength" in r for r in reasons(variant("WTW 1 2 1", "WTW 1 2"))))
        self.assertTrue(any("non-negative integers" in r for r in reasons(variant("WTW 1 2 1", "WTW 1 2.5 1"))))
        self.assertTrue(any("DIM must be" in r for r in reasons(variant("DIM M", "DIM X"))))

    def test_content_after_go_is_refused(self):
        self.assertTrue(any("after GO" in r for r in reasons(MINIMAL + "EPD 30\n")))

    def test_numbers_must_be_plain_finite_numbers(self):
        for token in ("nan", "inf", "1e999", "0x10", "1,5", "--1"):
            with self.subTest(token=token):
                self.assertTrue(reasons(variant("EPD 20", f"EPD {token}")))

    def test_size_and_control_character_limits(self):
        self.assertTrue(reasons(MINIMAL + "\x00"))
        self.assertTrue(reasons(MINIMAL + "! " + "x" * (seq_import.MAX_BYTES + 1)))
        self.assertTrue(reasons("\n" * (seq_import.MAX_LINES + 1)))

    def test_unterminated_strings_are_refused(self):
        self.assertTrue(any("unterminated" in r for r in reasons(variant("TITLE 'Singlet'", "TITLE 'Singlet"))))

    def test_markers_need_a_surface_and_a_number(self):
        self.assertTrue(reasons(variant("EPD 20", "CCY 0\nEPD 20")))
        self.assertTrue(any("number" in r for r in reasons(variant("  STO", "  CCY zero\n  STO"))))

    def test_metadata_is_listed_not_imported(self):
        text = variant("GO", "UID 'abc'\nDOR 1\nDER VAL I1 0.1\nGO")
        listed = {item["keyword"]: item["reason"] for item in parse_seq(text).not_imported}
        self.assertEqual(listed["UID"], "metadata")
        self.assertEqual(listed["DOR"], "unknown system item")
        self.assertIn("AUT", listed["DER"])


class Planning(unittest.TestCase):
    def test_the_dbgauss_plan_is_a_typed_request_pair(self):
        plan = plan_import(parse_seq((DATA / "compound.seq").read_text(encoding="utf-8")))
        self.assertIsInstance(plan.create, CreateLensRequest)
        self.assertEqual(plan.create.image_solve, "pim")
        self.assertEqual(plan.create.stop_surface, 6)
        self.assertIsInstance(plan.field_set, FieldSetReplacement)
        self.assertEqual([item.vuy for item in plan.field_set.fields], [0.0, 0.2, 0.4])
        self.assertEqual(plan.commands[0], "len")
        self.assertIn("pim yes", plan.commands)
        self.assertIn("VUY F3 0.4", plan.commands)
        UpdateRequest(field_set=plan.field_set)

    def test_only_service_built_commands_are_planned(self):
        plan = plan_import(parse_seq((DATA / "compound.seq").read_text(encoding="utf-8")))
        for command in plan.commands:
            self.assertRegex(command.split()[0].lower(),
                             r"^(len|rdm|dim|wl|epd|xan|yan|ins|sto|pim|wtf|vux|vlx|vuy|vly)$")

    def test_values_outside_the_typed_limits_are_refused_before_any_call(self):
        cases = [variant("XAN 0 0", "XAN 0 0\nVUY 0 1.5").replace("WTF 1 1", "WTF 1 1\nVLY 0 0"),
                 variant("YAN 0 5", "YAN 0 95"),
                 variant("EPD 20", "EPD 0"),
                 variant("S 50 4 BK7_SCHOTT", "S 50 4 BK7$_SCHOTT")]
        for text in cases:
            with self.subTest(text=text[-80:]):
                with self.assertRaises(SeqRejected):
                    plan_import(parse_seq(text))

    def test_weights_and_factors_that_are_not_default_need_a_field_set(self):
        plan = plan_import(parse_seq(variant("WTF 1 1", "WTF 1 3")))
        self.assertEqual([item.weight for item in plan.field_set.fields], [1, 3])
        self.assertIsNone(plan_import(parse_seq(MINIMAL)).field_set)

    def test_the_description_lists_commands_and_skipped_items(self):
        parsed = parse_seq(MINIMAL)
        text = describe(parsed, plan_import(parsed))
        self.assertIn("pim yes", text)
        self.assertIn("TITLE", text)


class BackendClient:
    """A stand-in MCP client backed by the real ComBackend on a fake COM session."""

    def __init__(self, work, _backend, _timeout, _log_dir):
        self.session = FakeCodeVSession()
        self.session.listing = self.session._build_listing()
        Path(work).mkdir(parents=True, exist_ok=True)
        self.backend = ComBackend(working_directory=Path(work) / "svc", session=self.session)
        guard = patch.object(self.backend, "_new_session",
                             side_effect=AssertionError("a test tried to start CODE V"))
        guard.start()
        self.guard = guard

    def call(self, name, arguments=None, timeout=None):
        arguments = arguments or {}
        if name == "create_lens":
            result = self.backend.create_lens(CreateLensRequest.model_validate(arguments["request"]))
        elif name == "update_lens":
            result = self.backend.update_lens(UpdateRequest.model_validate(arguments["request"]))
        elif name == "get_lens":
            result = self.backend.get_lens()
        elif name == "open_lens":
            result = self.backend.open_lens(arguments["path"])
        elif name == "save_lens_as":
            result = self.backend.save_lens_as(arguments["path"])
        else:  # pragma: no cover - the import only uses these tools
            raise AssertionError(name)
        return result.model_dump(mode="json"), []

    def close(self):
        self.guard.stop()
        return {"returncode": 0, "close_session": {"session_open": False, "details": {"cleanup_confirmed": True}}}


class RunImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seq = self.root / "lens.seq"
        self.seq.write_text(MINIMAL, encoding="utf-8")

    def run_it(self, output="out.len", **kwargs):
        with patch("codev_mcp.seq_import.ROOT", self.root):
            return run_import(self.seq, self.root / output, output_dir=self.root / "bundles",
                              client_factory=BackendClient, **kwargs)

    def test_a_sequence_becomes_a_verified_saved_lens(self):
        bundle, manifest = self.run_it()
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertTrue((self.root / "out.len").is_file())
        self.assertEqual(manifest["output"]["sha256"], seq_import.digest(self.root / "out.len"))
        checks = manifest["verification"]["checks"]
        self.assertTrue(all(item["passed"] for item in checks), [i for i in checks if not i["passed"]])
        self.assertIn("native solves (LIS)", [item["name"] for item in checks])
        self.assertEqual(manifest["reopen"]["failed"], [])
        self.assertLess(manifest["verification"]["max_relative_deviation"], 1e-9)
        self.assertEqual(manifest["not_imported"]["counts"], {"LEN": 1, "TITLE": 1})
        lens = json.loads((bundle / "lens-imported.json").read_text(encoding="utf-8"))
        self.assertEqual([item["weight"] for item in lens["wavelengths"]], [1, 2, 1])
        self.assertTrue(next(item for item in lens["wavelengths"] if item["number"] == 2)["is_reference"])

    def test_the_execution_record_lists_the_service_built_commands(self):
        bundle, manifest = self.run_it()
        record = json.loads((bundle / "execution-record.json").read_text(encoding="utf-8"))
        step = record["steps"][0]
        self.assertEqual(step["action"], "import_seq")
        self.assertEqual(step["native_commands"][0], "len")
        self.assertIn("pim yes", step["native_commands"])
        self.assertIn("REF 2", step["native_commands"])
        self.assertIn("WTW W2 2", step["native_commands"])
        self.assertNotIn("WTW W1 1", step["native_commands"])
        self.assertIn("never sent", step["command_note"])
        self.assertEqual(step["outputs"][0]["sha256"], manifest["output"]["sha256"])

    def test_dbgauss_is_imported_with_its_vignetting_and_pim(self):
        self.seq.write_text((DATA / "compound.seq").read_text(encoding="utf-8"), encoding="utf-8")
        _, manifest = self.run_it()
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        checks = {item["name"]: item for item in manifest["verification"]["checks"]}
        self.assertTrue(checks["field vuy"]["passed"])
        self.assertEqual(checks["field vuy"]["actual"], [0.0, 0.2, 0.4])
        self.assertTrue(checks["surface 11"]["passed"])

    def test_a_rejected_sequence_writes_a_manifest_and_no_lens(self):
        self.seq.write_text((DATA / "refused.seq").read_text(encoding="utf-8"), encoding="utf-8")
        bundle, manifest = self.run_it()
        self.assertEqual(manifest["status"], "rejected")
        self.assertGreaterEqual(len(manifest["rejected"]), 3)
        self.assertFalse((self.root / "out.len").exists())
        self.assertTrue((bundle / "manifest.json").is_file())

    def test_an_existing_output_or_a_wrong_input_is_refused_up_front(self):
        (self.root / "out.len").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "new .len"):
            self.run_it()
        with self.assertRaisesRegex(ValueError, "existing .seq"):
            with patch("codev_mcp.seq_import.ROOT", self.root):
                run_import(self.root / "missing.seq", self.root / "new.len", client_factory=BackendClient)

    def test_the_imported_lens_can_be_compared_with_a_reference_lens(self):
        bundle, manifest = self.run_it()
        reference = self.root / "reference.len"
        reference.write_bytes((self.root / "out.len").read_bytes())
        _, second = self.run_it("second.len", reference_lens=reference)
        self.assertEqual(second["status"], "succeeded", second.get("error"))
        self.assertEqual(second["reference"]["differences"], [])
        self.assertTrue(second["reference"]["unchanged"])

    def test_a_different_reference_lens_fails_the_run_and_lists_the_differences(self):
        reference = self.root / "other.len"
        reference.write_text("! not the imported lens\n", encoding="utf-8")
        _, manifest = self.run_it(reference_lens=reference)
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(manifest["reference"]["differences"])
        self.assertFalse((self.root / "out.len").exists())

    def test_a_read_back_that_disagrees_with_the_sequence_fails_the_run(self):
        class Disagreeing(BackendClient):
            def call(self, name, arguments=None, timeout=None):
                result, images = super().call(name, arguments, timeout)
                if name == "get_lens":
                    result["fields"][1]["y_angle"] = 6.0
                return result, images

        with patch("codev_mcp.seq_import.ROOT", self.root):
            _, manifest = run_import(self.seq, self.root / "out.len", output_dir=self.root / "bundles",
                                     client_factory=Disagreeing)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("does not match the sequence", manifest["error"])
        self.assertIn("field y_angles", manifest["error"])
        self.assertFalse((self.root / "out.len").exists())

    def test_a_construction_the_service_itself_cannot_verify_fails_the_run(self):
        original = FakeCodeVSession.command_raw

        def wrong_wavelength(session, text):
            return original(session, text.replace("656.3", "650") if text.lower().startswith("wl ") else text)

        with patch.object(FakeCodeVSession, "command_raw", wrong_wavelength):
            _, manifest = self.run_it()
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("did not match the typed request", manifest["error"])
        self.assertFalse((self.root / "out.len").exists())


class VerificationHelpers(unittest.TestCase):
    def lens(self):
        return {
            "units": "mm", "aperture": {"kind": "epd", "value": 20.0}, "stop_surface": 1, "zoom_positions": 1,
            "wavelengths": [{"number": 1, "micrometers": 0.6563, "weight": 1, "is_reference": False},
                            {"number": 2, "micrometers": 0.5876, "weight": 2, "is_reference": True},
                            {"number": 3, "micrometers": 0.4861, "weight": 1, "is_reference": False}],
            "fields": [{"number": 1, "x_angle": 0, "y_angle": 0, "weight": 1, "vux": 0, "vlx": 0, "vuy": 0, "vly": 0},
                       {"number": 2, "x_angle": 0, "y_angle": 5, "weight": 1, "vux": 0, "vlx": 0, "vuy": 0, "vly": 0}],
            "surfaces": [{"radius_is_infinite": True, "thickness_is_infinite": True, "role": "object", "is_stop": False},
                         {"radius": 50, "radius_is_infinite": False, "thickness": 4, "thickness_is_infinite": False,
                          "glass": "BK7_SCHOTT", "role": "surface", "is_stop": True},
                         {"radius": -50, "radius_is_infinite": False, "thickness": 47.7, "thickness_is_infinite": False,
                          "glass": None, "role": "surface", "is_stop": False},
                         {"radius_is_infinite": True, "thickness": 0, "thickness_is_infinite": False,
                          "role": "image", "is_stop": False}],
            "raw_listing": "SPECIFICATION DATA\n   EPD       20.00000\n   DIM             MM\n"
                           "   WL          656.30    587.60    486.10\n   REF              2\n"
                           "   XAN        0.00000       0.00000\n   YAN        0.00000       5.00000\n"
                           "   WTF        1.00000       1.00000\n"
                           " REFRACTIVE INDICES\n SOLVES\n    PIM\n No pickups defined in system\n"
                           " INFINITE CONJUGATES\n",
        }

    def test_a_matching_lens_passes_every_check(self):
        text = variant("S -50 30", "S -50 47.7")
        checks, worst, warnings = verify_against_sequence(self.lens(), parse_seq(text))
        self.assertTrue(all(item["passed"] for item in checks), [i for i in checks if not i["passed"]])
        self.assertEqual(warnings, [])
        self.assertLess(worst, 1e-12)

    def test_a_pim_thickness_that_moved_is_a_warning_not_a_failure(self):
        checks, _, warnings = verify_against_sequence(self.lens(), parse_seq(MINIMAL))  # started at 30
        self.assertTrue(all(item["passed"] for item in checks))
        self.assertIn("PIM solve re-derived surface 2", warnings[0])

    def test_each_kind_of_difference_is_caught(self):
        mutations = {
            "units": lambda lens: lens.update(units="cm"),
            "system aperture": lambda lens: lens["aperture"].update(value=21.0),
            "wavelength weights": lambda lens: lens["wavelengths"][0].update(weight=5),
            "reference wavelength": lambda lens: lens["wavelengths"][0].update(is_reference=True)
            or lens["wavelengths"][1].update(is_reference=False),
            "field y_angles": lambda lens: lens["fields"][1].update(y_angle=6),
            "field weights": lambda lens: lens["fields"][1].update(weight=2),
            "field vuy": lambda lens: lens["fields"][1].update(vuy=0.1),
            "stop surface": lambda lens: lens.update(stop_surface=2),
            "surface 1": lambda lens: lens["surfaces"][1].update(glass="SK16_SCHOTT"),
            "native solves (LIS)": lambda lens: lens.update(raw_listing=lens["raw_listing"].replace("    PIM", "    CUY")),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                lens = self.lens()
                mutate(lens)
                failed = [item["name"] for item in verify_against_sequence(lens, parse_seq(MINIMAL))[0]
                          if not item["passed"]]
                self.assertIn(name, failed)

    def test_compare_lenses_lists_differences_and_ignores_titles(self):
        first, second = self.lens(), self.lens()
        first["title"], second["title"] = "a", "b"
        self.assertEqual(compare_lenses(first, second), [])
        second["surfaces"][1]["radius"] = 51
        second["fields"][0]["weight"] = 2
        problems = compare_lenses(first, second)
        self.assertTrue(any("surface 1 radius" in item for item in problems))
        self.assertTrue(any("field 0 weight" in item for item in problems))


if __name__ == "__main__":
    unittest.main()
