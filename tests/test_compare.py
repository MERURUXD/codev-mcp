"""Comparison workflow, protocol failures and real simulated MCP integration."""
from __future__ import annotations

import base64
import copy
import io
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codev_mcp.compare import (
    AnalysisConfig, analysis_requests, assert_equivalent, check_compatible, collect_artifacts,
    digest, export_listing, main, optical_state, poll_result, render_evaluation, run_comparison,
    validate_result, vignetting_differences,
)
from codev_mcp.batch import input_id, run_batch
from codev_mcp.models import AnalysisRequest
from codev_mcp.server import _strip_images
from codev_mcp.simulated import SimulatedBackend
from codev_mcp.stdio_client import ProtocolError, StdioClient


class LocalClient:
    """Test-only tool adapter; production always launches an MCP process."""
    def __init__(self, work, backend, timeout, logs):
        self.backend = SimulatedBackend(working_directory=work)
        self.broken = False

    def call(self, name, arguments=None, timeout=None):
        arguments = arguments or {}
        if name == "run_analysis":
            arguments = {"request": AnalysisRequest.model_validate(arguments["request"])}
        payload = getattr(self.backend, name)(**arguments).model_dump(mode="json")
        payload, images = _strip_images(payload)
        return payload, [{"type": "image", "data": i.base64_data, "mimeType": i.media_type} for i in images]

    def close(self):
        return {"returncode": 0, "close_session": self.backend.close_session().model_dump(mode="json")}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "singlet.len"
        self.source.write_bytes(b"simulated fixture")
        self.backend = SimulatedBackend(working_directory=self.root / "backend")
        self.lens = self.backend.open_lens(str(self.source)).model_dump(mode="json")

    def completed(self, index=0):
        request = analysis_requests(self.lens)[index][1]
        task = self.backend.run_analysis(AnalysisRequest.model_validate(request))
        snapshot = self.backend.get_analysis()
        while snapshot.task.state.value in {"queued", "running"}:
            snapshot = self.backend.get_analysis()
        return request, task.task_id, snapshot.model_dump(mode="json")

    def test_state_tolerance_and_configuration(self):
        a = optical_state(self.lens)
        b = copy.deepcopy(a)
        b["surfaces"][1]["radius"] += 1e-10
        assert_equivalent(a, b)
        b["surfaces"][1]["radius"] += 1
        with self.assertRaises(ValueError):
            assert_equivalent(a, b)
        changed = copy.deepcopy(self.lens)
        changed["fields"][0]["weight"] += 1
        with self.assertRaises(ValueError):
            check_compatible(self.lens, changed)
        differences = check_compatible(self.lens, changed, allow_field_weight_difference=True)
        self.assertEqual(differences[0]["field"], 1)
        changed["wavelengths"][0]["weight"] += 1
        with self.assertRaises(ValueError):
            check_compatible(self.lens, changed, allow_field_weight_difference=True)
        changed = copy.deepcopy(self.lens)
        changed["zoom_positions"] = 2
        with self.assertRaises(ValueError):
            check_compatible(self.lens, changed)
        changed = copy.deepcopy(self.lens)
        changed["fields"][0]["vuy"] = 0.25
        self.assertEqual(check_compatible(self.lens, changed), [])
        self.assertEqual(vignetting_differences(self.lens, changed),
                         [{"field": 1, "factor": "vuy", "initial": 0.0, "final": 0.25}])

    def test_contract_and_failure_flags(self):
        request, task_id, good = self.completed()
        validate_result(good, task_id, request, "simulated", self.lens)
        for key, value in (("task_id", "wrong"), ("kind", "mtf"), ("history_only", True),
                           ("output_truncated", True), ("state", "failed"), ("source", "codev")):
            with self.subTest(key=key):
                bad = copy.deepcopy(good)
                bad["task"][key] = value
                with self.assertRaises(ValueError):
                    validate_result(bad, task_id, request, "simulated", self.lens)
        for key in ("effective_focal_length", "f_number", "overall_length"):
            bad = copy.deepcopy(good)
            bad["first_order"][key] = None
            with self.assertRaises(ValueError):
                validate_result(bad, task_id, request, "simulated", self.lens)

    def test_mtf_incomplete_and_settings_mismatch(self):
        request, task_id, good = self.completed(2)
        validate_result(good, task_id, request, "simulated", self.lens)
        bad = copy.deepcopy(good)
        bad["mtf"]["curves"][0]["sagittal"].pop()
        with self.assertRaises(ValueError):
            validate_result(bad, task_id, request, "simulated", self.lens)
        bad = copy.deepcopy(good)
        bad["task"]["settings"]["frequencies"] = [10]
        with self.assertRaises(ValueError):
            validate_result(bad, task_id, request, "simulated", self.lens)

    def test_image_identity_missing_and_escaping_files(self):
        image = self.root / "image.png"
        image.write_bytes(b"image")
        result = {"image": {"path": str(image), "media_type": "image/png"}}
        blocks = [{"data": base64.b64encode(b"image").decode(), "mimeType": "image/png"}]
        dest = self.root / "dest"
        dest.mkdir()
        collect_artifacts(result, blocks, self.root, dest)
        blocks[0]["data"] = base64.b64encode(b"different").decode()
        with self.assertRaises(ValueError):
            collect_artifacts(result, blocks, self.root, dest)
        with self.assertRaises(ValueError):
            collect_artifacts(result, blocks, dest, dest)
        image.unlink()
        with self.assertRaises(ValueError):
            collect_artifacts(result, blocks, self.root, dest)

    def test_real_plot_required_but_simulated_plot_is_absent(self):
        request, task_id, snapshot = self.completed(4)
        result = validate_result(snapshot, task_id, request, "simulated", self.lens)
        payload, images = _strip_images(snapshot)
        blocks = [{"data": i.base64_data, "mimeType": i.media_type} for i in images]
        dest = self.root / "native"
        dest.mkdir()
        collect_artifacts(payload["native_plot"], blocks, self.root, dest)
        result["source"] = "codev"
        with self.assertRaises(ValueError):
            collect_artifacts(result, blocks, self.root, dest)

    def test_async_timeout_records_unconfirmed_cancel(self):
        class Client:
            broken = False

            def call(self, name, *args, **kwargs):
                if name == "run_analysis":
                    return {"task_id": "t"}, []
                if name == "cancel_analysis":
                    return {"requested": True, "still_running": True}, []
                return {"task": {"task_id": "t", "state": "running"}}, []

        with patch("codev_mcp.compare.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                poll_result(Client(), {"kind": "spot_diagram"}, 1, self.root)
        cancel = json.loads((self.root / "cancellation.json").read_text())
        self.assertTrue(cancel["still_running"])

    def test_polled_task_must_match(self):
        class Client:
            broken = False

            def call(self, name, *args, **kwargs):
                return ({"task_id": "one"} if name == "run_analysis" else
                        {"task": {"task_id": "two", "state": "succeeded"}}), []
        with self.assertRaises(ValueError):
            poll_result(Client(), {"kind": "first_order"}, 1, self.root)

    def test_rerun_isolation_and_bundle_hashes(self):
        before = digest(self.source)
        with patch("codev_mcp.compare.ROOT", self.root):
            a, ma = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                   client_factory=LocalClient)
            b, mb = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                   client_factory=LocalClient)
        self.assertNotEqual(a, b)
        self.assertEqual(ma["status"], "succeeded", ma.get("error"))
        self.assertEqual(mb["status"], "succeeded", mb.get("error"))
        self.assertEqual(before, digest(self.source))
        for entry in ma["analyses"]:
            self.assertIsNotNone(entry["provenance"]["lens_id"])
            for file in entry["artifacts"]:
                self.assertEqual(file["sha256"], digest(a / file["path"]))

    def test_state_change_fails_without_continuing(self):
        class MutatingClient(LocalClient):
            def call(self, name, *args, **kwargs):
                result, images = super().call(name, *args, **kwargs)
                if name == "get_lens":
                    result["surfaces"][1]["radius"] += 1
                return result, images
        with patch("codev_mcp.compare.ROOT", self.root):
            path, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                           client_factory=MutatingClient)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(manifest["analyses"]), 1)
        self.assertTrue((path / "initial/first_order/lens-after.json").is_file())

    def test_incompatible_inputs_stop_before_any_analysis(self):
        other = self.root / "dbgauss.len"
        other.write_bytes(b"different simulated sample")
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.source, other, self.root / "out", "simulated", 10,
                                         client_factory=LocalClient)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["analyses"], [])
        self.assertTrue(all(v["unchanged"] for v in manifest["inputs"].values()))

    def test_unconfirmed_cleanup_is_not_success(self):
        class BadCleanup(LocalClient):
            def close(self):
                result = super().close()
                result["forced_server_stop"] = True
                return result
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                              client_factory=BadCleanup)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("cleanup", manifest["error"])
        self.assertTrue((bundle / "initial/preflight/cleanup.json").is_file())

    def test_missing_image_stops_at_first_image_analysis(self):
        class MissingImage(LocalClient):
            def call(self, name, *args, **kwargs):
                result, images = super().call(name, *args, **kwargs)
                if name == "get_analysis" and result.get("spot_diagram"):
                    result["spot_diagram"]["image"] = None
                    images = []
                return result, images
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                         client_factory=MissingImage)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual([e["status"] for e in manifest["analyses"]], ["succeeded", "failed"])
        self.assertIn("missing its image", manifest["error"])

    def three_fields(self):
        """The simulated dbgauss fixture: three fields, so three spot analyses."""
        path = self.root / "dbgauss.len"
        path.write_bytes(b"simulated dbgauss fixture")
        return path

    def failing_client(self, kind, matching, closes_cleanly=True):
        """A client whose spot analysis of field 2 fails with the given error kind."""
        class FailingSpot(LocalClient):
            def call(self, name, *args, **kwargs):
                result, images = super().call(name, *args, **kwargs)
                spot = result.get("spot_diagram") if name == "get_analysis" else None
                if spot and matching(spot):
                    result["task"]["state"] = "failed"
                    result["task"]["error"] = {"kind": kind, "message": "No ray of the plot grid reached the image surface."}
                    result["spot_diagram"] = None
                    images = []
                return result, images

            def close(self):
                record = super().close()
                if not closes_cleanly:
                    record["forced_server_stop"] = True
                return record
        return FailingSpot

    def test_a_failed_analysis_is_recorded_and_the_others_still_run(self):
        client = self.failing_client("computation_failed", lambda spot: spot["field_number"] == 2)
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(self.three_fields(), None, self.root / "out", "simulated", 10,
                                              client_factory=client)
        self.assertEqual(manifest["status"], "failed")
        by_name = {e["name"]: e["status"] for e in manifest["analyses"]}
        self.assertEqual([name for name, status in by_name.items() if status == "failed"], ["spot-2"])
        self.assertIn("mtf", by_name)
        self.assertEqual({status for name, status in by_name.items() if name != "spot-2"}, {"succeeded"})
        self.assertEqual(len(manifest["analyses"]), 3 + 1 + 1 + 1 + 5)
        self.assertEqual(manifest["failed_analyses"],
                         [{"stage": "lens", "name": "spot-2", "kind": "computation_failed",
                           "error": "computation_failed: No ray of the plot grid reached the image surface."}])
        self.assertIn("1 analyses failed", manifest["error"])
        report = (bundle / "report.md").read_text(encoding="utf-8")
        self.assertIn("失败的分析", report)
        self.assertIn("spot-2", report)
        record = json.loads((bundle / "execution-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["steps"][0]["results"]["failed_analyses"], manifest["failed_analyses"])

    def test_a_pair_run_continues_after_a_failure_in_the_first_stage(self):
        client = self.failing_client("unsupported", lambda spot: spot["field_number"] == 1)
        source = self.three_fields()
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(source, source, self.root / "out", "simulated", 10,
                                         client_factory=client)
        self.assertEqual(manifest["status"], "failed")
        failed = [(item["stage"], item["name"]) for item in manifest["failed_analyses"]]
        self.assertEqual(failed, [("initial", "spot-1"), ("final", "spot-1")])
        self.assertEqual(sum(1 for e in manifest["analyses"] if e["status"] == "succeeded"),
                         len(manifest["analyses"]) - 2)
        self.assertTrue((self.root / "out").is_dir())

    def test_a_lost_session_still_stops_the_run(self):
        client = self.failing_client("session_invalid", lambda spot: spot["field_number"] == 2)
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.three_fields(), None, self.root / "out", "simulated", 10,
                                         client_factory=client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failed_analyses"], [])
        self.assertEqual([e["status"] for e in manifest["analyses"]][-1], "failed")
        self.assertNotIn("running", [e["status"] for e in manifest["analyses"]])
        self.assertLess(len(manifest["analyses"]), 3 + 1 + 1 + 1 + 5)

    def test_a_failure_with_unconfirmed_cleanup_stops_the_run(self):
        client = self.failing_client("computation_failed", lambda spot: spot["field_number"] == 2,
                                     closes_cleanly=False)
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.three_fields(), None, self.root / "out", "simulated", 10,
                                         client_factory=client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failed_analyses"], [])
        self.assertLess(len(manifest["analyses"]), 3 + 1 + 1 + 1 + 5)

    def test_cleanup_failure_preserves_primary_failure(self):
        class FailingClient(LocalClient):
            def call(self, name, *args, **kwargs):
                raise TimeoutError("primary timeout")

            def close(self):
                super().close()
                raise RuntimeError("secondary cleanup error")
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                              client_factory=FailingClient)
        self.assertEqual(manifest["error"], "TimeoutError: primary timeout")
        cleanup = json.loads((bundle / "initial/preflight/cleanup.json").read_text())
        self.assertIn("secondary", cleanup["close_error"])

    def test_owned_process_release_failure_stops_comparison(self):
        class BadRelease(LocalClient):
            closes = 0

            def close(self):
                type(self).closes += 1
                record = super().close()
                record["close_session"].setdefault("details", {})["cleanup_confirmed"] = False
                return record

        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                         client_factory=BadRelease)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(BadRelease.closes, 1)
        self.assertFalse(manifest["performance"]["sessions"][0]["cleanup_confirmed"])

    def test_real_stdio_simulated_end_to_end(self):
        # Exercise server + worker + image content over actual pipes.
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 30)
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertEqual(len(manifest["analyses"]), 18)
        self.assertEqual(manifest["source"], "simulated")
        self.assertIn("不是作业镜头实测", (bundle / "comparison.md").read_text(encoding="utf-8"))
        for entry in manifest["analyses"]:
            snapshot = json.loads((bundle / entry["snapshot"]).read_text(encoding="utf-8"))
            self.assertEqual(snapshot["source"], "simulated")

    def test_config_rejects_invalid_values_before_client_start(self):
        bad = (AnalysisConfig(kinds=("mtf", "mtf")), AnalysisConfig(fields=(0,)),
               AnalysisConfig(frequencies=(20, 10)), AnalysisConfig(spot_grid=1),
               AnalysisConfig(zoom_positions=(0,)))
        with patch("codev_mcp.compare.ROOT", self.root):
            for config in bad:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                   client_factory=LocalClient, config=config)
        self.assertFalse((self.root / "out").exists())
        with patch("codev_mcp.compare.ROOT", self.root):
            for config in (AnalysisConfig(kinds=("first_order",), zoom_positions=(1,)),):
                with self.subTest(reuse=config), self.assertRaises(ValueError):
                    run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                   client_factory=LocalClient, config=config,
                                   max_analyses_per_session=3)
        self.assertFalse((self.root / "out").exists())

    def test_configured_requests_and_actual_settings(self):
        config = AnalysisConfig(kinds=("spot_diagram", "mtf"), fields=(1,),
                                frequencies=(0.0, 20.0, 40.0), spot_grid=5)
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                              client_factory=LocalClient, config=config)
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertEqual(len(manifest["analyses"]), 4)
        self.assertEqual(manifest["analyses"][0]["request"]["options"]["ray_grid"], 5)
        self.assertEqual(manifest["analyses"][1]["actual_settings"]["frequencies"], [0.0, 20.0, 40.0])
        self.assertTrue((bundle / "comparison.md").is_file())

    def test_bounded_reuse_matches_isolated_results_and_releases(self):
        config = AnalysisConfig(kinds=("first_order", "spot_diagram", "mtf"), fields=(1,),
                                frequencies=(0.0, 20.0), spot_grid=5)
        with patch("codev_mcp.compare.ROOT", self.root):
            isolated, first = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                             client_factory=LocalClient, config=config)
            reused, second = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                            client_factory=LocalClient, config=config,
                                            max_analyses_per_session=3)
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(len(first["performance"]["sessions"]), 8)
        self.assertEqual(len(second["performance"]["sessions"]), 4)
        self.assertTrue(all(s["cleanup_confirmed"] for s in second["performance"]["sessions"]))
        for a, b in zip(first["analyses"], second["analyses"]):
            self.assertEqual((a["stage"], a["name"]), (b["stage"], b["name"]))
            left = json.loads((isolated / a["snapshot"]).read_text())
            right = json.loads((reused / b["snapshot"]).read_text())
            kind = a["request"]["kind"]
            for field in ("effective_focal_length", "rms_radius", "frequencies"):
                if field in left[kind]:
                    self.assertEqual(left[kind][field], right[kind][field])
            self.assertIn("compute_seconds", b)
            self.assertIn("read_after_seconds", b)

    def test_bounded_reuse_rejects_drift_and_closes_group(self):
        class DriftClient(LocalClient):
            instances = []

            def __init__(self, *args):
                super().__init__(*args)
                self.instances.append(self)
                self.lens_reads_after_analysis = 0
                self.analysis_done = False

            def call(self, name, arguments=None, timeout=None):
                result, images = super().call(name, arguments, timeout)
                if name == "get_analysis" and (result.get("task") or {}).get("state") == "succeeded":
                    self.analysis_done = True
                if name == "get_lens" and self.analysis_done:
                    self.lens_reads_after_analysis += 1
                    if self.lens_reads_after_analysis > 1:
                        result["surfaces"][1]["radius"] += 1
                return result, images

        config = AnalysisConfig(kinds=("first_order", "mtf"), frequencies=(0.0, 20.0))
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                         client_factory=DriftClient, config=config,
                                         max_analyses_per_session=3)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(manifest["analyses"]), 2)
        self.assertTrue(manifest["performance"]["sessions"][-1]["cleanup_confirmed"])

    def test_full_reuse_includes_wavefront_and_all_plots_without_crossing_lenses(self):
        # Draw first, then numeric MTF/WAV: option settings and task history must
        # not leak from a native plot into the following numeric result.
        config = AnalysisConfig(kinds=("native_plot", "wavefront", "mtf", "first_order", "spot_diagram"))
        with patch("codev_mcp.compare.ROOT", self.root):
            isolated, first = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                             client_factory=LocalClient, config=config)
            reused, second = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                            client_factory=LocalClient, config=config,
                                            max_analyses_per_session=8)
        self.assertEqual(second["status"], "succeeded", second.get("error"))
        self.assertEqual(len(first["performance"]["sessions"]), 20)  # singlet has one field
        self.assertEqual(len(second["performance"]["sessions"]), 6)
        for record in second["performance"]["sessions"]:
            self.assertLessEqual(record["analyses"], 8)
            self.assertTrue(record["cleanup_confirmed"])
        from codev_mcp.numeric_metrics import metrics
        for a, b in zip(first["analyses"], second["analyses"]):
            self.assertEqual(a["request"], b["request"])
            self.assertEqual(a["actual_settings"], b["actual_settings"])
            self.assertEqual(b["before_fingerprint"], b["after_fingerprint"])
            left = json.loads((isolated / a["snapshot"]).read_text())
            right = json.loads((reused / b["snapshot"]).read_text())
            kind = a["request"]["kind"]
            assert_equivalent(metrics(left[kind], kind), metrics(right[kind], kind))
        first_session = second["analyses"][0]["session_id"]
        self.assertNotEqual(first_session, second["analyses"][9]["session_id"])

    def test_bounded_reuse_cleanup_failure_blocks_success(self):
        class BadCleanup(LocalClient):
            calls = 0

            def close(self):
                self.__class__.calls += 1
                result = super().close()
                if self.__class__.calls == 3:
                    result["forced_server_stop"] = True
                return result

        BadCleanup.calls = 0
        config = AnalysisConfig(kinds=("first_order", "mtf"), frequencies=(0.0, 20.0))
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 10,
                                         client_factory=BadCleanup, config=config,
                                         max_analyses_per_session=3)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("cleanup", manifest["error"])
        self.assertFalse(manifest["performance"]["sessions"][-1]["cleanup_confirmed"])

    def test_wavefront_and_plot_failures_close_reused_group_before_continuing(self):
        for failed_kind in ("wavefront", "native_plot"):
            with self.subTest(kind=failed_kind):
                class Failing(LocalClient):
                    failed = False

                    def call(self, name, arguments=None, timeout=None):
                        result, images = super().call(name, arguments, timeout)
                        task = result.get("task") or {}
                        if (name == "get_analysis" and task.get("state") == "succeeded"
                                and task.get("kind") == failed_kind and not self.__class__.failed):
                            self.__class__.failed = True
                            task.update(state="failed", error={"kind": "computation_failed", "message": "probe"})
                        return result, images

                config = AnalysisConfig(kinds=("first_order", failed_kind, "mtf"))
                with patch("codev_mcp.compare.ROOT", self.root):
                    _, manifest = run_comparison(self.source, None, self.root / failed_kind, "simulated", 10,
                                                 client_factory=Failing, config=config,
                                                 max_analyses_per_session=8)
                self.assertEqual(len(manifest["failed_analyses"]), 1)
                failed_index = next(i for i, e in enumerate(manifest["analyses"]) if e["status"] == "failed")
                failed = manifest["analyses"][failed_index]
                after = manifest["analyses"][failed_index + 1]
                self.assertNotEqual(failed["session_id"], after["session_id"])
                self.assertEqual(manifest["analyses"][-1]["status"], "succeeded")
                self.assertTrue(all(s["cleanup_confirmed"] for s in manifest["performance"]["sessions"]))

    def test_bounded_reuse_timeout_requests_cancel_and_releases(self):
        class HangingClient(LocalClient):
            cancel_calls = 0

            def __init__(self, *args):
                super().__init__(*args)
                self.hanging_task = None

            def call(self, name, arguments=None, timeout=None):
                if name == "get_analysis" and self.hanging_task is not None:
                    return {"task": {"task_id": self.hanging_task, "state": "running"}}, []
                if name == "cancel_analysis" and self.hanging_task is not None:
                    self.__class__.cancel_calls += 1
                    return {"requested": True, "confirmed_stopped": True}, []
                result, images = super().call(name, arguments, timeout)
                if name == "run_analysis" and arguments["request"]["kind"] == "spot_diagram":
                    self.hanging_task = result["task_id"]
                return result, images

        HangingClient.cancel_calls = 0
        config = AnalysisConfig(kinds=("first_order", "spot_diagram"), fields=(1,))
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(self.source, self.source, self.root / "out", "simulated", 1.0,
                                              client_factory=HangingClient, config=config,
                                              max_analyses_per_session=3)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual([e["status"] for e in manifest["analyses"]], ["succeeded", "failed"])
        self.assertEqual(HangingClient.cancel_calls, 1)
        record = json.loads((bundle / "initial/spot-1/cancellation.json").read_text())
        self.assertTrue(record["confirmed_stopped"])
        self.assertTrue(manifest["performance"]["sessions"][-1]["cleanup_confirmed"])

    def test_multi_zoom_numeric_results_keep_position(self):
        source = self.root / "zoomtriplet.len"
        source.write_bytes(b"simulated zoom fixture")
        config = AnalysisConfig(kinds=("first_order", "spot_diagram", "mtf"), zoom_positions=(1, 2),
                                frequencies=(0.0, 20.0))
        with patch("codev_mcp.compare.ROOT", self.root):
            bundle, manifest = run_comparison(source, source, self.root / "out", "simulated", 10,
                                              client_factory=LocalClient, config=config)
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertEqual({e["request"]["options"]["zoom_position"] for e in manifest["analyses"]}, {1, 2})
        self.assertIn("z2-mtf", {e["name"] for e in manifest["analyses"]})
        self.assertIn("逐变焦数值对比", (bundle / "comparison.md").read_text(encoding="utf-8"))

    def test_batch_marks_incompatible_pair_and_keeps_stable_ids(self):
        same = self.root / "same.len"
        same.write_bytes(b"simulated singlet fixture")
        incompatible = self.root / "dbgauss.len"
        incompatible.write_bytes(b"simulated zoom fixture")
        config = AnalysisConfig(kinds=("first_order",))
        with patch("codev_mcp.compare.ROOT", self.root):
            batch, report = run_batch([self.source, same, incompatible], self.root / "batch-out", "simulated", 10,
                                      config=config, client_factory=LocalClient)
        self.assertEqual(report["status"], "failed")
        self.assertEqual([c["status"] for c in report["comparisons"]], ["succeeded", "failed"])
        self.assertTrue(report["comparisons"][0]["metrics"])
        self.assertEqual(report["inputs"][0]["id"], input_id(self.source))
        self.assertTrue(all(item["unchanged"] for item in report["inputs"]))
        self.assertIn("不兼容", (batch / "batch.md").read_text(encoding="utf-8"))

    def test_multi_zoom_detects_change_at_another_position(self):
        source = self.root / "zoomtriplet.len"
        source.write_bytes(b"simulated zoom fixture")

        class DriftClient(LocalClient):
            def __init__(self, *args):
                super().__init__(*args)
                self.finished_analysis = False

            def call(self, name, arguments=None, timeout=None):
                result, images = super().call(name, arguments, timeout)
                if name == "get_analysis" and (result.get("task") or {}).get("state") == "succeeded":
                    self.finished_analysis = True
                if name == "get_lens" and arguments == {"zoom_position": 2} and self.finished_analysis:
                    result["surfaces"][1]["radius"] += 1
                return result, images

        config = AnalysisConfig(kinds=("first_order",), zoom_positions=(1, 2))
        with patch("codev_mcp.compare.ROOT", self.root):
            _, manifest = run_comparison(source, source, self.root / "out", "simulated", 10,
                                         client_factory=DriftClient, config=config)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(manifest["analyses"]), 1)
        self.assertIn("analysis_lens.z2", manifest["error"])

    def test_batch_preserves_pair_bundle_when_summary_fails(self):
        second = self.root / "second.len"
        second.write_bytes(b"another simulated singlet")
        with (patch("codev_mcp.compare.ROOT", self.root),
              patch("codev_mcp.batch.pair_metrics", side_effect=ValueError("summary failed"))):
            batch, report = run_batch([self.source, second], self.root / "batch", "simulated", 10,
                                      config=AnalysisConfig(kinds=("first_order",)), client_factory=LocalClient)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["comparisons"][0]["metrics"], [])
        self.assertTrue((batch / report["comparisons"][0]["bundle"] / "manifest.json").is_file())

    def test_batch_stops_on_direct_interrupt(self):
        others = [self.root / f"candidate-{i}.len" for i in range(2)]
        for path in others:
            path.write_bytes(b"simulated fixture")
        with patch("codev_mcp.batch.run_comparison", side_effect=KeyboardInterrupt) as comparison:
            batch, report = run_batch([self.source, *others], self.root / "batch", "simulated", 10,
                                      config=AnalysisConfig(kinds=("first_order",)))
        self.assertEqual(comparison.call_count, 1)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual([item["status"] for item in report["comparisons"]], ["interrupted"])
        self.assertTrue(all(item["unchanged"] for item in report["inputs"]))
        self.assertEqual(json.loads((batch / "batch.json").read_text())["status"], "interrupted")
        self.assertIn("后续镜头未启动", (batch / "batch.md").read_text(encoding="utf-8"))

    def test_batch_stops_after_comparison_cleans_up_interrupt(self):
        others = [self.root / f"candidate-{i}.len" for i in range(2)]
        for path in others:
            path.write_bytes(b"simulated fixture")
        with (patch("codev_mcp.compare.ROOT", self.root),
              patch("codev_mcp.compare.poll_result", side_effect=KeyboardInterrupt)):
            batch, report = run_batch([self.source, *others], self.root / "batch", "simulated", 10,
                                      config=AnalysisConfig(kinds=("first_order",)), client_factory=LocalClient)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(len(report["comparisons"]), 1)
        bundle = batch / report["comparisons"][0]["bundle"]
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["interrupted"])
        self.assertTrue((bundle / "initial" / "first_order" / "cleanup.json").is_file())
        self.assertEqual(json.loads((batch / "batch.json").read_text())["status"], "interrupted")

    def test_batch_records_missing_input_at_final_hash_check(self):
        second = self.root / "second.len"
        second.write_bytes(b"another simulated singlet")
        original = run_comparison

        def remove_after_pair(*args, **kwargs):
            result = original(*args, **kwargs)
            second.unlink()
            return result

        with (patch("codev_mcp.compare.ROOT", self.root),
              patch("codev_mcp.batch.run_comparison", side_effect=remove_after_pair)):
            batch, report = run_batch([self.source, second], self.root / "batch", "simulated", 10,
                                      config=AnalysisConfig(kinds=("first_order",)), client_factory=LocalClient)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["inputs"][1]["unchanged"])
        self.assertIn("FileNotFoundError", report["input_errors"][0]["error"])
        self.assertEqual(json.loads((batch / "batch.json").read_text())["status"], "failed")
        self.assertIn("输入复核失败", (batch / "batch.md").read_text(encoding="utf-8"))

    def test_batch_metrics_include_units_and_mtf_frequency(self):
        second = self.root / "second.len"
        second.write_bytes(b"another simulated singlet")
        config = AnalysisConfig(kinds=("first_order", "spot_diagram", "mtf", "wavefront"),
                                frequencies=(0.0, 20.0))
        with patch("codev_mcp.compare.ROOT", self.root):
            batch, report = run_batch([self.source, second], self.root / "batch", "simulated", 10,
                                      config=config, client_factory=LocalClient)
        self.assertEqual(report["status"], "succeeded")
        metrics = report["comparisons"][0]["metrics"]
        by_metric = {item["metric"]: item for item in metrics}
        self.assertEqual(by_metric["effective_focal_length"]["unit"], "mm")
        self.assertEqual(by_metric["f_number"]["unit"], "1")
        self.assertEqual(by_metric["field-1.rms_radius"]["unit"], "mm")
        self.assertEqual(by_metric["field-1.rms_waves"]["unit"], "waves")
        self.assertEqual(by_metric["field-1.strehl"]["unit"], "1")
        mtf = [item for item in metrics if item["analysis"] == "mtf"]
        self.assertEqual({item["frequency"] for item in mtf}, {0.0, 20.0})
        self.assertEqual({item["frequency_unit"] for item in mtf}, {"cycles/mm"})
        self.assertEqual({item["unit"] for item in mtf}, {"1"})
        markdown = (batch / "batch.md").read_text(encoding="utf-8")
        self.assertIn("频率（cycles/mm）", markdown)
        self.assertIn("无量纲", markdown)


