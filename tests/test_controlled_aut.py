"""AUT isolation, terminal-output and explicit checkpoint-commit guards."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codev_mcp import aut
from codev_mcp.aut_spec import expand_field_ramp, legacy_spec
from codev_mcp.checkpoints import (CheckpointPublishError, FieldState, LensCheckpointStore, LensSnapshot,
                                   SurfaceState, ZoomState, hash_file, parse_variable_controls)
from tests import workspace_temp_directory


def snapshot(radius: float | None, angles: tuple[float, ...] = ()) -> LensSnapshot:
    """Surface 1 has this radius; None makes it a plane."""
    fields = [FieldState(number=n, x_angle=0.0, y_angle=angle, weight=1.0, vux=0.0, vlx=0.0, vuy=0.0, vly=0.0)
              for n, angle in enumerate(angles, 1)]
    return LensSnapshot(
        surface_count=3, zoom_positions=1,
        variable_controls={str(n): {"CCY": "100", "THC": "100", "GLC": ""}
                           for n in range(3)},
        zooms=[ZoomState(position=1, surfaces=[
            SurfaceState(number=0, radius_infinite=True, thickness_infinite=True),
            SurfaceState(number=1, radius=radius, radius_infinite=radius is None, thickness=5),
            SurfaceState(number=2, radius_infinite=True, thickness=10),
        ], fields=fields)],
    )


class FakeVerifier:
    expected: LensSnapshot

    def __init__(self, **_kwargs):
        self.opened = None

    def open_lens(self, path: str):
        self.opened = path

    def _current_checkpoint(self):
        return SimpleNamespace(snapshot=self.expected)

    def _require_session(self):
        return SimpleNamespace(command=lambda _command: SIMPLE_LISTING,
                               output_is_truncated=lambda _output: False)

    def close_session(self):
        pass


SIMPLE_LISTING = "\n".join([
    "                RDY             THI     RMD       GLA           CCY   THC   GLC",
    "> OBJ:        INFINITY        INFINITY                          100   100",
    "  STO:        40.00000        5.000000       BK7_SCHOTT         100   100",
    "  IMG:        INFINITY        0.000000                          100   100",
    "SPECIFICATION DATA",
])


LISTING = """VARIABLE LIST
CYCLE NUMBER 0:

 ERR. F.  =      50.00000000
 Specific Constraints:        target        value         diff      active (**)
 EFL                    =   4.00000E+01   4.10000E+01   1.000E+00    **
CYCLE NUMBER 2:

 ERR. F.  =      20.00000000       (change =     -30.00000000)
 Active Constraints -   1:    target        value         diff        cost
 EFL                    =   4.00000E+01   4.00000E+01   0.000E+00  -1.000E-02
 Inactive Constraints:        target        value         diff
 OAL S1..1              <   4.00000E+00   5.00000E+00   1.000E+00
    Normal AUTO Completion - Maximum cycle limit reached
