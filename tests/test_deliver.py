"""Deliverable arrangement (E7): paired figures, native text, equivalent macro, index and refusals."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from codev_mcp import deliver
from codev_mcp.deliver import deliver as make_delivery
from codev_mcp.deliver import equivalent_stage, macro_texts, split_stages

from tests.publication_fixtures import aut_record
RECORD = aut_record()
COMMANDS = RECORD["steps"][0]["native_commands"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Bundle:
    """A minimal comparison bundle with the files the manifest points at."""

    def __init__(self, root: Path, *, stages=("initial", "final"), fail: dict | None = None):
        self.root = root
        self.stages = stages
        self.fail = fail or {}
        root.mkdir(parents=True)
        analyses, failed, inputs = [], [], {}
        for stage in stages:
            for name, kind, native in (("first_order", "first_order", False), ("spot-1", "spot_diagram", False),
                                       ("wavefront", "wavefront", False), ("native-layout", "native_plot", True)):
                if (stage, name) in self.fail:
                    analyses.append({"stage": stage, "name": name, "status": "failed",
                                     "request": {"kind": kind, "options": {}}, "error": self.fail[(stage, name)]})
                    failed.append({"stage": stage, "name": name, "kind": kind, "error": self.fail[(stage, name)]})
                    continue
                artifacts = []
                for filename in (["image.png", "native.PLT"] if native else ["image.png"] if kind == "spot_diagram" else []) \
                        + ["raw-output.txt", "cleanup.json"]:
                    path = root / stage / name / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f"{stage}/{name}/{filename}".encode())
                    artifacts.append({"path": f"{stage}/{name}/{filename}", "sha256": sha(path), "bytes": path.stat().st_size})
                analyses.append({"stage": stage, "name": name, "status": "succeeded",
                                 "request": {"kind": kind, "options": {}}, "artifacts": artifacts})
            lis = root / stage / "preflight" / "lis.txt"
            lis.parent.mkdir(parents=True, exist_ok=True)
            lis.write_text(f"LIS of {stage}", encoding="utf-8")
            inputs[stage] = {"listing": {"path": f"{stage}/preflight/lis.txt"}}
        manifest = {"mode": "pair" if len(stages) == 2 else "single", "analyses": analyses, "inputs": inputs,
                    "failed_analyses": failed}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "evaluation.json").write_text("{}", encoding="utf-8")
        (root / "comparison.md").write_text("# report", encoding="utf-8")


def aut_run(root: Path, commands=None, *, results=None) -> Path:
    root.mkdir(parents=True)
    step = {"action": "aut", "native_commands": commands if commands is not None else COMMANDS,
            "results": results if results is not None else RECORD["steps"][0]["results"]}
    (root / "execution-record.json").write_text(json.dumps({"steps": [step]}), encoding="utf-8")
    for number in (1, 2):
        (root / f"stage-{number}-aut-output.lis").write_text(f"AUT output {number}", encoding="utf-8")
    return root


class MacroTests(unittest.TestCase):
    def test_the_recorded_ramp_stages_reduce_to_setup_settings_and_go(self):
        stages = split_stages(COMMANDS)
        self.assertEqual([header.split(" ")[1] for header, _ in stages], ["1", "2", "3"])
        reduced = equivalent_stage(stages[0][1])
        self.assertEqual(reduced[:3], ["yan f2 6", "yan f3 9", "wtf f3 2"])
        self.assertEqual(reduced[3], "frz s0..i")
        self.assertEqual(reduced[-1], "go")
        self.assertEqual(reduced.count("go"), 1)
        self.assertIn("efl = 100", reduced)
        for dropped in ("mxc 0", "out t"):
            self.assertNotIn(dropped, reduced)
        self.assertFalse(any(command.startswith(("out ", "sav ", "tim ")) or command == "vli y" for command in reduced))
        self.assertLess(len(reduced), len(stages[0][1]))

    def test_the_settings_keep_their_order(self):
        reduced = equivalent_stage(split_stages(COMMANDS)[1][1])
        block = reduced[reduced.index("aut"):]
        self.assertEqual(block[:4], ["aut", "err cdv", "mxc 15", "mnc 1"])
        self.assertEqual(block[-1], "go")

    def test_set_vig_is_kept_after_go(self):
        block = split_stages(COMMANDS)[0][1] + ["set vig"]
        reduced = equivalent_stage(block)
        self.assertEqual(reduced[-2:], ["go", "set vig"])

    def test_a_block_that_does_not_fit_is_refused_not_guessed(self):
        block = split_stages(COMMANDS)[0][1]
        broken = [command for command in block if command != "mxc 0"]
        for bad in (broken, [c for c in block if not c.startswith("out ")], ["frz s0..i"], [c for c in block if c != "go"]):
            with self.assertRaises(ValueError):
                equivalent_stage(bad)
        with self.assertRaises(ValueError):
            split_stages(["frz s0..i"])

    def test_the_verbatim_text_keeps_everything_and_the_equivalent_lists_problems(self):
        verbatim, equivalent, problems = macro_texts(COMMANDS, source="x")
        self.assertEqual(problems, [])
        self.assertIn("out ", verbatim)
        self.assertFalse([line for line in equivalent.splitlines() if line.startswith(("out ", "sav ", "tim "))])
        self.assertLess(len(equivalent.splitlines()), len(verbatim.splitlines()))
        _, equivalent, problems = macro_texts(["! stage 1 (a): succeeded", "frz s0..i"], source="x")
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be reduced", equivalent)


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_pair_bundle_and_an_aut_run_are_arranged_with_init_and_final_names(self):
        Bundle(self.root / "bundle")
        run = aut_run(self.root / "aut")
        seq = self.root / "final.seq"
        seq.write_text("RDM;LEN\n", encoding="utf-8")
        out = self.root / "deliver"
        result = make_delivery(self.root / "bundle", [run], [seq], out, title="demo")
        names = {item["path"] for item in result["files"]}
        for expected in ("figures/init-spot-1.png", "figures/final-spot-1.png", "figures/init-native-layout.png",
                         "figures/final-native-layout.PLT", "lis/init-lis.txt", "lis/final-lis.txt",
                         "wav/init-wavefront.txt", "wav/final-wavefront.txt", "first-order/init-first_order.txt",
                         "raw/final-spot-1.txt", "macro/optimize-verbatim.seq", "macro/optimize-equivalent.seq",
                         "runs/aut-summary.md", "runs/stage-1-aut-output.lis", "lens/final.seq",
                         "evaluation/evaluation.json", "evaluation/comparison.md", "index.md"):
            self.assertIn(expected, names)
        self.assertEqual((out / "figures" / "init-spot-1.png").read_bytes(), b"initial/spot-1/image.png")
        self.assertEqual((out / "lis" / "final-lis.txt").read_text(encoding="utf-8"), "LIS of final")
        for item in result["files"]:
            if item["source"] != "generated" and not item["source"].endswith("execution-record.json"):
                self.assertEqual(sha(out / item["path"]), sha(Path(item["source"])))
        index = (out / "index.md").read_text(encoding="utf-8")
        self.assertIn("资料索引：demo", index)
        self.assertIn("关于精简宏", index)
        self.assertIn("figures/init-spot-1.png", index)
        summary = (out / "runs" / "aut-summary.md").read_text(encoding="utf-8")
        self.assertIn("grow-1", summary)
        self.assertTrue(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["files"])

    def test_an_initial_lens_that_could_not_be_traced_lists_what_is_missing_and_why(self):
        Bundle(self.root / "bundle", fail={("initial", "spot-1"): "computation_failed: every grid ray was blocked",
                                           ("initial", "native-layout"): "no plot"})
        result = make_delivery(self.root / "bundle", [], [], self.root / "out")
        names = {item["path"] for item in result["files"]}
        self.assertNotIn("figures/init-spot-1.png", names)
        self.assertIn("figures/final-spot-1.png", names)
        self.assertEqual({item["item"] for item in result["unavailable"]}, {"init-spot-1", "init-native-layout"})
        index = (self.root / "out" / "index.md").read_text(encoding="utf-8")
        self.assertIn("未能生成的项", index)
        self.assertIn("every grid ray was blocked", index)
        self.assertTrue(any("初始镜头有 2 项没有生成" in note for note in result["notes"]))

    def test_a_single_lens_bundle_uses_the_final_prefix(self):
        Bundle(self.root / "bundle", stages=("lens",))
        result = make_delivery(self.root / "bundle", [], [], self.root / "out")
        names = {item["path"] for item in result["files"]}
        self.assertIn("figures/final-spot-1.png", names)
        self.assertFalse(any("init-" in name for name in names))

    def test_a_macro_that_cannot_be_reduced_is_a_note_not_a_failure(self):
        run = aut_run(self.root / "aut", ["! stage 1 (a): succeeded", "frz s0..i", "aut"])
        result = make_delivery(None, [run], [], self.root / "out")
        self.assertTrue(any("精简宏" in note and "zero-cycle" in note for note in result["notes"]))
        self.assertTrue((self.root / "out" / "macro" / "optimize-verbatim.seq").is_file())

    def test_setup_runs_give_their_commands_as_macro_files(self):
        setup = self.root / "edit"
        setup.mkdir()
        record = {"steps": [{"action": "edit", "command_note": "Sent by update_lens",
                             "native_commands": ["GLA S4 HF1_CDGM", "GLA S7 HF1_CDGM"]}]}
        (setup / "execution-record.json").write_text(json.dumps(record), encoding="utf-8")
        result = make_delivery(None, [aut_run(self.root / "aut")], [], self.root / "out", setup_runs=[setup])
        text = (self.root / "out" / "macro" / "setup-1-edit.seq").read_text(encoding="utf-8")
        self.assertIn("GLA S4 HF1_CDGM", text)
        self.assertIn("类型化修改", text)
        self.assertIn("macro/setup-1-edit.seq", {item["path"] for item in result["files"]})
        wrong = self.root / "aut-only"
        aut_run(wrong)
        with self.assertRaisesRegex(ValueError, "no edit, scale or import"):
            make_delivery(None, [], [], self.root / "o9", setup_runs=[wrong])
        with self.assertRaisesRegex(ValueError, "Not a run directory"):
            make_delivery(None, [], [], self.root / "o8", setup_runs=[self.root / "nothing"])

    def test_several_aut_runs_get_numbered_files(self):
        first, second = aut_run(self.root / "a1"), aut_run(self.root / "a2")
        result = make_delivery(None, [first, second], [], self.root / "out")
        names = {item["path"] for item in result["files"]}
        self.assertIn("macro/optimize-equivalent.seq", names)
        self.assertIn("macro/optimize-equivalent-2.seq", names)
        self.assertIn("runs/stage-1-aut-output-2.lis", names)

    def test_refusals_happen_before_anything_is_written(self):
        Bundle(self.root / "bundle")
        (self.root / "exists").mkdir()
        cases = [
            (lambda: make_delivery(self.root / "bundle", [], [], self.root / "exists"), "already exists"),
            (lambda: make_delivery(self.root / "bundle", [], [], self.root / "missing" / "out"), "parent"),
            (lambda: make_delivery(None, [], [], self.root / "o1"), "at least one"),
            (lambda: make_delivery(self.root / "nothing", [], [], self.root / "o2"), "Not a comparison bundle"),
            (lambda: make_delivery(None, [self.root / "nothing"], [], self.root / "o3"), "Not an AUT run"),
            (lambda: make_delivery(None, [], [self.root / "bad.txt"], self.root / "o4"), "Expected an existing"),
        ]
        for call, message in cases:
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                call()
        self.assertFalse((self.root / "o1").exists() or (self.root / "o2").exists())

    def test_a_failure_while_copying_removes_the_half_written_folder(self):
        Bundle(self.root / "bundle")
        (self.root / "bundle" / "initial" / "spot-1" / "image.png").unlink()
        with self.assertRaises(OSError):
            make_delivery(self.root / "bundle", [], [], self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_the_command_line_reports_counts_and_refuses_an_existing_folder(self):
        Bundle(self.root / "bundle")
        out = self.root / "cli-out"
        import io
        from contextlib import redirect_stderr, redirect_stdout

        with redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(deliver.main(["--comparison", str(self.root / "bundle"), "--output-dir", str(out)]), 0)
        self.assertIn("个文件", printed.getvalue())
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(deliver.main(["--comparison", str(self.root / "bundle"), "--output-dir", str(out)]), 1)
        self.assertIn("already exists", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
