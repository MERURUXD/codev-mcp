"""S1 finite scan isolation and failure accounting."""
from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from codev_mcp.models import AnalysisRequest, UpdateRequest
from codev_mcp.scan import run_scan, validate_grid
from codev_mcp.server import _strip_images
from codev_mcp.simulated import SimulatedBackend


class LocalClient:
    opens = []

    def __init__(self, work, backend, timeout, logs):
        self.backend = SimulatedBackend(working_directory=work)
        self.broken = False

    def call(self, name, arguments=None, timeout=None):
        arguments = arguments or {}
        if name == "run_analysis":
            arguments = {"request": AnalysisRequest.model_validate(arguments["request"])}
        if name == "update_lens":
            arguments = {"request": UpdateRequest.model_validate(arguments["request"])}
        if name == "open_lens":
            self.opens.append(arguments["path"])
        payload = getattr(self.backend, name)(**arguments).model_dump(mode="json")
        payload, images = _strip_images(payload)
        return payload, [{"type": "image", "data": i.base64_data, "mimeType": i.media_type}
                         for i in images]

    def close(self):
        return {"returncode": 0,
                "close_session": self.backend.close_session().model_dump(mode="json")}


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "singlet.len"
        self.source.write_bytes(b"user input")
        LocalClient.opens = []

    def scan(self, *, client_factory=LocalClient, values=(60.0, 61.0), surface=1):
        with patch("codev_mcp.scan.ROOT", self.root):
            return run_scan(self.source, self.root / "out", "simulated", 10,
                            surface, "radius", values, client_factory=client_factory)

    def test_grid_validation_before_engine(self):
        for values in ((1.0,), (1.0, 1.0), (1.0, float("nan")),
                       tuple(float(n) for n in range(17))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_grid(1, "radius", values, 10)
        with self.assertRaises(ValueError):
            validate_grid(1, "glass", (1.0, 2.0), 10)

    def test_each_sample_starts_at_same_baseline_and_input_unchanged(self):
        bundle, manifest = self.scan()
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertEqual([r["actual"] for r in manifest["samples"]], [60.0, 61.0])
        self.assertEqual(len(set(LocalClient.opens)), 3)
        self.assertTrue(manifest["input"]["unchanged"])
        self.assertEqual(self.source.read_bytes(), b"user input")
        for index in (1, 2):
            self.assertEqual(manifest["baseline"]["value"], 62.5)
            self.assertEqual((bundle / f"sample-{index:03d}" / "lens-before.json").is_file(), True)
        self.assertTrue((bundle / "metrics.csv").is_file())

    def test_rejected_sample_is_independent(self):
        bundle, manifest = self.scan(values=(60.0, 62.0), surface=999)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["samples"], [])
        self.assertTrue(manifest["input"]["unchanged"])
        self.assertTrue((bundle / "scan.md").is_file())

    def test_one_edit_failure_does_not_contaminate_next_sample(self):
        class RefuseFirst(LocalClient):
            failed_once = False

            def call(self, name, arguments=None, timeout=None):
                if name == "update_lens" and not type(self).failed_once:
                    type(self).failed_once = True
                    raise RuntimeError("injected edit failure")
                return super().call(name, arguments, timeout)

        bundle, manifest = self.scan(client_factory=RefuseFirst)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual([r["status"] for r in manifest["samples"]], ["failed", "succeeded"])
        self.assertEqual(manifest["samples"][1]["actual"], 61.0)
        self.assertTrue((bundle / "sample-001" / "cleanup.json").is_file())

    def test_unconfirmed_cleanup_stops_remaining_samples(self):
        class BadCleanup(LocalClient):
            count = 0

            def close(self):
                type(self).count += 1
                result = super().close()
                if type(self).count == 2:
                    result["forced_server_stop"] = True
                return result

        _, manifest = self.scan(client_factory=BadCleanup)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual([r["status"] for r in manifest["samples"]], ["failed"])
        self.assertTrue(manifest["cleanup_unconfirmed"])

    def test_missing_source_still_writes_failed_report(self):
        source = self.source

        class RemoveSource(LocalClient):
            closes = 0

            def close(self):
                result = super().close()
                type(self).closes += 1
                if type(self).closes == 1:
                    source.unlink()
                return result

        bundle, manifest = self.scan(client_factory=RemoveSource)
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse(manifest["input"]["unchanged"])
        self.assertIn("FileNotFoundError", manifest["input"]["verification_error"])
        self.assertEqual([row["status"] for row in manifest["samples"]], ["succeeded", "succeeded"])
        self.assertIn('"status": "failed"', (bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue((bundle / "metrics.csv").is_file())
        self.assertIn("输入核对错误：FileNotFoundError", (bundle / "scan.md").read_text(encoding="utf-8"))

    def test_interrupt_with_cleanup_failure_stops_next_sample(self):
        class Interrupted(LocalClient):
            opens_count = 0

            def call(self, name, arguments=None, timeout=None):
                if name == "update_lens":
                    raise KeyboardInterrupt("edit interrupted")
                return super().call(name, arguments, timeout)

            def close(self):
                result = super().close()
                type(self).opens_count += 1
                if type(self).opens_count == 2:
                    result["forced_server_stop"] = True
                return result

        _, manifest = self.scan(client_factory=Interrupted)
        self.assertTrue(manifest["interrupted"])
        self.assertEqual(len(manifest["samples"]), 1)
        self.assertIn("primary", manifest["samples"][0]["error_sources"])
        self.assertIn("cleanup", manifest["samples"][0]["error_sources"])

    def test_close_interrupt_stops_next_sample(self):
        class InterruptedClose(LocalClient):
            closes = 0

            def close(self):
                type(self).closes += 1
                if type(self).closes == 2:
                    raise KeyboardInterrupt("close interrupted")
                return super().close()

        _, manifest = self.scan(client_factory=InterruptedClose)
        self.assertTrue(manifest["interrupted"])
        self.assertEqual(len(manifest["samples"]), 1)
        self.assertIn("cleanup", manifest["samples"][0]["error_sources"])

    def quality_config(self, **overrides):
        config = {"schema_version": 1,
                  "analysis": {"kinds": ["first_order", "mtf", "spot_diagram", "wavefront"],
                               "fields": None, "frequencies": [0, 20], "spot_grid": 5},
                  "requirements": [{"id": "mtf-on-axis", "metric": "mtf", "unit": "ratio",
                                    "field": 1, "direction": "tangential", "frequency": 20,
                                    "minimum": 0.1, "maximum": None, "required": True}],
                  "export_indices": [1]}
        config.update(overrides)
        path = self.root / "quality.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_quality_scan_multimetric_and_reopened_candidate(self):
        class RoundTrip(LocalClient):
            def call(self, name, arguments=None, timeout=None):
                payload = super().call(name, arguments, timeout)
                if name == "open_lens":
                    content = Path(arguments["path"]).read_text(encoding="utf-8", errors="ignore")
                    if content.startswith("! Simulated lens file"):
                        for line in content.splitlines():
                            parts = line.split()
                            if parts and parts[0] == "1":
                                self.backend.lens.surfaces[1].radius = float(parts[1])
                                self.backend.lens_surface_state[1]["radius"] = float(parts[1])
                                payload = super().call("get_lens")
                                break
                return payload
        config = self.quality_config()
        with patch("codev_mcp.scan.ROOT", self.root):
            bundle, manifest = run_scan(self.source, self.root / "out", "simulated", 10,
                                        1, "radius", (60.0, 61.0), client_factory=RoundTrip,
                                        config_path=config)
        self.assertEqual(manifest["status"], "succeeded", manifest)
        self.assertEqual(manifest["samples"][0]["evaluation"]["status"], "unknown")
        self.assertTrue(manifest["samples"][0]["candidate"]["recomputed"])
        self.assertTrue((bundle / "sample-001" / "reopen" / "mtf" / "snapshot.json").is_file())
        self.assertTrue((bundle / "quality-metrics.csv").is_file())
        curves = json.loads((bundle / "curves.json").read_text(encoding="utf-8"))
        self.assertTrue(curves["mtf.f1.tangential.20"]["chart"])
        self.assertEqual(self.source.read_bytes(), b"user input")

    def test_quality_config_rejected_before_engine(self):
        config = self.quality_config()
        value = json.loads(config.read_text(encoding="utf-8"))
        value["requirements"][0]["frequency"] = 21
        config.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError), patch("codev_mcp.scan.ROOT", self.root):
            run_scan(self.source, self.root / "out", "simulated", 10, 1, "radius",
                     (60.0, 61.0), client_factory=LocalClient, config_path=config)
        self.assertEqual(LocalClient.opens, [])

    def test_quality_rejects_image_plane_solve(self):
        from codev_mcp.scan import _fixed_focus
        with self.assertRaisesRegex(ValueError, "image-plane"):
            _fixed_focus({"raw_listing": "SOLVES\n PIM\nNo pickups defined in system\nINFINITE CONJUGATES"})

    def test_quality_rejects_final_image_distance_as_air_gap(self):
        config = self.quality_config(export_indices=[])
        with patch("codev_mcp.scan.ROOT", self.root):
            _, manifest = run_scan(self.source, self.root / "out", "simulated", 10,
                                   2, "thickness", (95.0, 96.0), client_factory=LocalClient,
                                   config_path=config)
        self.assertEqual(manifest["samples"], [])
        self.assertIn("internal air space", manifest["error"])

    def test_failed_analysis_creates_curve_gap_and_next_sample_runs(self):
        class FirstMtfFails(LocalClient):
            failed = False

            def call(self, name, arguments=None, timeout=None):
                if (name == "run_analysis" and arguments["request"]["kind"] == "mtf"
                        and not type(self).failed):
                    type(self).failed = True
                    raise RuntimeError("injected MTF failure")
                return super().call(name, arguments, timeout)

        config = self.quality_config(export_indices=[])
        with patch("codev_mcp.scan.ROOT", self.root):
            bundle, manifest = run_scan(self.source, self.root / "out", "simulated", 10,
                                        1, "radius", (60.0, 61.0), client_factory=FirstMtfFails,
                                        config_path=config)
        self.assertEqual([r["status"] for r in manifest["samples"]], ["failed", "succeeded"])
        curves = json.loads((bundle / "curves.json").read_text(encoding="utf-8"))
        self.assertEqual([p["value"] for p in curves["mtf.f1.tangential.20"]["points"]][0], None)
        self.assertIsNotNone(curves["mtf.f1.tangential.20"]["points"][1]["value"])

    def test_candidate_reopen_mismatch_never_deliverable(self):
        config = self.quality_config()
        with patch("codev_mcp.scan.ROOT", self.root):
            _, manifest = run_scan(self.source, self.root / "out", "simulated", 10,
                                   1, "radius", (60.0, 61.0), client_factory=LocalClient,
                                   config_path=config)
        self.assertEqual(manifest["samples"][0]["status"], "failed")
        self.assertFalse(manifest["samples"][0]["candidate"]["deliverable"])

    def test_config_mutation_fails_final_report(self):
        config = self.quality_config(export_indices=[])
        class MutateConfig(LocalClient):
            closes = 0

            def close(self):
                result = super().close()
                type(self).closes += 1
                if type(self).closes == 1:
                    config.write_text(config.read_text(encoding="utf-8") + " ", encoding="utf-8")
                return result

        with patch("codev_mcp.scan.ROOT", self.root):
            _, manifest = run_scan(self.source, self.root / "out", "simulated", 10,
                                   1, "radius", (60.0, 61.0), client_factory=MutateConfig,
                                   config_path=config)
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse(manifest["evaluation_config_unchanged"])


if __name__ == "__main__":
    unittest.main()
