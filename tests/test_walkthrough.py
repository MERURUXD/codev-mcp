"""D9 walkthrough: snapshot of a synthetic workflow, missing data, failures and refusals."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from codev_mcp.record import execution_step, write_record
from codev_mcp.walkthrough import load_runs, render, write_walkthrough

SNAPSHOT = Path(__file__).resolve().parent / "data" / "walkthrough-snapshot.md"
SPEC = Path(__file__).resolve().parents[1] / "docs" / "design" / "design-spec-dbgauss.json"


def step(root: Path, name: str, **kwargs) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    defaults = {"status": "succeeded", "source": "codev", "inputs": [], "parameters": {}, "native_commands": [],
                "command_note": "", "results": {}, "outputs": [], "bundle": str(directory)}
    record = execution_step(**{**defaults, **kwargs})
    record["recorded_at"] = "2026-09-28T00:00:00+00:00"
    write_record(directory / "execution-record.json", [record])
    return directory


def workflow(root: Path) -> list[Path]:
    runs = [
        step(root, "scale", action="scale", tool="codev_mcp.scale",
             inputs=[{"role": "lens", "path": "D:/lenses/start.len", "sha256": "a" * 64}],
             parameters={"target_efl": 50.0, "factor": 0.5},
             native_commands=["RDY S1 28.7", "EPD 25"], command_note="one update_lens transaction",
             results={"efl_check": {"after": 50.0000000001, "relative_deviation": 2e-12},
                      "first_order": {"effective_focal_length": {"before": 100.0, "after": 50.0}},
                      "solve_coupled": [{"surface": 11, "control": "PIM", "before": 63.1, "after": 31.6}]},
             outputs=[{"path": "D:/lenses/scaled.len", "sha256": "b" * 64}]),
        step(root, "glass", action="glass_candidates", tool="codev_mcp.glass_near",
             inputs=[{"role": "glass_catalog", "path": "glass.cat", "sha256": "c" * 64}],
             parameters={"reference": {"glass": "REF_SCHOTT", "nd": 1.6, "vd": 38.0}, "catalogs": ["CDGM"]},
             native_commands=["GLD;GLI CDGM"],
             results={"candidates": {"CDGM": [{"name": "CAND", "code": "603380", "nd": 1.60342, "vd": 38.0,
                                               "delta_nd": -0.002, "delta_vd": 0.1, "distance": 0.2}]},
                      "definitions": {"distance": "sqrt((dnd/0.01)^2 + (dvd/1)^2)"}}),
        step(root, "edit", action="edit", tool="codev_mcp.edit",
             inputs=[{"role": "lens", "path": "D:/lenses/scaled.len", "sha256": "b" * 64}],
             parameters={"name": "swap", "reason": "nearest", "edits": [
                 {"target": "surface", "surface": 4, "parameter": "glass", "value": "CAND_CDGM"}]},
             native_commands=["GLA S4 CAND_CDGM"],
             results={"transaction": {"warnings": ["surface 11 thickness re-derived"]}},
             outputs=[{"path": "D:/设计/换玻璃.len", "sha256": "d" * 64}]),
        step(root, "aut", action="aut", tool="codev_mcp.aut prepare", status="failed",
             inputs=[{"role": "lens", "path": "D:/设计/换玻璃.len", "sha256": "d" * 64}],
             native_commands=["! stage 1 (mono): succeeded", "frz s0..i", "ccy s1 0", "go",
                              "! stage 2 (poly): failed", "frz s0..i"],
             results={"stages": [
                 {"index": 1, "name": "mono", "status": "succeeded", "completion": "Maximum cycle limit reached",
                  "initial_error": 10.0, "final_error": 5.0, "final_cycle": 3, "constraints_satisfied": False,
                  "lens_changes": [{"target": "wavelength", "wavelength": 1, "parameter": "weight", "value": 0}],
                  "variables": [{"surface": 1, "parameter": "radius", "before": 28.7, "after": 28.1,
                                 "within_bounds": True}],
                  "constraints": [{"label": "EFL", "relation": "=", "target": 50, "status": "violated",
                                   "printed": {"value": "4.99990E+01", "diff": "-1.000E-04"}}],
                  "general_constraints": {"service_checks": [{"id": "MNE", "metric": "edge_thickness_min",
                                                              "value": 0.4, "unit": "mm", "status": "fail"}],
                                          "frozen_violations": ["Mn ET S10"]},
                  "solve_coupled": [{"surface": 11, "parameter": "thickness", "value": "PIM", "before": 31.6, "after": 30.9}]},
                 {"index": 2, "name": "poly", "status": "failed", "error": "ValueError: no completion line"}],
                 "error": "stage 2 (poly) failed"},
             error="ValueError: stage 2 (poly) failed"),
    ]
    bundle = root / "evaluate"
    bundle.mkdir()
    (bundle / "lens" / "first_order").mkdir(parents=True)
    (bundle / "lens" / "first_order" / "snapshot.json").write_text(json.dumps({"first_order": {
        "effective_focal_length": 50.0, "f_number": 2.0, "overall_length": 37.5, "precision_note": "string items"}}),
        encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps({"inputs": {"lens": {"path": "D:/lenses/other.len", "sha256": "e" * 64}},
        "analyses": [{"stage": "lens", "name": "first_order", "status": "succeeded", "request": {"kind": "first_order"},
                      "snapshot": "lens/first_order/snapshot.json"}]}), encoding="utf-8")
    (bundle / "evaluation.json").write_text(json.dumps({"stages": {"lens": {"status": "unknown",
        "conditions": [], "criteria": [], "requirements": [
            {"id": "efl", "required": True, "status": "pass", "value": 50.0, "unit": "mm"},
            {"id": "mtf-edge-50", "required": True, "status": "unknown", "value": None, "reason": "threshold pending (待补)"}]}}}),
        encoding="utf-8")
    record = execution_step(action="evaluate", tool="codev_mcp.compare", status="succeeded", source="codev",
                            inputs=[{"role": "lens", "path": "D:/lenses/other.len", "sha256": "e" * 64}],
                            parameters={}, native_commands=["spo;go"], command_note="native plots",
                            results={"evaluation": {"status": {"lens": "unknown"}}, "report": "report.md",
                                     "figures": [{"stage": "lens", "name": "native-spot", "origin": "codev_native",
                                                  "image": "lens/native-spot/image.png", "plot_file": "lens/native-spot/native.PLT"}]},
                            outputs=[], bundle=str(bundle))
    record["recorded_at"] = "2026-09-28T00:00:00+00:00"
    write_record(bundle / "execution-record.json", [record])
    return runs + [bundle]


class WalkthroughTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def normalised(self, text: str) -> str:
        return text.replace(str(self.root), "<ROOT>").replace(os.sep, "/")

    def test_snapshot_of_a_failed_unaccepted_workflow(self):
        from codev_mcp.spec import read_spec
        spec, _ = read_spec(SPEC)
        text = render(load_runs(workflow(self.root)), spec, title="演示", generated_at="2026-09-28T00:00:00+00:00")
        text = self.normalised(text)
        if os.environ.get("UPDATE_WALKTHROUGH_SNAPSHOT"):
            SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")
        self.assertEqual(text, SNAPSHOT.read_text(encoding="utf-8"))
        for expected in ("未满足", "步骤 4（受控 AUT 候选）状态为 失败", "哈希不连续", "待补", "Mn ET S10",
                         "RES D:/lenses/start.len", "SAV D:/设计/换玻璃.len", "GLA S4 CAND_CDGM", "不含像距"):
            self.assertIn(expected, text)

    def test_missing_pieces_are_written_as_missing(self):
        runs = workflow(self.root)
        (runs[-1] / "evaluation.json").unlink()
        text = render(load_runs([runs[0], runs[-1]]), None, title="t", generated_at="now", spec_error="absent")
        self.assertIn("设计规格：缺失（absent）", text)
        self.assertIn("规格判定：缺失", text)

    def test_failed_analyses_of_an_evaluation_are_listed(self):
        directory = step(self.root, "eval", action="evaluate", tool="codev_mcp.compare", status="failed",
                         results={"failed_analyses": [{"stage": "lens", "name": "spot-2", "kind": "computation_failed",
                                                       "error": "computation_failed: No ray reached the image surface."}]},
                         error="1 analyses failed and the rest ran: lens/spot-2 (computation_failed)")
        text = render(load_runs([directory]), None, title="t", generated_at="now")
        self.assertIn("lens/spot-2 没有得到结果（computation_failed", text)
        self.assertIn("规格判定中对应的量记为未知", text)

    def test_simulated_source_and_unaccepted_candidate_are_flagged(self):
        directory = step(self.root, "sim-aut", action="aut", tool="codev_mcp.aut prepare", status="complete",
                         source="simulated", inputs=[{"role": "lens", "path": "x.len", "sha256": "f" * 64}],
                         results={"stages": []}, outputs=[{"role": "candidate", "path": "c.len", "sha256": "0" * 64}])
        text = render(load_runs([directory]), None, title="t", generated_at="now")
        self.assertIn("模拟来源", text)
        self.assertIn("未被接受", text)

    def test_output_rules(self):
        runs = workflow(self.root)
        target = self.root / "设计记录.md"
        write_walkthrough(runs, target, spec_path=SPEC, generated_at="now")
        self.assertIn("# 设计记录", target.read_text(encoding="utf-8"))
        for bad in (target, self.root / "x.txt", self.root / "missing" / "a.md"):
            with self.subTest(bad=bad.name), self.assertRaises(ValueError):
                write_walkthrough(runs, bad)
        with self.assertRaisesRegex(ValueError, "execution-record"):
            load_runs([self.root])


if __name__ == "__main__":
    unittest.main()
