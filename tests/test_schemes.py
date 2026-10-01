"""Scheme comparison (E6): file validation, metrics, comparability, recommendation and the run loop."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codev_mcp import schemes
from codev_mcp.cli_cleanup import CleanupError, cleanup_in_doubt
from codev_mcp.schemes import (comparability, extract_metrics, read_scheme_set, recommend, render_summary,
                               run_schemes)

from tests.publication_fixtures import aut_report
REPORT = aut_report()

AUT_SPEC = {"schema_version": 1, "kind": "aut_spec", "name": "tiny", "wall_seconds": 60, "stages": [
    {"name": "one", "variables": [{"surface": 1, "parameter": "radius"}], "error_function": {"MXC": 2}}]}
GLASS = [{"target": "surface", "surface": 4, "parameter": "glass", "value": "HF1_CDGM"}]


def scheme_file(*items, **extra) -> dict:
    return {"schema_version": 1, "kind": "scheme_set", "name": "demo", "schemes": list(items), **extra}


def report_for(*, error=100.0, rms=0.1, vuy=0.4, satisfied=True, bounds=True, weight=1.0, rays=None) -> dict:
    value = copy.deepcopy(REPORT)
    value.update(final_error=error, all_constraints_satisfied=satisfied, explicit_bounds_satisfied=bounds,
                 state="complete", candidate_path="C:/x/candidate.len", candidate_sha256="abc")
    value["after_wavefront"]["result"]["weighted_rms_waves"] = rms
    for field, count in zip(value["after_wavefront"]["result"]["fields"], rays or []):
        field["rays_traced"] = count
    fields = value["candidate_snapshot"]["zooms"][0]["fields"]
    fields[2]["vuy"] = vuy
    fields[2]["weight"] = weight
    return value


class SchemeFileTests(unittest.TestCase):
    def write(self, payload) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "schemes.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_valid_file_normalises_edits_and_field_sets(self):
        path = self.write(scheme_file(
            {"name": "base"}, {"name": "cdgm", "reason": "swap", "edits": GLASS},
            {"name": "vig", "field_set": {"fields": [{"y_angle": 0}, {"y_angle": 10, "vuy": 0.1}]}},
            {"name": "own", "aut_spec": "other.json"}))
        payload, digest = read_scheme_set(path)
        self.assertEqual(len(digest), 64)
        self.assertEqual(payload["schemes"][1]["edits"][0]["value"], "HF1_CDGM")
        self.assertEqual(payload["schemes"][2]["field_set"]["fields"][0]["x_angle"], 0.0)

    def test_rejections(self):
        cases = [
            (scheme_file(), "1 to 8"),
            (scheme_file(*[{"name": f"s{n}"} for n in range(9)]), "1 to 8"),
            (scheme_file({"name": "a"}, {"name": "a"}), "Duplicate"),
            (scheme_file({"name": "bad name"}), "Scheme names"),
            (scheme_file({"name": "a", "edits": GLASS, "field_set": {"fields": [{"y_angle": 0}]}}), "not both"),
            (scheme_file({"name": "a", "edits": []}), "1 to 100"),
            (scheme_file({"name": "a", "edits": [{"target": "surface", "parameter": "radius", "value": 1}]}), "surface"),
            (scheme_file({"name": "a", "command": "in x"}), "takes name"),
            (scheme_file({"name": "a", "aut_spec": 5}), "aut_spec"),
            (scheme_file({"name": "a"}, extra=1), "schema_version 1"),
            ({**scheme_file({"name": "a"}), "kind": "other"}, "schema_version 1"),
        ]
        for payload, message in cases:
            with self.subTest(message):
                if "extra" in payload:
                    payload = {**scheme_file({"name": "a"}), "extra": 1}
                path = self.write(payload)
                with self.assertRaisesRegex(ValueError, message):
                    read_scheme_set(path)


class MetricTests(unittest.TestCase):
    def test_metrics_come_straight_from_the_aut_report(self):
        metrics = extract_metrics(REPORT)
        self.assertEqual(metrics["stages"], 3)
        self.assertEqual(metrics["final_error"], REPORT["final_error"])
        self.assertEqual(metrics["first_order"]["effective_focal_length"],
                         REPORT["after_first_order"]["effective_focal_length"])
        self.assertEqual(metrics["wavefront"]["weighted_rms_waves"],
                         REPORT["after_wavefront"]["result"]["weighted_rms_waves"])
        self.assertEqual(metrics["wavefront_start"]["weighted_rms_waves"],
                         REPORT["before_wavefront"]["result"]["weighted_rms_waves"])
        self.assertTrue(metrics["all_constraints_satisfied"])
        self.assertEqual(metrics["violated_constraints"], [])
        self.assertIsNotNone(metrics["error_signature"])

    def test_a_failed_wavefront_diagnostic_leaves_the_value_out(self):
        report = copy.deepcopy(REPORT)
        report["after_wavefront"] = {"state": "failed", "error": "x"}
        self.assertIsNone(extract_metrics(report)["wavefront"])

    def test_signatures_follow_fields_weights_vignetting_and_pupil(self):
        base = extract_metrics(report_for())
        self.assertEqual(base["error_signature"], extract_metrics(report_for(error=5.0, rms=0.5))["error_signature"])
        for changed in (report_for(vuy=0.2), report_for(weight=2.0)):
            self.assertNotEqual(base["wave_signature"], extract_metrics(changed)["wave_signature"])
        other = report_for()
        other["spec"]["stages"][-1]["error_function"]["DEL"] = 0.2
        self.assertEqual(base["wave_signature"], extract_metrics(other)["wave_signature"])
        self.assertNotEqual(base["error_signature"], extract_metrics(other)["error_signature"])


    def test_the_accepted_pupil_and_the_unweighted_rms_are_derived_from_the_printed_values(self):
        wave = extract_metrics(report_for(rays=[948, 900, 474]))["wavefront"]
        self.assertEqual(wave["pupil_fractions"], [1.0, 900 / 948, 0.5])
        fields = REPORT["after_wavefront"]["result"]["fields"]
        expected = (sum(f["rms_waves"] ** 2 for f in fields) / len(fields)) ** 0.5  # all field weights are 1
        self.assertAlmostEqual(wave["equal_field_rms_waves"], expected, places=9)

    def test_missing_ray_counts_leave_the_pupil_ratio_out(self):
        report = report_for()
        del report["after_wavefront"]["result"]["fields"][1]["rays_traced"]
        wave = extract_metrics(report)["wavefront"]
        self.assertIsNone(wave["pupil_fractions"])
        self.assertIsNotNone(wave["equal_field_rms_waves"])


def entry(name, **kwargs) -> dict:
    return {"name": name, "status": "succeeded", "metrics": extract_metrics(report_for(**kwargs)),
            "aut": {"result": f"C:/out/{name}/aut/result.json", "root": f"C:/out/{name}/aut"},
            "directory": f"C:/out/{name}", "candidate": {"path": "x", "sha256": "y"}}


class ComparabilityTests(unittest.TestCase):
    def test_schemes_with_the_same_setup_compare_directly(self):
        found = comparability([entry("a"), entry("b", error=50.0)])
        self.assertEqual(found["error_function"], [["a", "b"]])
        self.assertEqual(found["notes"], [])

    def test_different_vignetting_splits_the_groups_and_says_so(self):
        found = comparability([entry("a"), entry("b", vuy=0.2), entry("c", vuy=0.2)])
        self.assertEqual(found["error_function"], [["b", "c"], ["a"]])
        self.assertEqual(len(found["notes"]), 2)
        self.assertIn("不能直接比较", found["notes"][0])
        self.assertEqual(found["first_order"], [["a", "b", "c"]])

    def test_failed_schemes_are_left_out(self):
        found = comparability([entry("a"), {"name": "b", "status": "failed", "error": "x"}])
        self.assertEqual(found["first_order"], [["a"]])


class PupilTests(unittest.TestCase):
    def test_ratios_within_three_points_share_a_pupil_and_larger_gaps_split_it(self):
        items = [entry("a", rays=[948, 900, 800]), entry("b", rays=[948, 890, 790]),
                 entry("c", rays=[948, 900, 700])]
        found = comparability(items)
        self.assertEqual(found["pupil"], [["a", "b"], ["c"]])
        self.assertTrue(any("光瞳不同" in note for note in found["notes"]))
        # The groups only mark WAV values; the error function grouping is unchanged.
        self.assertEqual(found["error_function"], [["a", "b", "c"]])

    def test_a_scheme_without_ray_counts_is_left_out_of_the_pupil_groups(self):
        without = entry("b")
        without["metrics"]["wavefront"]["pupil_fractions"] = None
        found = comparability([entry("a", rays=[948, 900, 800]), without])
        self.assertEqual(found["pupil"], [["a"]])

    def test_recommendation_keeps_its_choice_but_warns_about_a_different_pupil(self):
        items = [entry("a", rms=0.3, rays=[948, 900, 800]), entry("b", rms=0.1, rays=[948, 900, 800]),
                 entry("c", rms=0.05, rays=[948, 800, 500])]
        found = recommend(items, comparability(items))
        self.assertEqual(found["scheme"], "c")
        self.assertIn("pupil_caveat", found)
        self.assertIn("a、b", found["pupil_caveat"])
        same = [entry("a", rms=0.3, rays=[948, 900, 800]), entry("b", rms=0.1, rays=[948, 905, 805])]
        self.assertNotIn("pupil_caveat", recommend(same, comparability(same)))

    def test_the_summary_lists_the_ratios_and_both_composite_values(self):
        items = [entry("a", rays=[948, 900, 800]), entry("c", rays=[948, 800, 500])]
        manifest = {"name": "demo", "source": "codev", "status": "succeeded",
                    "baseline": {"path": "x", "sha256": "0" * 64, "unchanged": True},
                    "schemes": items, "comparability": comparability(items),
                    "recommendation": recommend(items, comparability(items))}
        text = render_summary(manifest)
        self.assertIn("## WAV 接受光瞳比", text)
        self.assertIn("1.000、0.949、0.844", text)
        self.assertIn("光瞳不同", text)
        self.assertIn("**注意**", text)


class RecommendationTests(unittest.TestCase):
    def test_the_best_wavefront_in_the_largest_group_wins(self):
        items = [entry("a", rms=0.3), entry("b", rms=0.1), entry("c", rms=0.05, vuy=0.1)]
        found = recommend(items, comparability(items))
        self.assertEqual(found["scheme"], "b")  # c is smaller but not comparable
        self.assertEqual(found["excluded_from_comparison"], ["c"])
        self.assertIn("aut accept", found["accept_command"])
        self.assertIn("只推荐", found["criteria"])

    def test_constraint_or_bound_violations_and_failed_specs_are_not_eligible(self):
        items = [entry("a", rms=0.3), entry("b", rms=0.1, satisfied=False), entry("c", rms=0.05, bounds=False)]
        self.assertEqual(recommend(items, comparability(items))["scheme"], "a")
        items[0]["evaluation"] = {"status": "fail"}
        found = recommend(items, comparability(items))
        self.assertIsNone(found["scheme"])
        self.assertIn("没有同时满足", found["reason"])

    def test_no_single_largest_group_gives_no_recommendation(self):
        # Two groups of equal size: choosing either by name would be arbitrary.
        items = [entry("a", rms=0.3), entry("b", rms=0.05, vuy=0.1)]
        found = recommend(items, comparability(items))
        self.assertIsNone(found["scheme"])
        self.assertIn("不唯一", found["reason"])
        self.assertNotIn("accept_command", found)

    def test_no_completed_scheme_gives_no_recommendation(self):
        found = recommend([{"name": "a", "status": "failed"}], comparability([]))
        self.assertIsNone(found["scheme"])
        self.assertNotIn("accept_command", found)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = self.root / "base.len"
        self.baseline.write_bytes(b"baseline lens")
        self.aut_path = self.root / "aut.json"
        self.aut_path.write_text(json.dumps(AUT_SPEC), encoding="utf-8")
        self.edit_calls, self.aut_calls, self.eval_calls = [], [], []
        self.reports = {}
        self.fail_aut = set()
        self.cleanup_doubt = set()

    def scheme_path(self, *items) -> Path:
        path = self.root / "schemes.json"
        path.write_text(json.dumps(scheme_file(*items)), encoding="utf-8")
        return path

    def edit_runner(self, source, edits_path, output, **kwargs):
        edits = json.loads(edits_path.read_text(encoding="utf-8"))
        self.edit_calls.append((edits, kwargs))
        output.write_bytes(b"edited " + edits["name"].encode())
        bundle = kwargs["output_dir"] / "bundle"
        bundle.mkdir(parents=True)
        return bundle, {"status": "succeeded", "output": {"sha256": "e" * 64}}

    def aut_runner(self, source, root, spec, *, spec_sha256):
        name = root.parent.name
        self.aut_calls.append((name, Path(source).read_bytes(), spec["name"], spec_sha256))
        root.mkdir(parents=True)
        if name in self.cleanup_doubt:
            raise CleanupError("AUT baseline failed: x; cleanup remaining: [4242]", remaining=[4242])
        if name in self.fail_aut:
            raise RuntimeError("AUT candidate was discarded: TimeoutExpired")
        return self.reports.get(name) or report_for()

    def evaluator(self, initial, final, output_dir, **kwargs):
        self.eval_calls.append((initial, kwargs))
        return output_dir, {"status": "succeeded", "evaluation": {"status": {"lens": "pass"}},
                            "failed_analyses": None}

    def execute(self, *items, design_spec=None, **kwargs):
        return run_schemes(self.baseline, self.scheme_path(*items), self.aut_path, self.root / "out",
                           design_spec=design_spec, edit_runner=self.edit_runner, aut_runner=self.aut_runner,
                           evaluator=self.evaluator, **kwargs)

    def test_three_schemes_run_in_order_and_are_summarised(self):
        self.reports = {"cdgm": report_for(error=80.0, rms=0.08), "hoya": report_for(error=90.0, rms=0.07)}
        directory, manifest = self.execute(
            {"name": "schott", "reason": "baseline glasses"},
            {"name": "cdgm", "reason": "swap", "edits": GLASS},
            {"name": "hoya", "edits": [dict(GLASS[0], value="HF2_HOYA")]})
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual([item["name"] for item in manifest["schemes"]], ["schott", "cdgm", "hoya"])
        self.assertEqual([call[0] for call in self.aut_calls], ["schott", "cdgm", "hoya"])
        # The baseline scheme optimises a copy of the baseline; the others their edited copy.
        self.assertEqual(self.aut_calls[0][1], b"baseline lens")
        self.assertEqual(self.aut_calls[1][1], b"edited scheme-cdgm")
        self.assertEqual(len(self.edit_calls), 2)
        self.assertEqual(self.edit_calls[0][0]["edits"][0]["value"], "HF1_CDGM")
        self.assertEqual(self.edit_calls[0][0]["reason"], "swap")
        self.assertNotIn("reason", self.edit_calls[1][0])
        self.assertEqual(manifest["recommendation"]["scheme"], "hoya")
        self.assertTrue(manifest["baseline"]["unchanged"])
        summary = (directory / "summary.md").read_text(encoding="utf-8")
        self.assertIn("所有候选都未被接受", summary)
        self.assertIn("hoya", summary.split("## 推荐")[1])
        self.assertEqual(json.loads((directory / "summary.json").read_text(encoding="utf-8"))["status"], "succeeded")
        record = json.loads((directory / "execution-record.json").read_text(encoding="utf-8"))["steps"][0]
        self.assertEqual(record["action"], "scheme_compare")
        self.assertEqual(record["native_commands"], [])

    def test_a_failed_scheme_is_recorded_and_the_rest_still_run(self):
        self.fail_aut = {"cdgm"}
        _, manifest = self.execute({"name": "schott"}, {"name": "cdgm", "edits": GLASS}, {"name": "hoya", "edits": GLASS})
        states = {item["name"]: item["status"] for item in manifest["schemes"]}
        self.assertEqual(states, {"schott": "succeeded", "cdgm": "failed", "hoya": "succeeded"})
        self.assertEqual(manifest["status"], "partial")
        self.assertIn("discarded", next(i for i in manifest["schemes"] if i["name"] == "cdgm")["error"])
        self.assertEqual(manifest["comparability"]["first_order"], [["schott", "hoya"]])

    def test_a_scheme_that_leaves_processes_in_doubt_stops_the_set(self):
        self.cleanup_doubt = {"cdgm"}
        _, manifest = self.execute({"name": "schott"}, {"name": "cdgm", "edits": GLASS}, {"name": "hoya", "edits": GLASS})
        states = {item["name"]: item["status"] for item in manifest["schemes"]}
        self.assertEqual(states, {"schott": "succeeded", "cdgm": "failed", "hoya": "skipped"})
        self.assertIn("in doubt", manifest["stop_reason"])
        self.assertEqual([call[0] for call in self.aut_calls], ["schott", "cdgm"])

    def test_a_failed_typed_edit_fails_only_that_scheme(self):
        original = self.edit_runner

        def refusing(source, edits_path, output, **kwargs):
            if "cdgm" in edits_path.read_text(encoding="utf-8"):
                return kwargs["output_dir"], {"status": "failed", "error": "glass not found"}
            return original(source, edits_path, output, **kwargs)

        _, manifest = run_schemes(
            self.baseline, self.scheme_path({"name": "cdgm", "edits": GLASS}, {"name": "hoya", "edits": GLASS}),
            self.aut_path, self.root / "out2", edit_runner=refusing, aut_runner=self.aut_runner)
        states = {item["name"]: item["status"] for item in manifest["schemes"]}
        self.assertEqual(states, {"cdgm": "failed", "hoya": "succeeded"})
        self.assertIn("glass not found", manifest["schemes"][0]["error"])
        self.assertEqual([call[0] for call in self.aut_calls], ["hoya"])

    def test_field_set_schemes_send_a_field_set_file_and_own_aut_specs_are_used(self):
        other = dict(AUT_SPEC, name="other-constraints")
        (self.root / "other.json").write_text(json.dumps(other), encoding="utf-8")
        _, manifest = self.execute(
            {"name": "vig", "field_set": {"fields": [{"y_angle": 0}, {"y_angle": 10, "vuy": 0.1}]}},
            {"name": "own", "aut_spec": "other.json"})
        self.assertIn("field_set", self.edit_calls[0][0])
        self.assertNotIn("edits", self.edit_calls[0][0])
        self.assertEqual([(call[0], call[2]) for call in self.aut_calls],
                         [("vig", "tiny"), ("own", "other-constraints")])

    def test_the_design_spec_judgement_is_run_per_candidate_and_its_failure_is_recorded(self):
        spec = self.root / "spec.json"
        spec.write_text("{}", encoding="utf-8")
        _, manifest = self.execute({"name": "a"}, {"name": "b"}, design_spec=spec)
        self.assertEqual(len(self.eval_calls), 2)
        self.assertEqual(self.eval_calls[0][1]["spec_path"], spec.resolve())
        self.assertEqual(manifest["schemes"][0]["evaluation"]["status"], "pass")

        def broken(initial, final, output_dir, **kwargs):
            raise RuntimeError("session cleanup unconfirmed")

        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path,
                                  self.root / "out3", design_spec=spec, edit_runner=self.edit_runner,
                                  aut_runner=self.aut_runner, evaluator=broken)
        self.assertEqual(manifest["schemes"][0]["status"], "succeeded")
        self.assertIn("unconfirmed", manifest["schemes"][0]["evaluation"]["error"])

    def test_evaluation_reuse_limit_is_forwarded_and_cleanup_doubt_stops_new_schemes(self):
        spec = self.root / "spec.json"
        spec.write_text("{}", encoding="utf-8")

        def doubtful(*args, **kwargs):
            self.assertEqual(kwargs["max_analyses_per_session"], 8)
            return args[2], {"status": "failed", "performance": {"sessions": [
                {"cleanup_confirmed": False}]}}

        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}),
                                  self.aut_path, self.root / "out", design_spec=spec,
                                  aut_runner=self.aut_runner, evaluator=doubtful,
                                  max_analyses_per_session=8)
        self.assertEqual(manifest["max_analyses_per_session"], 8)
        self.assertTrue(manifest["schemes"][0]["cleanup_in_doubt"])
        self.assertEqual(manifest["schemes"][1]["status"], "skipped")
        self.assertEqual([call[0] for call in self.aut_calls], ["a"])

    def test_invalid_evaluation_reuse_limit_runs_nothing(self):
        for limit in (0, 9, True, 2.5):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "max_analyses"):
                self.execute({"name": "a"}, max_analyses_per_session=limit)
        self.assertEqual(self.aut_calls, [])
        self.assertFalse((self.root / "out").exists())

    def test_up_front_refusals_run_nothing(self):
        with self.assertRaisesRegex(ValueError, "no AUT spec"):
            run_schemes(self.baseline, self.scheme_path({"name": "a"}), None, self.root / "o1",
                        edit_runner=self.edit_runner, aut_runner=self.aut_runner)
        with self.assertRaisesRegex(ValueError, "not found"):
            run_schemes(self.baseline, self.scheme_path({"name": "a", "aut_spec": "missing.json"}), self.aut_path,
                        self.root / "o2", edit_runner=self.edit_runner, aut_runner=self.aut_runner)
        with self.assertRaisesRegex(ValueError, "baseline"):
            run_schemes(self.root / "nothing.len", self.scheme_path({"name": "a"}), self.aut_path, self.root / "o3")
        (self.root / "exists").mkdir()
        with self.assertRaisesRegex(ValueError, "new and use an ASCII"):
            run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path, self.root / "exists")
        with self.assertRaisesRegex(ValueError, "new and use an ASCII"):
            run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path, self.root / "中文")
        self.assertEqual(self.aut_calls, [])

    def test_a_baseline_that_changed_fails_the_run(self):
        original = self.aut_runner

        def touching(source, root, spec, *, spec_sha256):
            self.baseline.write_bytes(b"changed")
            return original(source, root, spec, spec_sha256=spec_sha256)

        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path, self.root / "o4",
                                  edit_runner=self.edit_runner, aut_runner=touching)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("baseline lens changed", manifest["error"])

    def test_an_interrupt_stops_and_keeps_what_ran(self):
        def interrupting(source, root, spec, *, spec_sha256):
            if root.parent.name == "b":
                raise KeyboardInterrupt
            return self.aut_runner(source, root, spec, spec_sha256=spec_sha256)

        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}, {"name": "c"}),
                                  self.aut_path, self.root / "o5", edit_runner=self.edit_runner,
                                  aut_runner=interrupting)
        self.assertEqual(manifest["stop_reason"], "interrupted")
        self.assertEqual([item["status"] for item in manifest["schemes"]], ["succeeded", "interrupted"])
        self.assertEqual(manifest["status"], "partial")

    # ------------------------------------------------------------ parallel scheduling (F7)

    def test_diagnostic_deadline_with_confirmed_cleanup_continues_but_remaining_processes_stop(self):
        for remaining in ([], [4242]):
            report = report_for()
            report.update(state="failed", error="after_wavefront cleanup or deadline failure: timed out",
                          after_wavefront={"state": "failed", "stage_error": True,
                                           "cleanup_remaining": remaining})
            self.reports = {"a": report}
            _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}),
                                      self.aut_path, self.root / f"cleanup-{len(remaining)}",
                                      aut_runner=self.aut_runner)
            self.assertEqual(manifest["schemes"][1]["status"], "skipped" if remaining else "succeeded")
            self.assertEqual(bool(manifest.get("stop_reason")), bool(remaining))

    def test_cleanup_exceptions_ignore_prose_and_keep_structured_outcome(self):
        for remaining in ([], [4242]):
            def failed(*args, **kwargs):
                raise CleanupError("arbitrary diagnostic wording", remaining=remaining)
            _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}),
                                      self.aut_path, self.root / f"exception-{len(remaining)}", aut_runner=failed)
            self.assertEqual(manifest["schemes"][1]["status"], "skipped" if remaining else "failed")
        self.assertFalse(cleanup_in_doubt({"error": "cleanup or deadline failure; cleanup remaining: [4242]"}))
        self.assertFalse(cleanup_in_doubt({"cleanup_error": "earlier close failed", "cleanup_remaining": []}))
        self.assertTrue(cleanup_in_doubt({"cleanup_remaining": [], "after_wavefront": {
            "cleanup_confirmed": False}}))  # a different session is still uncertain

    def test_typed_edit_primary_error_preserves_cleanup_uncertainty(self):
        def leaking(*args, **kwargs):
            return kwargs["output_dir"], {"status": "failed", "error": "glass refused", "cleanup_confirmed": False}
        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a", "edits": GLASS}, {"name": "b"}),
                                  self.aut_path, self.root / "out", edit_runner=leaking, aut_runner=self.aut_runner)
        self.assertEqual([s["status"] for s in manifest["schemes"]], ["failed", "skipped"])

    def test_screening_runs_only_finalists_in_full_and_restarts_the_same_typed_baseline(self):
        calls = []
        def runner(source, root, spec, *, spec_sha256):
            calls.append((root.parent.name, root.parent.parent.name, source.read_bytes(), copy.deepcopy(spec), spec_sha256))
            return self.aut_runner(source, root, spec, spec_sha256=spec_sha256)
        self.reports = {"a": report_for(error=5), "b": report_for(error=1, satisfied=False),
                        "c": report_for(error=3)}
        design = self.root / "design.json"
        design.write_text("{}", encoding="utf-8")
        path, result = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b", "edits": GLASS},
                                                                {"name": "c"}), self.aut_path, self.root / "out",
                                  aut_runner=runner, edit_runner=self.edit_runner, evaluator=self.evaluator,
                                  screen_cycles=1, finalists=1, design_spec=design)
        self.assertEqual([(c[0], c[1]) for c in calls], [("a", "screen"), ("b", "screen"),
                                                       ("c", "screen"), ("b", "full")])
        self.assertEqual(calls[1][2], calls[-1][2])  # typed edits rerun, never optimize coarse candidate bytes
        self.assertTrue(all(c[3]["stages"][0]["error_function"]["MXC"] == 1 for c in calls[:3]))
        self.assertEqual(calls[-1][3], AUT_SPEC)
        self.assertIsNone(calls[0][4])  # derived spec cannot claim the original file hash
        self.assertIsNotNone(calls[-1][4])
        self.assertEqual(result["screening"]["selected"], ["b"])
        self.assertEqual([s["status"] for s in result["schemes"]], ["screened_out", "succeeded", "screened_out"])
        self.assertEqual(len(self.edit_calls), 2)
        self.assertEqual(len(self.eval_calls), 1)
        self.assertIn("粗筛与入围", (path / "summary.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((path / "execution-record.json").read_text(encoding="utf-8"))["steps"][0]
                         ["parameters"]["screen_cycles"], 1)

    def test_screening_keeps_a_finalist_from_each_incomparable_group_and_stable_ties(self):
        self.reports = {"a": report_for(error=5), "b": report_for(error=5),
                        "c": report_for(error=0.01, vuy=0.1)}
        _, result = self.execute({"name": "a"}, {"name": "b"}, {"name": "c"}, screen_cycles=1, finalists=1)
        self.assertEqual(result["screening"]["selected"], ["a", "c"])
        self.assertEqual([c[0] for c in self.aut_calls], ["a", "b", "c", "a", "c"])
        self.assertIsNone(result["recommendation"]["scheme"])

    def test_screening_excludes_failure_nonfinite_error_and_variable_bound_violation(self):
        items = [entry("failed"), entry("nan", error=float("nan")), entry("inf", error=float("inf")),
                 entry("bounds", bounds=False), entry("doubt"), entry("ok", satisfied=False)]
        items[0]["status"] = "failed"
        items[4]["cleanup_in_doubt"] = True
        self.assertEqual(schemes.shortlist(items, 2), ["ok"])
        self.fail_aut = {"a", "b"}
        _, result = self.execute({"name": "a"}, {"name": "b"}, screen_cycles=1, finalists=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.aut_calls), 2)
        self.assertIsNone(result["recommendation"]["scheme"])

    def test_screening_doubt_never_starts_full_optimization(self):
        self.cleanup_doubt = {"a"}
        _, result = self.execute({"name": "a"}, {"name": "b"}, screen_cycles=1, finalists=1)
        self.assertEqual(result["screening"]["selected"], [])
        self.assertEqual(len(self.aut_calls), 1)
        self.assertIn("in doubt", result["stop_reason"])

    def test_screening_interrupt_preserves_the_partial_screening_record(self):
        def interrupted(*args, **kwargs):
            raise CleanupError("stage interrupted", remaining=[], interrupted=True)
        path, result = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}),
                                  self.aut_path, self.root / "out", screen_cycles=1, finalists=1, aut_runner=interrupted)
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual(result["screening"]["schemes"][0]["status"], "interrupted")
        self.assertEqual(result["screening"]["selected"], [])
        self.assertTrue((path / "summary.md").is_file())
        self.assertIsNone(result["recommendation"]["scheme"])

    def test_screening_detects_baseline_changes_before_full_optimization(self):
        def touching(source, root, spec, **kwargs):
            self.baseline.write_bytes(b"changed")
            return self.aut_runner(source, root, spec, **kwargs)
        _, result = run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path, self.root / "out",
                                screen_cycles=1, finalists=1, aut_runner=touching)
        self.assertEqual(len(self.aut_calls), 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["screening"]["selected"], [])
        self.assertIsNone(result["recommendation"]["scheme"])

    def test_screening_arguments_are_validated_before_creating_output(self):
        for kwargs in ({"screen_cycles": 1}, {"finalists": 1}, {"screen_cycles": 0, "finalists": 1},
                       {"screen_cycles": True, "finalists": 1}, {"screen_cycles": 501, "finalists": 1},
                       {"screen_cycles": 1, "finalists": 0}, {"screen_cycles": 1, "finalists": 9}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.execute({"name": "a"}, **kwargs)
        self.assertFalse((self.root / "out").exists())

    def test_parallel_screening_uses_separate_round_workers_and_full_original_specs(self):
        path, result = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}),
                                  self.aut_path, self.root / "out", jobs=2, launcher=self.launcher,
                                  screen_cycles=1, finalists=1)
        self.assertEqual(result["screening"]["selected"], ["a"])
        screen = json.loads((path / "screen/_workers/a.json").read_text(encoding="utf-8"))
        full = json.loads((path / "full/_workers/a.json").read_text(encoding="utf-8"))
        self.assertEqual(screen["aut_spec"]["stages"][0]["error_function"]["MXC"], 1)
        self.assertEqual(full["aut_spec"], AUT_SPEC)
        self.assertFalse((path / "full/_workers/b.json").exists())
        self.assertEqual(result["status"], "succeeded")

    def test_interrupted_parallel_screening_worker_prevents_the_full_round(self):
        path, result = run_schemes(self.baseline, self.scheme_path({"name": "a", "reason": "sleep:0.1,interrupt"},
                                                                {"name": "b", "reason": "sleep:0.4"}),
                                  self.aut_path, self.root / "out", jobs=2, launcher=self.launcher,
                                  screen_cycles=1, finalists=1)
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual(result["screening"]["selected"], [])
        self.assertFalse((path / "full").exists())

    def test_screening_caps_the_expanded_ramp_without_dropping_typed_changes_or_mutating_original(self):
        spec = {"schema_version": 1, "kind": "aut_spec", "name": "ramp", "wall_seconds": 60,
                "field_ramp": {"name": "grow", "steps": [{"fields": [{"field": 2, "y_angle": 3}]},
                                                         {"fields": [{"field": 2, "y_angle": 6}]}],
                               "stage": {"variables": [{"surface": 1, "parameter": "radius"}],
                                         "constraints": [{"operand": "EFL", "relation": ">", "value": 10}],
                                         "error_function": {"MXC": 5, "MNC": 3}}}}
        original = copy.deepcopy(spec)
        (self.root / "ramp.json").write_text(json.dumps(spec), encoding="utf-8")
        calls = []
        def runner(source, root, spec, **kwargs):
            calls.append(copy.deepcopy(spec))
            return self.aut_runner(source, root, spec, **kwargs)
        run_schemes(self.baseline, self.scheme_path({"name": "a", "aut_spec": "ramp.json"}),
                    self.aut_path, self.root / "out", screen_cycles=1, finalists=1, aut_runner=runner)
        self.assertEqual([s["lens_changes"][0]["value"] for s in calls[0]["stages"]], [3, 6])
        self.assertEqual([s["error_function"] for s in calls[0]["stages"]], [{"MXC": 1, "MNC": 1}] * 2)
        self.assertTrue(all(s["constraints"] == spec["field_ramp"]["stage"]["constraints"] for s in calls[0]["stages"]))
        self.assertEqual(calls[1], original)

    def test_unknown_worker_result_is_a_cleanup_failure(self):
        result = self.root / "bad-entry.json"
        for payload in ([], {}, {"name": "other", "status": "succeeded"}):
            result.write_text(json.dumps(payload), encoding="utf-8")
            item = schemes._collect("a", self.root, result, 0, time.monotonic())
            self.assertEqual(item["status"], "failed")
            self.assertTrue(item["cleanup_in_doubt"])

    def launcher(self, request):
        return [sys.executable, str(Path(__file__).with_name("fake_scheme_worker.py")), str(request)]

    def parallel(self, jobs, *items, name="par"):
        return run_schemes(self.baseline, self.scheme_path(*items), self.aut_path, self.root / name,
                           jobs=jobs, launcher=self.launcher)

    @staticmethod
    def overlap(directory) -> int:
        """The most workers that were alive at the same time, from the workers' own start/end lines."""
        events = []
        for path in (Path(directory) / "_workers").glob("*-events.txt"):
            kinds = []
            for line in path.read_text(encoding="utf-8").splitlines():
                stamp, kind, _ = line.split()
                kinds.append(kind)
                events.append((int(stamp), 1 if kind == "start" else -1))
            if kinds != ["start", "end"]:
                raise AssertionError(f"Incomplete worker events: {path}: {kinds}")
        alive = peak = 0
        for _, step in sorted(events):
            alive += step
            peak = max(peak, alive)
        return peak

    def test_jobs_run_that_many_schemes_at_once_and_keep_the_scheme_order(self):
        items = [{"name": n, "reason": f"sleep:2.0,barrier:{'a+b' if n in 'ab' else 'c+d'}"}
                 for n in "abcd"]
        directory, manifest = self.parallel(2, *items)
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["jobs"], 2)
        self.assertEqual(self.overlap(directory), 2)
        self.assertEqual([item["name"] for item in manifest["schemes"]], list("abcd"))
        self.assertTrue((directory / "summary.md").is_file())
        record = json.loads((directory / "execution-record.json").read_text(encoding="utf-8"))["steps"][0]
        self.assertEqual(record["parameters"]["jobs"], 2)
        self.assertIn("2 at a time", record["command_note"])

    def test_one_job_stays_in_process_and_never_starts_a_worker(self):
        directory, manifest = self.execute({"name": "a"}, {"name": "b"})
        self.assertEqual(manifest["jobs"], 1)
        self.assertFalse((directory / "_workers").exists())

    def test_parallel_requests_and_worker_forward_the_evaluation_reuse_limit(self):
        from codev_mcp.schemes import _scheme_worker
        import os
        spec = self.root / "design-spec.json"
        spec.write_text("{}", encoding="utf-8")
        previous_directory = Path.cwd()
        try:
            # Windows test temp files may be on C: while the checkout is on D:.
            # Use the real invoking directory to test a genuinely relative path.
            os.chdir(self.root)
            directory, manifest = run_schemes(
                self.baseline, self.scheme_path({"name": "a", "reason": "sleep:0.1"}),
                self.aut_path, self.root / "par-reuse", jobs=2,
                launcher=self.launcher, max_analyses_per_session=8,
                design_spec=Path("design-spec.json"))
        finally:
            os.chdir(previous_directory)
        request = directory / "_workers/a.json"
        payload = json.loads(request.read_text(encoding="utf-8"))
        self.assertEqual(payload["max_analyses_per_session"], 8)
        self.assertEqual(Path(payload["design_spec"]), spec.resolve())
        self.assertEqual(manifest["max_analyses_per_session"], 8)
        with mock.patch("codev_mcp.schemes.run_scheme") as runner:
            self.assertEqual(_scheme_worker(request), 0)
        self.assertEqual(runner.call_args.kwargs["max_analyses_per_session"], 8)
        self.assertEqual(runner.call_args.kwargs["design_spec"], spec.resolve())

    def test_a_failing_scheme_does_not_touch_the_ones_running_beside_it(self):
        _, manifest = self.parallel(2, {"name": "a", "reason": "sleep:0.8"}, {"name": "b", "reason": "sleep:0.1,fail"},
                                    {"name": "c", "reason": "sleep:0.1"})
        states = {item["name"]: item["status"] for item in manifest["schemes"]}
        self.assertEqual(states, {"a": "succeeded", "b": "failed", "c": "succeeded"})
        self.assertEqual(manifest["status"], "partial")

    def test_processes_in_doubt_stop_new_launches_but_let_the_running_ones_finish(self):
        directory, manifest = self.parallel(2, {"name": "a", "reason": "sleep:2.5,barrier:a+b"},
                                            {"name": "b", "reason": "sleep:0.1,barrier:a+b,doubt"},
                                            {"name": "c"}, {"name": "d"})
        states = {item["name"]: item["status"] for item in manifest["schemes"]}
        self.assertEqual(states, {"a": "succeeded", "b": "failed", "c": "skipped", "d": "skipped"})
        self.assertIn("in doubt", manifest["stop_reason"])
        self.assertEqual(self.overlap(directory), 2)

    def test_a_worker_that_leaves_no_result_is_a_failure_in_doubt(self):
        _, manifest = self.parallel(2, {"name": "a", "reason": "sleep:0.1,crash"}, {"name": "b", "reason": "sleep:3.0"},
                                    {"name": "c"})
        by_name = {item["name"]: item for item in manifest["schemes"]}
        self.assertEqual(by_name["a"]["status"], "failed")
        self.assertTrue(by_name["a"]["cleanup_in_doubt"])
        self.assertIn("exited 3", by_name["a"]["error"])
        self.assertEqual(by_name["b"]["status"], "succeeded")  # already running, allowed to finish
        self.assertEqual(by_name["c"]["status"], "skipped")

    def test_an_interrupt_lets_finished_workers_be_kept_and_stops_a_stuck_one(self):
        real_sleep = time.sleep
        calls = []
        quick_entry = self.root / "par" / "_workers" / "quick-entry.json"

        def sleep(seconds):
            calls.append(seconds)
            if len(calls) == 1:
                deadline = time.monotonic() + 60
                while not quick_entry.is_file() and time.monotonic() < deadline:
                    real_sleep(0.1)  # the "Ctrl-C" comes once the quick worker has finished
                raise KeyboardInterrupt
            real_sleep(seconds)

        with (mock.patch.object(schemes.time, "sleep", sleep), mock.patch.object(schemes, "INTERRUPT_GRACE_SECONDS", 2)):
            directory, manifest = self.parallel(2, {"name": "quick", "reason": "sleep:0.2"},
                                                {"name": "stuck", "reason": "sleep:0.1,hang"}, {"name": "never"})
        by_name = {item["name"]: item for item in manifest["schemes"]}
        self.assertEqual(manifest["stop_reason"], "interrupted")
        self.assertEqual(by_name["quick"]["status"], "succeeded")
        self.assertEqual(by_name["stuck"]["status"], "interrupted")
        self.assertTrue(by_name["stuck"]["cleanup_in_doubt"])
        self.assertNotIn("never", by_name)
        self.assertEqual(manifest["status"], "partial")

    def test_the_job_count_is_validated_and_replacement_runners_need_one_job(self):
        for jobs in (0, 9, "2", 2.0, True):
            with self.subTest(jobs=jobs), self.assertRaisesRegex(ValueError, "jobs must be"):
                run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path, self.root / f"j{jobs}",
                            jobs=jobs)
        with self.assertRaisesRegex(ValueError, "in-process"):
            run_schemes(self.baseline, self.scheme_path({"name": "a"}), self.aut_path, self.root / "jr",
                        jobs=2, aut_runner=self.aut_runner)
        self.assertFalse((self.root / "jr").exists())

    def test_the_worker_entry_point_runs_one_scheme_and_writes_its_entry(self):
        request = self.root / "request.json"
        result = self.root / "entry.json"
        request.write_text(json.dumps({
            "baseline": str(self.baseline), "scheme": {"name": "solo"}, "aut_spec": AUT_SPEC, "aut_spec_sha": "s",
            "directory": str(self.root / "solo"), "design_spec": None, "backend": "com", "timeout": 1.0,
            "result_path": str(result)}), encoding="utf-8")

        def fake_run(baseline, scheme, spec, sha, directory, **kwargs):
            kwargs["sink"].append({"name": scheme["name"], "status": "succeeded", "directory": str(directory)})
            return kwargs["sink"][0]

        with mock.patch.object(schemes, "run_scheme", fake_run):
            self.assertEqual(schemes.main(["_scheme", str(request)]), 0)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["name"], "solo")

        def interrupted(baseline, scheme, spec, sha, directory, **kwargs):
            kwargs["sink"].append({"name": scheme["name"], "status": "interrupted"})
            raise KeyboardInterrupt

        result.unlink()
        with mock.patch.object(schemes, "run_scheme", interrupted):
            self.assertEqual(schemes.main(["_scheme", str(request)]), 2)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["status"], "interrupted")

    def test_an_edit_or_evaluation_cut_short_by_ctrl_c_stops_the_serial_loop(self):
        def interrupted_edit(source, edits_path, output, **kwargs):
            return kwargs["output_dir"], {"status": "failed", "error": "KeyboardInterrupt: ", "interrupted": True}

        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a", "edits": GLASS}, {"name": "b"}),
                                  self.aut_path, self.root / "i1", edit_runner=interrupted_edit,
                                  aut_runner=self.aut_runner)
        self.assertEqual(manifest["stop_reason"], "interrupted")
        self.assertEqual([(i["name"], i["status"]) for i in manifest["schemes"]], [("a", "interrupted")])
        self.assertEqual(self.aut_calls, [])

        spec = self.root / "spec.json"
        spec.write_text("{}", encoding="utf-8")

        def interrupted_evaluation(initial, final, output_dir, **kwargs):
            return output_dir, {"status": "failed", "interrupted": True}

        _, manifest = run_schemes(self.baseline, self.scheme_path({"name": "a"}, {"name": "b"}), self.aut_path,
                                  self.root / "i2", design_spec=spec, edit_runner=self.edit_runner,
                                  aut_runner=self.aut_runner, evaluator=interrupted_evaluation)
        self.assertEqual([(i["name"], i["status"]) for i in manifest["schemes"]], [("a", "interrupted")])

    def test_the_rendering_names_unreproducible_comparisons_and_the_accept_step(self):
        self.reports = {"wide": report_for(vuy=0.1, rms=0.01)}
        directory, manifest = self.execute({"name": "narrow"}, {"name": "narrow2"}, {"name": "wide", "edits": GLASS})
        text = render_summary(manifest)
        self.assertIn("不可直接比较", text)
        self.assertIn("python -m codev_mcp.aut accept", text)
        self.assertIn("narrow", text.split("## 推荐")[1])


if __name__ == "__main__":
    unittest.main()