SPEC = Path(__file__).resolve().parents[1] / "docs" / "design" / "design-spec-dbgauss.json"


class SpecBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lens = self.root / "dbgauss.len"
        self.lens.write_bytes(b"simulated dbgauss fixture")
        self.spec = self.root / "spec.json"
        self.spec.write_bytes(SPEC.read_bytes())

    def run_spec(self, final=None, client_factory=LocalClient, spec=None):
        with patch("codev_mcp.compare.ROOT", self.root):
            return run_comparison(self.lens, final, self.root / "out", "simulated", 10,
                                  client_factory=client_factory, spec_path=spec or self.spec)

    def test_single_lens_bundle_with_spec_judgement(self):
        bundle, manifest = self.run_spec()
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertEqual(manifest["mode"], "single")
        self.assertEqual(list(manifest["inputs"]), ["lens"])
        self.assertEqual(len(manifest["analyses"]), 3 + 1 + 1 + 1 + 5)
        self.assertEqual(manifest["analyses"][4]["request"]["options"]["frequencies"],
                         [0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertTrue(manifest["design_spec"]["unchanged"])
        self.assertEqual(digest(bundle / "design-spec.json"), manifest["design_spec"]["sha256"])
        self.assertEqual(manifest["inputs"]["lens"]["listing"]["path"], None)
        record = json.loads((bundle / "evaluation.json").read_text(encoding="utf-8"))
        judged = record["stages"]["lens"]
        self.assertEqual(judged["status"], "unknown")
        self.assertEqual({i["status"] for i in judged["requirements"] + judged["criteria"]}, {"unknown"})
        self.assertEqual(manifest["evaluation"]["sha256"], digest(bundle / "evaluation.json"))
        figures = manifest["figures"]
        self.assertEqual(len(figures), 9)
        for figure in figures:
            self.assertEqual(figure["image_sha256"], digest(bundle / figure["image"]))
            if figure["origin"] == "service_redrawn_from_numbers":
                self.assertTrue((bundle / figure["numbers"]).is_file())
            else:
                self.assertIsNone(figure["numbers"])
        report = (bundle / "report.md").read_text(encoding="utf-8")
        for text in ("规格判定", "待补", "演示", "服务按数值重绘", "CODE V 原生", "不含像距"):
            self.assertIn(text, report)

    def test_pair_bundle_judges_both_stages(self):
        bundle, manifest = self.run_spec(final=self.lens)
        self.assertEqual(manifest["status"], "succeeded", manifest.get("error"))
        self.assertEqual(set(manifest["evaluation"]["status"]), {"initial", "final"})
        text = (bundle / "comparison.md").read_text(encoding="utf-8")
        self.assertIn("initial 判定", text)
        self.assertIn("final 判定", text)

    def test_spec_conflicts_and_lens_mismatch(self):
        with self.assertRaisesRegex(ValueError, "analysis configuration"):
            run_comparison(self.lens, None, self.root / "out", "simulated", 10, client_factory=LocalClient,
                           config=AnalysisConfig(), spec_path=self.spec)
        spec = json.loads(self.spec.read_text(encoding="utf-8"))
        spec["units"] = "cm"
        for item in spec["requirements"]:
            if item["unit"] == "mm":
                item["unit"] = "cm"
        other = self.root / "cm.json"
        other.write_text(json.dumps(spec), encoding="utf-8")
        bundle, manifest = self.run_spec(spec=other)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("units", manifest["error"])
        self.assertEqual(manifest["analyses"], [])
        self.assertTrue((bundle / "evaluation.json").is_file())

    def test_spec_change_during_run_fails(self):
        spec = self.spec

        class EditingClient(LocalClient):
            def call(self, name, arguments=None, timeout=None):
                if name == "run_analysis":
                    spec.write_bytes(spec.read_bytes() + b"\n")
                return super().call(name, arguments, timeout)

        _, manifest = self.run_spec(client_factory=EditingClient)
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse(manifest["design_spec"]["unchanged"])
        self.assertIn("Design spec changed", manifest["error"])

    def test_evaluation_table_escapes_pipes(self):
        item = {"id": "edge", "status": "unknown", "required": True, "minimum": 1, "maximum": None,
                "value": None, "unit": "mm", "reason": "height exceeds |R| of surface 5"}
        (self.root / "evaluation.json").write_text(json.dumps({"stages": {"lens": {
            "status": "unknown", "conditions": [], "requirements": [item], "criteria": []}}}), encoding="utf-8")
        lines = render_evaluation(self.root, {"design_spec": {"name": "x", "sha256": "0" * 64}})
        row = next(line for line in lines if line.startswith("| edge"))
        self.assertIn("exceeds \\|R\\| of", row)
        self.assertEqual(row.replace("\\|", "").count("|"), 7)

    def test_listing_export(self):
        destination = self.root / "stage" / "preflight"
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "LIS"):
            export_listing({"raw_listing": None}, destination, self.root, "codev")
        record = export_listing({"raw_listing": "LIS text\n"}, destination, self.root, "codev")
        self.assertEqual(record["path"], "stage/preflight/lis.txt")
        self.assertEqual(record["sha256"], digest(destination / "lis.txt"))

    def test_command_line_input_modes(self):
        for argv in (["--lens", str(self.lens), "--initial", str(self.lens)],
                     ["--initial", str(self.lens)],
                     ["--lens", str(self.lens), "--spec", str(self.spec), "--analyses", "mtf"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    patch("sys.stderr", io.StringIO()):
                main(argv + ["--backend", "simulated"])


class ProtocolTests(unittest.TestCase):
    def client(self, response=None):
        client = StdioClient.__new__(StdioClient)
        client.timeout = 0.01
        client.broken = False
        client.sequence = 0
        client.responses = queue.Queue()
        client.transcript = io.StringIO()
        client._send = lambda message: None
        if response is not None:
            client.responses.put(json.dumps(response))
        return client

    def test_queue_deadline_does_not_wait_for_newline(self):
        client = self.client()
        with self.assertRaises(TimeoutError):
            client.request("tools/list", {}, timeout=0.01)
        self.assertTrue(client.broken)

    def test_mismatched_id_breaks_stream(self):
        client = self.client({"jsonrpc": "2.0", "id": 3, "result": {}})
        with self.assertRaises(ProtocolError):
            client.request("tools/list", {})
        self.assertTrue(client.broken)

    def test_eof_fails(self):
        client = self.client()
        client.responses.put(None)
        with self.assertRaises(ProtocolError):
            client.request("tools/list", {})

    def test_a_long_wait_is_sliced_so_an_interrupt_is_seen_within_a_slice(self):
        from codev_mcp import stdio_client

        client = self.client()
        waits: list[float] = []
        real_get = client.responses.get

        def get(timeout):
            waits.append(timeout)
            if len(waits) == 3:
                raise KeyboardInterrupt  # what Python raises in the main thread between two slices
            return real_get(timeout=min(timeout, 0.01))

        client.responses.get = get
        with patch.object(stdio_client, "RESPONSE_POLL_SECONDS", 0.02):
            with self.assertRaises(KeyboardInterrupt):
                client.request("tools/list", {}, timeout=600)
        self.assertTrue(all(wait <= 0.02 for wait in waits), waits)
        self.assertTrue(client.broken)  # an interrupted request leaves the stream untrustworthy


if __name__ == "__main__":
    unittest.main()


class PupilRatioTests(unittest.TestCase):
    WAV = {"fields": [{"field_number": 1, "rms_waves": 1.0, "rays_traced": 948},
                      {"field_number": 2, "rms_waves": 2.0, "rays_traced": 474}],
           "weighted_rms_waves": 1.7}

    def test_ratios_are_relative_to_field_one(self):
        from codev_mcp.compare import pupil_fractions
        self.assertEqual(pupil_fractions(self.WAV), [1.0, 0.5])

    def test_missing_or_invalid_counts_give_none(self):
        from codev_mcp.compare import pupil_fractions
        for fields in ([], [{"rays_traced": 0}], [{"rays_traced": 948}, {}], [{"rays_traced": True}]):
            self.assertIsNone(pupil_fractions({"fields": fields}))

    def test_the_unweighted_composite_uses_field_weights_only(self):
        from codev_mcp.compare import equal_ray_weighted_rms
        self.assertAlmostEqual(equal_ray_weighted_rms(self.WAV), (2.5) ** 0.5, places=12)
        self.assertAlmostEqual(equal_ray_weighted_rms(self.WAV, [3.0, 1.0]), (7.0 / 4.0) ** 0.5, places=12)
        self.assertAlmostEqual(equal_ray_weighted_rms(self.WAV, [1.0]), (2.5) ** 0.5, places=12)  # wrong length
        self.assertIsNone(equal_ray_weighted_rms({"fields": []}))

    def test_notes_call_out_a_different_pupil_only_beyond_the_tolerance(self):
        from codev_mcp.compare import pupil_notes
        close = {**self.WAV, "fields": [self.WAV["fields"][0], {**self.WAV["fields"][1], "rays_traced": 460}]}
        self.assertFalse(any("光瞳不同" in line for line in pupil_notes(self.WAV, close)))
        far = {**self.WAV, "fields": [self.WAV["fields"][0], {**self.WAV["fields"][1], "rays_traced": 400}]}
        self.assertTrue(any("光瞳不同" in line for line in pupil_notes(self.WAV, far)))
        self.assertEqual(pupil_notes({"fields": []}), [])
        self.assertTrue(any("不是追迹失败" in line for line in pupil_notes(self.WAV)))