"""


class ScriptedSession:
    """Just enough of a CODE V session for the candidate process logic."""

    owned_processes = {123: "codevm.exe"}

    def __init__(self, *, variables="CUY S1", out_t_fails=False, listings=None):
        self.commands = []
        self.variables = variables
        self.out_t_fails = out_t_fails
        self.forbid_close = out_t_fails
        self.listings = list(listings or [LISTING])
        self.out_base = None

    def command(self, value):
        self.commands.append(value)
        if value == "go":
            return f"VARIABLE LIST\n  NO  PARAMETERS *\n   1    {self.variables}\n     1 VARIABLES\n" \
                   "Normal AUTO Completion - Evaluation only"
        if value.startswith("out ") and value != "out t":
            self.out_base = Path(value.split(" ", 1)[1])
        if value == "out t" and self.out_t_fails:
            raise RuntimeError("CODE V did not return to the command prompt")
        if value.startswith("sav "):
            Path(value.split(" ", 1)[1]).write_bytes(b"candidate lens")
        if value == "lis":
            return SIMPLE_LISTING
        return "Command End:"

    def async_command(self, value):
        self.commands.append("async:" + value)
        text = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
        Path(str(self.out_base) + ".lis").write_text(text, encoding="utf-8")

    def is_executing_command(self):
        return False

    def wait(self, _seconds):
        return 0

    def get_command_output(self):
        return "AUT> go"

    def output_is_truncated(self, _value):
        return False


class ControlledAutTest(unittest.TestCase):
    def setUp(self):
        self.temp = workspace_temp_directory("aut")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.root.mkdir()
        self.source = self.root / "source.len"
        self.source.write_bytes(b"source")
        self.store = LensCheckpointStore(self.root / "baseline-engine" / "checkpoints",
                                         backend_id="test-backend")
        self.store.ensure()
        self.lens_id, self.directory = self.store.create_lens()
        baseline = self.directory / "revision-000000.len"
        baseline.write_bytes(b"baseline")
        self.store.publish(self.directory, self.lens_id, 0, baseline, snapshot(40),
                           source_path=str(self.source))
        self.candidate = self.root / "candidate.len"
        self.candidate.write_bytes(b"candidate")
        self.result = self.root / "result.json"
        self.report = {
            "state": "complete", "accepted": False, "candidate_path": str(self.candidate),
            "candidate_sha256": hash_file(self.candidate),
            "candidate_snapshot": snapshot(35).to_dict(),
            "source": str(self.source), "source_sha256": hash_file(self.source),
            "checkpoint_directory": str(self.directory), "backend_id": "test-backend",
            "lens_id": self.lens_id, "revision": 0,
            "baseline_sha256": hash_file(baseline),
            "spec": legacy_spec(1, "radius", 25, 80, 0, 2, 60),
        }
        self.result.write_text(json.dumps(self.report), encoding="utf-8")
        FakeVerifier.expected = snapshot(35)

    def test_native_variable_controls_are_part_of_snapshot(self):
        # Use the real fixed columns because the native listing is column based.
        header = "                RDY             THI     RMD       GLA           CCY   THC   GLC"
        listing = "\n".join(["Lens", header,
                             "> OBJ:        INFINITY        INFINITY                          100   100",
                             "  STO:        40.00000        5.000000       BK7_SCHOTT           0   100",
                             "  IMG:        INFINITY        10.00000                          100   100",
                             "", "SPECIFICATION DATA"])
        controls = parse_variable_controls(listing, 3)
        self.assertEqual(controls["1"]["CCY"], "0")
        self.assertEqual(parse_variable_controls(listing.replace("  STO:", ">   1:"), 3)["1"]["CCY"], "0")
        self.assertEqual(parse_variable_controls(listing, 4), {})

    def test_relations_allow_pim_and_refuse_couplings_and_glass_variables(self):
        base = snapshot(40)
        base.variable_controls["1"]["THC"] = "PIM"
        relations = aut._relations(base)
        self.assertEqual(relations["controlled"],
                         [{"surface": 1, "code": "THC", "value": "PIM", "parameter": "thickness"}])
        spec = legacy_spec(1, "radius", 25, 80, 0, 2, 60)
        lens = SimpleNamespace(surfaces=[])
        self.assertEqual(aut._spec_preflight(base, lens, spec)["controlled"][0]["value"], "PIM")
        with self.assertRaisesRegex(ValueError, "controlled by a solve"):
            aut._spec_preflight(base, lens, legacy_spec(1, "thickness", 1, 9, 0, 2, 60))
        for code, value, message in (("CCY", "7", "coupled"), ("GLC", "0", "glass variables")):
            changed = snapshot(40)
            changed.variable_controls["1"][code] = value
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, message):
                aut._relations(changed)
        # A start on or outside a bound is no longer refused: CODE V moves the variable into the
        # bounds (E5 probe), so the stage record notes it instead.
        outside = legacy_spec(1, "radius", 45, 80, 0, 2, 60)
        aut._spec_preflight(snapshot(40), lens, outside)
        note = aut.start_bounds(snapshot(40), outside["stages"][0])
        self.assertEqual([(n["bound"], n["position"], n["value"], n["limit"]) for n in note],
                         [("lower", "outside", 40, 45)])
        on_bound = aut.start_bounds(snapshot(40), legacy_spec(1, "radius", 40, 80, 0, 2, 60)["stages"][0])
        self.assertEqual([(n["bound"], n["position"]) for n in on_bound], [("lower", "on")])
        self.assertEqual(aut.start_bounds(snapshot(40), legacy_spec(1, "radius", 25, 80, 0, 2, 60)["stages"][0]), [])
        self.assertEqual(aut.start_bounds(snapshot(40), {"variables": [{"surface": 1, "parameter": "thickness"}]}), [])
        with self.assertRaisesRegex(ValueError, "not an ordinary surface"):
            aut._spec_preflight(snapshot(40), lens, legacy_spec(2, "radius", 1, 80, 0, 2, 60))
        vig = legacy_spec(1, "radius", 25, 80, 0, 2, 60)
        vig["stages"][0]["set_vignetting"] = True
        with self.assertRaisesRegex(ValueError, "explicit clear apertures"):
            aut._spec_preflight(snapshot(40), lens, vig)

    def test_a_flat_surface_can_be_a_radius_variable_without_radius_bounds(self):
        lens = SimpleNamespace(surfaces=[])
        flat = snapshot(None)
        free = {"schema_version": 1, "kind": "aut_spec", "name": "flat", "stages": [{
            "name": "s", "variables": [{"surface": 1, "parameter": "radius"},
                                       {"surface": 1, "parameter": "thickness"}],
            "error_function": {"MXC": 2}}]}
        aut._spec_preflight(flat, lens, free)
        self.assertEqual(aut.start_bounds(flat, free["stages"][0]), [])
        with self.assertRaisesRegex(ValueError, "flat surface 1 cannot have radius bounds"):
            aut._spec_preflight(flat, lens, legacy_spec(1, "radius", 25, 80, 0, 2, 60))
        # A finite radius keeps its bounds, and an infinite thickness is still no variable.
        aut._spec_preflight(snapshot(40), lens, legacy_spec(1, "radius", 25, 80, 0, 2, 60))
        far = snapshot(40)
        far.zooms[0].surfaces[1].thickness, far.zooms[0].surfaces[1].thickness_infinite = None, True
        with self.assertRaisesRegex(ValueError, "thickness of surface 1 is infinite"):
            aut._spec_preflight(far, lens, legacy_spec(1, "thickness", 1, 9, 0, 2, 60))

    def test_only_a_radius_variable_may_change_its_infinity_flag(self):
        flat, bent = snapshot(None), snapshot(35)
        radius = {"variables": [{"surface": 1, "parameter": "radius"}]}
        thickness = {"variables": [{"surface": 1, "parameter": "thickness"}]}
        relations = {"controlled": []}
        self.assertEqual(aut.compare_snapshots(flat, bent, allowed_changes=aut._allowed_changes(radius, relations, flat)), [])
        self.assertEqual(aut.compare_snapshots(bent, flat, allowed_changes=aut._allowed_changes(radius, relations, bent)), [])
        problems = aut.compare_snapshots(flat, bent, allowed_changes=aut._allowed_changes(thickness, relations, flat))
        self.assertTrue(any("radius" in item["where"] + item["detail"] for item in problems), problems)

    def test_spec_child_records_a_flat_start_and_the_curved_result(self):
        spec = {"schema_version": 1, "kind": "aut_spec", "name": "flat", "stages": [{
            "name": "s", "variables": [{"surface": 1, "parameter": "radius"}], "error_function": {"MXC": 2}}]}
        session = ScriptedSession()
        code, result, _ = self.run_child(spec, session, [snapshot(35), snapshot(35), snapshot(35)],
                                         before=snapshot(None))
        self.assertEqual((code, result["state"]), (0, "complete"), result.get("error"))
        variable = result["stages"][0]["variables"][0]
        self.assertEqual((variable["before"], variable["before_infinite"]), (None, True))
        self.assertEqual((variable["after"], variable["after_infinite"]), (35, False))
        self.assertTrue(variable["within_bounds"])
        self.assertTrue(result["explicit_bounds_satisfied"])

    def test_legacy_request_limits_remain(self):
        with self.assertRaises(ValueError):
            aut._validate_request(1, "radius", 10, 10, 0, 1, 60)
        with self.assertRaises(ValueError):
            aut._validate_request(1, "radius", 10, 20, 0, 9, 60)

    def test_special_surface_listing_is_rejected(self):
        ordinary = SimpleNamespace(command=lambda _command: SIMPLE_LISTING,
                                   output_is_truncated=lambda _output: False)
        aut._require_ordinary_surfaces(ordinary, 3)
        special = SIMPLE_LISTING.replace("SPECIFICATION DATA", "  CON:\n  K: 0\nSPECIFICATION DATA")
        with self.assertRaises(ValueError):
            aut._require_ordinary_surfaces(
                SimpleNamespace(command=lambda _command: special,
                                output_is_truncated=lambda _output: False), 3)
        with self.assertRaises(ValueError):
            aut._require_ordinary_surfaces(ordinary, 4)

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_accept_is_explicit_and_refuses_duplicate_or_stale_candidate(self):
        self.assertEqual(self.store.load_current(self.directory).revision, 0)
        accepted = aut.accept(self.result)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.store.load_current(self.directory).revision, 1)
        with self.assertRaises(ValueError):
            aut.accept(self.result)

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_accept_allows_solve_coupled_and_typed_changes_only(self):
        from codev_mcp.checkpoints import WavelengthState
        lens_id, directory = self.store.create_lens()
        base = snapshot(40)
        base.variable_controls["1"]["THC"] = "PIM"
        base.wavelengths = [WavelengthState(number=1, micrometers=0.5876, weight=1)]
        baseline = directory / "revision-000000.len"
        baseline.write_bytes(b"pim baseline")
        self.store.publish(directory, lens_id, 0, baseline, base, source_path=str(self.source))
        spec = legacy_spec(1, "radius", 25, 80, 0, 2, 60)
        spec["stages"][0]["lens_changes"] = [{"target": "wavelength", "wavelength": 1,
                                              "parameter": "weight", "value": 2}]
        candidate_state = LensSnapshot.from_dict(base.to_dict())
        candidate_state.zooms[0].surfaces[1].radius = 35
        candidate_state.zooms[0].surfaces[1].thickness = 4.2
        candidate_state.wavelengths[0].weight = 2
        report = {**self.report, "spec": spec, "checkpoint_directory": str(directory), "lens_id": lens_id,
                  "baseline_sha256": hash_file(baseline), "candidate_snapshot": candidate_state.to_dict()}
        self.result.write_text(json.dumps(report), encoding="utf-8")
        accepted = aut.accept(self.result)
        self.assertEqual(accepted["accepted_revision"], 1)
        self.assertTrue(Path(accepted["accepted_path"]).is_file())
        record = json.loads((self.root / "execution-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["steps"][-1]["action"], "aut_accept")

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_accept_refuses_an_unrequested_change(self):
        changed = snapshot(35)
        changed.zooms[0].surfaces[2].thickness = 11
        self.result.write_text(json.dumps({**self.report, "candidate_snapshot": changed.to_dict()}),
                               encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the requested stages"):
            aut.accept(self.result)
        self.assertEqual(self.store.load_current(self.directory).revision, 0)

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_stale_pointer_or_modified_candidate_cannot_publish(self):
        self.candidate.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            aut.accept(self.result)
        self.assertEqual(self.store.load_current(self.directory).revision, 0)
        self.candidate.write_bytes(b"candidate")
        other = self.directory / "revision-000001.len"
        other.write_bytes(b"other")
        self.store.publish(self.directory, self.lens_id, 1, other, snapshot(40),
                           source_path=str(self.source))
        with self.assertRaises(ValueError):
            aut.accept(self.result)
        self.assertEqual(self.store.load_current(self.directory).revision, 1)

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_changed_bytes_during_revision_copy_cannot_publish(self):
        original = aut.shutil.copyfileobj

        def changed(source, destination):
            original(source, destination)
            destination.write(b"changed")

        with mock.patch.object(aut.shutil, "copyfileobj", changed):
            with self.assertRaises(ValueError):
                aut.accept(self.result)
        self.assertEqual(self.store.load_current(self.directory).revision, 0)

    def test_publish_rejects_bytes_changed_after_reopen(self):
        destination = self.directory / "revision-000001.len"
        destination.write_bytes(b"changed after reopen")
        with self.assertRaises(CheckpointPublishError):
            self.store.publish(self.directory, self.lens_id, 1, destination,
                               snapshot(35), source_path=str(self.source),
                               expected_sha256=hash_file(self.candidate))
        self.assertEqual(self.store.load_current(self.directory).revision, 0)

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_revision_denies_writers_until_pointer_is_published(self):
        import win32con
        import win32file

        original = LensCheckpointStore.publish

        def check_lock(store, directory, lens_id, revision, lens_path, state,
                       *, source_path, expected_sha256=None):
            with self.assertRaises(Exception):
                win32file.CreateFile(
                    str(lens_path), win32con.GENERIC_WRITE,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                    None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
            return original(store, directory, lens_id, revision, lens_path, state,
                            source_path=source_path, expected_sha256=expected_sha256)

        with mock.patch.object(LensCheckpointStore, "publish", check_lock):
            self.assertTrue(aut.accept(self.result)["accepted"])

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_failed_publish_keeps_previous_pointer(self):
        original = LensCheckpointStore.publish

        def fail(store, directory, lens_id, revision, lens_path, state, *, source_path,
                 expected_sha256=None):
            if revision == 1:
                raise OSError("metadata unavailable")
            return original(store, directory, lens_id, revision, lens_path, state,
                            source_path=source_path, expected_sha256=expected_sha256)

        with mock.patch.object(LensCheckpointStore, "publish", fail):
            with self.assertRaises(OSError):
                aut.accept(self.result)
        self.assertEqual(self.store.load_current(self.directory).revision, 0)
        self.assertTrue(aut.accept(self.result)["accepted"])
        self.assertEqual(self.store.load_current(self.directory).revision, 1)

    @mock.patch.object(aut, "_run_stage", new=lambda *args, **kwargs: {"verified": True})
    def test_post_commit_diagnostic_failure_does_not_reverse_success(self):
        original = LensCheckpointStore.publish

        def commit_then_raise(store, directory, lens_id, revision, lens_path, state,
                              *, source_path, expected_sha256=None):
            value = original(store, directory, lens_id, revision, lens_path, state,
                             source_path=source_path, expected_sha256=expected_sha256)
            if revision == 1:
                raise OSError("diagnostic after pointer")
            return value

        with mock.patch.object(LensCheckpointStore, "publish", commit_then_raise):
            accepted = aut.accept(self.result)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.store.load_current(self.directory).revision, 1)

    def run_child(self, spec, session, snapshots, *, general=None, before=None):
        backend_session = session
        starting = before if before is not None else snapshot(40)

        class Backend:
            _listing = None
            closed = False

            def __init__(self, **kwargs):
                self._lens = None
                Path(kwargs["working_directory"]).mkdir(parents=True, exist_ok=True)

            def open_lens(self, _path):
                pass

            def _require_session(self):
                return backend_session

            def _current_checkpoint(self):
                return SimpleNamespace(snapshot=starting)

            def _read_lens(self):
                return SimpleNamespace(surfaces=[])

            def _load_lens_file(self, _session, _path):
                pass

            def close_session(self):
                if session.forbid_close:
                    raise AssertionError("an unterminated AUT must not call COM cleanup")
                return SimpleNamespace(details={"cleanup_confirmed": True})

        config = {"root": str(self.root), "baseline_path": str(self.directory / "revision-000000.len"),
                  "baseline_snapshot": starting.to_dict(), "spec": spec}
        config_path = self.root / "request.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with (mock.patch.object(aut, "ComBackend", Backend),
              mock.patch.object(aut, "_require_ordinary_surfaces"),
              mock.patch.object(aut, "read_snapshot", side_effect=snapshots),
              mock.patch.object(aut, "_analysis", return_value={"effective_focal_length": 40.0}),
              mock.patch.object(aut, "_general_checks", return_value=general or []),
              mock.patch.object(aut, "_owned_cleanup", return_value=[]) as cleanup):
            code = aut._spec_child(config_path)
        result = json.loads((self.root / "child-result.json").read_text(encoding="utf-8"))
        return code, result, cleanup

    def test_spec_child_runs_constrained_stage_and_restores_controls(self):
        spec = legacy_spec(1, "radius", 25, 80, 0, 2, 60)
        spec["stages"][0]["constraints"] = [{"operand": "EFL", "relation": "=", "value": 40},
                                            {"operand": "OAL", "relation": "<", "value": 4}]
        spec["stages"][0]["general_constraints"] = {"MNE": 1}
        session = ScriptedSession()
        code, result, _ = self.run_child(spec, session, [snapshot(35), snapshot(35), snapshot(35)])
        self.assertEqual((code, result["state"]), (0, "complete"), result.get("error"))
        stage = result["stages"][0]
        self.assertEqual(stage["completion"], "Maximum cycle limit reached")
        self.assertEqual([c["status"] for c in stage["constraints"]], ["satisfied", "violated"])
        self.assertFalse(result["final_constraints_satisfied"])
        self.assertEqual(stage["variables"][0]["before"], 40)
        self.assertEqual(stage["variables"][0]["after"], 35)
        commands = session.commands
        self.assertEqual(commands[:2], ["frz s0..i", "ccy s1 0"])
        self.assertIn("efl = 40", commands)
        self.assertIn("oal s1..1 < 4", commands)
        self.assertIn("mne 1", commands)
        self.assertLess(commands.index("out t"), commands.index("ccy s1 100"))
        self.assertTrue(any(command.startswith("sav ") for command in commands))
        self.assertEqual(stage["native_commands"][-1].split()[0], "sav")
        self.assertTrue((self.root / "stage-1-aut-output.lis").is_file())

    def test_spec_child_refuses_a_different_native_variable_list(self):
        session = ScriptedSession(variables="CUY S1       CUY S2")
        code, result, _ = self.run_child(legacy_spec(1, "radius", 25, 80, 0, 2, 60), session, [])
        self.assertEqual((code, result["state"]), (1, "failed"))
        self.assertIn("variable list", result["error"])
        self.assertFalse(any(command.startswith("sav ") for command in session.commands))

    def test_child_discards_unterminated_aut_without_saving(self):
        session = ScriptedSession(out_t_fails=True)
        code, result, cleanup = self.run_child(legacy_spec(1, "radius", 25, 80, 0, 2, 60), session, [])
        self.assertEqual(code, 1)
        self.assertTrue(result["discarded_unterminated_session"])
        cleanup.assert_called_once()
        self.assertFalse(any(command.startswith("sav ") for command in session.commands))
        self.assertEqual(self.store.load_current(self.directory).revision, 0)

    def test_failed_later_stage_keeps_the_earlier_candidate(self):
        spec = legacy_spec(1, "radius", 25, 80, 0, 2, 60)
        spec["stages"].append({"name": "polish", "variables": [{"surface": 1, "parameter": "radius"}],
                               "error_function": {"MXC": 3}})
        session = ScriptedSession(listings=[LISTING, LISTING.replace("Normal AUTO Completion", "Stopped")])
        code, result, _ = self.run_child(spec, session, [snapshot(35), snapshot(35)])
        self.assertEqual((code, result["state"]), (1, "failed"))
        self.assertEqual(result["last_good_stage"], 1)
        self.assertEqual([s["status"] for s in result["stages"]], ["succeeded", "failed"])
        self.assertTrue(Path(result["stages"][0]["candidate"]["path"]).is_file())
        self.assertIn("stage 2 (polish)", result["error"])

    def ramp_spec(self):
        return expand_field_ramp({
            "schema_version": 1, "kind": "aut_spec", "name": "ramp", "wall_seconds": 60,
            "field_ramp": {"name": "grow", "steps": [
                {"fields": [{"field": 2, "y_angle": 8.0}]},
                {"fields": [{"field": 2, "y_angle": 10.0, "weight": 2}]}],
                "stage": {"variables": [{"surface": 1, "parameter": "radius", "lower": 25, "upper": 80}],
                          "error_function": {"MXC": 2}}}})

    def test_a_field_ramp_runs_step_by_step_from_each_previous_candidate(self):
        spec = self.ramp_spec()
        session = ScriptedSession()
        snapshots = [snapshot(35, (0.0, 8.0)), snapshot(35, (0.0, 8.0)),
                     snapshot(34, (0.0, 10.0)), snapshot(34, (0.0, 10.0)), snapshot(34, (0.0, 10.0))]
        code, result, _ = self.run_child(spec, session, snapshots, before=snapshot(40, (0.0, 12.0)))
        self.assertEqual((code, result["state"]), (0, "complete"), result.get("error"))
        self.assertEqual([stage["name"] for stage in result["stages"]], ["grow-1", "grow-2"])
        self.assertEqual([stage["ramp_step"] for stage in result["stages"]], [1, 2])
        first, second = (stage["native_commands"] for stage in result["stages"])
        self.assertEqual(first[0], "yan f2 8")
        self.assertEqual(second[:2], ["yan f2 10", "wtf f2 2"])
        self.assertLess(second.index("wtf f2 2"), second.index("frz s0..i"))
        self.assertTrue(Path(result["stages"][0]["candidate"]["path"]).is_file())
        self.assertEqual(result["stages"][1]["variables"][0]["before"], 35)

    def test_a_field_edit_that_moves_another_field_fails_the_stage(self):
        spec = self.ramp_spec()
        session = ScriptedSession()
        wrong = snapshot(35, (0.0, 8.0))
        wrong.zooms[0].fields[0].y_angle = 1.0  # field 1 moved as well
        code, result, _ = self.run_child(spec, session, [wrong], before=snapshot(40, (0.0, 12.0)))
        self.assertEqual((code, result["state"]), (1, "failed"))
        self.assertIn("outside the stage's parameters", result["stages"][0]["error"])

    def test_a_ramp_on_a_missing_field_is_refused_before_any_command(self):
        spec = self.ramp_spec()
        spec["stages"][0]["lens_changes"][0]["field"] = 5
        session = ScriptedSession()
        code, result, _ = self.run_child(spec, session, [], before=snapshot(40, (0.0, 12.0)))
        self.assertEqual((code, result["state"]), (1, "failed"))
        self.assertIn("field 5 does not exist", result["error"])
        self.assertEqual(session.commands, [])

    def test_a_start_on_the_bound_runs_and_is_recorded(self):
        spec = legacy_spec(1, "radius", 40, 80, 0, 2, 60)
        session = ScriptedSession()
        code, result, _ = self.run_child(spec, session, [snapshot(41), snapshot(41), snapshot(41)])
        self.assertEqual((code, result["state"]), (0, "complete"), result.get("error"))
        note = result["stages"][0]["start_bounds"][0]
        self.assertEqual((note["position"], note["bound"], note["limit"]), ("on", "lower", 40))

    def test_prepare_expands_the_ramp_and_keeps_the_input_spec(self):
        spec = {"schema_version": 1, "kind": "aut_spec", "name": "ramp", "wall_seconds": 60,
                "field_ramp": {"name": "grow", "steps": [{"fields": [{"field": 2, "y_angle": 8.0}]}],
                               "stage": {"variables": [{"surface": 1, "parameter": "radius"}],
                                         "error_function": {"MXC": 2}}}}
        seen = {}

        def capture(_root, _phase, payload, **_kwargs):
            seen.update(payload)
            raise RuntimeError("stop after the baseline request")

        with mock.patch.object(aut, "_run_stage", capture), self.assertRaises(RuntimeError):
            aut.prepare_spec(self.source, self.root / "ramp-run", spec, spec_sha256="abc")
        self.assertEqual([s["name"] for s in seen["spec"]["stages"]], ["grow-1"])
        self.assertNotIn("field_ramp", seen["spec"])
        self.assertIn("field_ramp", seen["spec_input"])
        failed = json.loads((self.root / "ramp-run" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["spec_input"]["field_ramp"]["name"], "grow")

    def test_general_constraints_are_checked_on_the_candidate(self):
        from tests.test_scale import lens_payload
        from codev_mcp.models import LensData
        lens = LensData.model_validate(lens_payload())
        stage = {"general_constraints": {"MNT": 3.0, "MXT": 11.0, "MNA": 0.1}}
        items = {item["id"]: item for item in aut._general_checks(stage, lens)}
        self.assertEqual(items["MNT"]["status"], "pass")
        self.assertEqual(items["MXT"]["status"], "fail")
        self.assertEqual(items["MNA"]["status"], "pass")
        self.assertIn("reference rays", items["MNT"]["note"])
        # A value driven onto the bound passes within the default tolerance; a real shortfall fails.
        thinnest = items["MNT"]["value"]
        for limit, status in ((thinnest + 1e-15, "pass"), (thinnest + 1e-3, "fail")):
            item = aut._general_checks({"general_constraints": {"MNT": limit}}, lens)[0]
            self.assertEqual((item["status"], item["limit"]), (status, limit))
            self.assertEqual(item["tolerance"], 1e-6 * max(1.0, limit))

    def test_parent_timeout_discards_candidate_without_publishing(self):
        checkpoint = self.store.load_current(self.directory)

        class BaselineBackend:
            def __init__(self, **_kwargs):
                pass

            def open_lens(self, _path):
                pass

            def _current_checkpoint(self):
                return checkpoint

            def _require_session(self):
                return object()

            def close_session(self):
                pass

        class Process:
            def __init__(self, *_args, **_kwargs):
                self.killed = False

            def wait(self, timeout):
                if not self.killed:
                    raise subprocess.TimeoutExpired("aut", timeout)
                return 1

            def kill(self):
                self.killed = True

        baseline_fields = {"baseline_path": str(checkpoint.lens_path),
                           "baseline_sha256": checkpoint.lens_sha256,
                           "baseline_snapshot": checkpoint.snapshot.to_dict(),
                           "checkpoint_directory": str(checkpoint.lens_path.parent),
                           "backend_id": checkpoint.backend_id, "lens_id": checkpoint.lens_id,
                           "revision": checkpoint.revision}
        with (mock.patch.object(aut, "_run_stage", return_value=baseline_fields),
              mock.patch.object(aut, "_require_ordinary_surfaces"),
              mock.patch.object(aut.subprocess, "Popen", Process),
              mock.patch.object(aut, "_owned_cleanup", return_value=[]) as cleanup):
            with self.assertRaises(RuntimeError):
                aut.prepare(self.source, self.root / "timeout", surface=1,
                            parameter="radius", lower=25, upper=80, target=0,
                            cycles=2, wall_seconds=5)
        cleanup.assert_called_once()
        self.assertEqual(self.store.load_current(self.directory).revision, 0)
        report = json.loads((self.root / "timeout" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["state"], "failed")

    def test_an_interrupted_ramp_discards_the_candidate_without_publishing(self):
        checkpoint = self.store.load_current(self.directory)

        class InterruptedProcess:
            def __init__(self, *_args, **_kwargs):
                self.killed = False

            def wait(self, timeout):
                if not self.killed:
                    raise KeyboardInterrupt
                return 1

            def kill(self):
                self.killed = True

        baseline_fields = {"baseline_path": str(checkpoint.lens_path),
                           "baseline_sha256": checkpoint.lens_sha256,
                           "baseline_snapshot": checkpoint.snapshot.to_dict(),
                           "checkpoint_directory": str(checkpoint.lens_path.parent),
                           "backend_id": checkpoint.backend_id, "lens_id": checkpoint.lens_id,
                           "revision": checkpoint.revision}
        spec = {"schema_version": 1, "kind": "aut_spec", "name": "ramp", "wall_seconds": 60,
                "field_ramp": {"name": "grow", "steps": [{"fields": [{"field": 2, "y_angle": 8.0}]},
                                                         {"fields": [{"field": 2, "y_angle": 10.0}]}],
                               "stage": {"variables": [{"surface": 1, "parameter": "radius"}],
                                         "error_function": {"MXC": 2}}}}
        with (mock.patch.object(aut, "_run_stage", return_value=baseline_fields),
              mock.patch.object(aut.subprocess, "Popen", InterruptedProcess),
              mock.patch.object(aut, "_owned_cleanup", return_value=[]) as cleanup):
            with self.assertRaisesRegex(RuntimeError, "KeyboardInterrupt"):
                aut.prepare_spec(self.source, self.root / "ramp-interrupt", spec, spec_sha256="abc")
        cleanup.assert_called_once()
        self.assertEqual(self.store.load_current(self.directory).revision, 0)
        report = json.loads((self.root / "ramp-interrupt" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((report["state"], report["error"]), ("failed", "KeyboardInterrupt"))
        self.assertEqual([stage["name"] for stage in report["spec"]["stages"]], ["grow-1", "grow-2"])
        self.assertIn("field_ramp", report["spec_input"])

    def test_stage_timeout_kills_child_and_rechecks_owned_processes(self):
        class HungProcess:
            def __init__(self, *_args, **_kwargs):
                self.killed = False

            def wait(self, timeout):
                if not self.killed:
                    raise subprocess.TimeoutExpired("stage", timeout)
                return 1

            def kill(self):
                self.killed = True

        for phase, payload, label in (
                ("baseline", {"source": str(self.source), "surface": 1,
                              "parameter": "radius"}, "baseline-hang"),
                ("diagnostic", {"directory": "before-wavefront-engine",
                                "lens_path": str(self.candidate)}, "wavefront-hang"),
                ("verify", {"directory": "accept-engine",
                            "lens_path": str(self.candidate),
                            "expected": snapshot(35).to_dict()}, "accept-hang")):
            with (self.subTest(phase=phase),
                  mock.patch.object(aut.subprocess, "Popen", HungProcess),
                  mock.patch.object(aut, "_owned_cleanup", return_value=[]) as cleanup):
                with self.assertRaisesRegex(RuntimeError, "timed out") as raised:
                    aut._run_stage(self.root, phase, payload, seconds=1, label=label)
                self.assertEqual(raised.exception.cleanup_info["cleanup_remaining"], [])
                self.assertTrue(raised.exception.cleanup_info["cleanup_confirmed"])
                cleanup.assert_called_once()
        self.assertEqual(self.store.load_current(self.directory).revision, 0)

    def test_owned_cleanup_never_stops_the_shared_com_server_other_sessions_use(self):
        directory = self.root / "candidate-engine"
        directory.mkdir()
        identity = {"created_at": 100.0}
        (directory / "codev-mcp-session.json").write_text(json.dumps({"processes": {
            "10": {"name": "cvcomsvr.exe", **identity}, "12": {"name": "cvcommand.exe", **identity}}}),
            encoding="utf-8")
        live = {10: "cvcomsvr.exe", 12: "cvcommand.exe", 21: "codevm.exe", 22: "cvcommand.exe"}
        killed: list[list[int]] = []

        def kill(pids):
            killed.append(list(pids))
            for pid in pids:
                live.pop(pid, None)
            return []

        class Process:
            def __init__(self, _pid):
                pass

            def create_time(self):
                return 100.0

        from codev_mcp import com_session
        with (mock.patch.object(aut, "list_codev_processes", side_effect=lambda: dict(live)),
              mock.patch.object(com_session, "list_codev_processes", side_effect=lambda: dict(live)),
              mock.patch.object(aut, "terminate_processes", side_effect=kill),
              mock.patch.object(aut, "_matching_owned", side_effect=lambda pid, _identity: pid in live),
              mock.patch("psutil.Process", Process)):
            remaining = aut._owned_cleanup(directory, launched_after=99.0)
        self.assertEqual(killed, [[12]])
        self.assertEqual(remaining, [])  # the shared server is not left "in doubt"
        self.assertIn(10, live)

    def test_diagnostic_cleanup_refusal_is_failure(self):
        class Backend:
            def __init__(self, **_kwargs):
                pass

            def open_lens(self, _path):
                pass

            def run_analysis(self, _request):
                return SimpleNamespace(state=SimpleNamespace(value="succeeded"))

            def get_analysis(self):
                return SimpleNamespace(wavefront=SimpleNamespace(
                    model_dump=lambda **_kwargs: {"rms": 1.0}))

            def close_session(self):
                return SimpleNamespace(details={"cleanup_confirmed": False,
                                                "cleanup_remaining": [123]})

        with mock.patch.object(aut, "ComBackend", Backend):
            result = aut._diagnostic_wavefront(self.candidate, self.root / "diagnostic")
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["cleanup_unconfirmed"])

    def test_accept_verification_deadline_prevents_commit(self):
        with mock.patch.object(aut, "_run_stage", side_effect=RuntimeError("stage timed out")):
            with self.assertRaisesRegex(RuntimeError, "stage timed out"):
                aut.accept(self.result)
        self.assertEqual(self.store.load_current(self.directory).revision, 0)


if __name__ == "__main__":
    unittest.main()
