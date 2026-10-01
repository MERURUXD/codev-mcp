"""Readable sequence export: only RES and WRL, new output only, execution record."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codev_mcp.export_seq import export_seq


class Session:
    wrl_writes = True

    def __init__(self, starting_directory):
        self.commands = []

    def start(self):
        return "10.2;Build (207)"

    def command_raw(self, text):
        self.commands.append(text)
        if text.startswith("res "):
            return "File input.len has been restored\r\nCommand End:"
        if text.startswith("wrl ") and self.wrl_writes:
            Path(text.split(" ", 1)[1] + ".seq").write_text("RDM;LEN\nEPD 50\n", encoding="utf-8")
            return "Sequence saved in file export.seq\r\nCommand End:"
        return "Command End:"

    def stop(self):
        return True


class ExportSeqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lens = self.root / "lens.len"
        self.lens.write_bytes(b"lens bytes")

    def test_writes_a_new_sequence_and_records_the_commands(self):
        target = self.root / "镜头.seq"
        result = export_seq(self.lens, target, run_root=self.root, session_factory=Session)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(target.read_text(encoding="utf-8"), "RDM;LEN\nEPD 50\n")
        record = json.loads((Path(result["run_directory"]) / "execution-record.json").read_text(encoding="utf-8"))
        self.assertEqual([c.split()[0] for c in record["steps"][0]["native_commands"]], ["RES", "WRL"])
        with self.assertRaisesRegex(ValueError, "new .seq"):
            export_seq(self.lens, target, run_root=self.root, session_factory=Session)

    def test_missing_sequence_is_a_failure(self):
        result = export_seq(self.lens, self.root / "out.seq", run_root=self.root,
                            session_factory=type("NoWrl", (Session,), {"wrl_writes": False}))
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "out.seq").exists())

    def test_unconfirmed_cleanup_fails_without_reporting_an_output(self):
        result = export_seq(self.lens, self.root / "out.seq", run_root=self.root,
                            session_factory=type("Stuck", (Session,), {"stop": lambda self: False}))
        self.assertEqual(result["status"], "failed")
        self.assertIn("cleanup unconfirmed", result["error"])
        self.assertNotIn("output", result)
        record = json.loads((Path(result["run_directory"]) / "execution-record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["steps"][0]["status"], record["steps"][0]["outputs"]), ("failed", []))


if __name__ == "__main__":
    unittest.main()
