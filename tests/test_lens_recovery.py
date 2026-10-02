"""Checkpoint files, verification snapshots and engine recovery tests.

These cover the lens state recovery plan: a successful update is only reported
after it was read back, saved to its own checkpoint and verified; an engine exit
is followed by a restore of the last committed checkpoint; and a restore that
cannot be confirmed stops every lens operation instead of silently falling back
to the original lens file.

The fake session is the stand in for CODE V, so a "lost engine" is the watchdog
flag it exposes, exactly like the real session after a crash.
"""

from __future__ import annotations

import json
import shutil
import unittest
import unittest.mock
from pathlib import Path

from codev_mcp import checkpoints as cps
from codev_mcp import com_backend as backend_module
from codev_mcp.checkpoints import (
    CheckpointError,
    CheckpointVerificationError,
    LensCheckpointStore,
    LensSnapshot,
    SurfaceApertureState,
    SurfaceState,
    WavelengthState,
    ZoomState,
    change_expectations,
    expectation_keys,
    compare_snapshots,
    expand_allowed_changes,
    hash_file,
    read_snapshot,
)
from codev_mcp.com_backend import ComBackend
from codev_mcp.errors import (
    ComputationError,
    NotReadyError,
    ParameterError,
    SessionInvalidError,
)
from codev_mcp.models import (
    AnalysisKind,
    AnalysisOptions,
    AnalysisRequest,
    ParameterEdit,
    TaskState,
    UpdateRequest,
)
from tests import workspace_temp_directory
from tests.fake_codev import FakeCodeVSession


def thickness_edit(value: float, *, zoom: int | None = None, surface: int = 1) -> UpdateRequest:
    return UpdateRequest(
        edits=[ParameterEdit(surface=surface, parameter="thickness", value=value, zoom_position=zoom)]
    )


def fresh_session(**kwargs) -> FakeCodeVSession:
    session = FakeCodeVSession(**kwargs)
    session.listing = session._build_listing()
    return session



class BackendTestCase(unittest.TestCase):
    session_kwargs: dict = {}

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("recovery")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.working = self.root / "run"
        self.lens_path = self.root / "dbgauss.len"
        self.lens_path.write_text("! lens placeholder\n", encoding="utf-8")
        self.session = fresh_session(**self.session_kwargs)
        self.configure_session(self.session)
        self.backend = ComBackend(working_directory=self.working, session=self.session)
        # A test must never start a real CODE V engine: the shared setup already
        # has a session, and anything that would build another one is a bug.
        guard = unittest.mock.patch.object(
            self.backend,
            "_new_session",
            side_effect=AssertionError("a test tried to start a real CODE V engine"),
        )
        guard.start()
        self.addCleanup(guard.stop)
        self.backend.open_lens(str(self.lens_path))

    def configure_session(self, session: FakeCodeVSession) -> None:
        """Hook for subclasses that need a different session before opening."""

    # -------------------------------------------------------------- helpers

    def status(self) -> dict:
        return self.backend.get_status().details

    def lens_dir(self) -> Path:
        return Path(str(self.status()["checkpoint_directory"]))

    def checkpoint_path(self) -> Path:
        return Path(str(self.status()["checkpoint_path"]))

    def checkpoint_json(self, revision: int) -> dict:
        path = self.lens_dir() / f"revision-{revision:06d}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def crash_engine(self) -> None:
        """Kill the engine the way the watchdog notices it."""
        self.backend._session.engine_dead = True

    def install_replacement(self, session: FakeCodeVSession) -> None:
        patcher = unittest.mock.patch.object(self.backend, "_new_session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rebuild_with(self) -> FakeCodeVSession:
        """Serve every later engine start from a fresh fake session."""
        session = fresh_session()
        self.install_replacement(session)
        return session


class CheckpointFiles(unittest.TestCase):
    """The files behind a committed revision, without CODE V in the picture."""

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("store")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.store = LensCheckpointStore(self.root / "checkpoints", backend_id="backend-a")
        self.lens_id, self.directory = self.store.create_lens()
        self.snapshot = LensSnapshot(
            units="mm",
            dimension_code=2,
            stop_surface=1,
            surface_count=2,
            zoom_positions=1,
            reference_wavelength=1,
            zooms=[
                ZoomState(
                    position=1,
                    surfaces=[SurfaceState(number=1, radius=10.0, thickness=5.0, glass="N-BK7")],
                )
            ],
        )

    def write_lens(self, name: str, text: str = "lens bytes") -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_publish_writes_metadata_and_a_pointer(self):
        lens = self.write_lens("revision-000000.len")
        checkpoint = self.store.publish(
            self.directory, self.lens_id, 0, lens, self.snapshot, source_path="C:/lens.len"
        )
        self.assertEqual(checkpoint.revision, 0)
        self.assertTrue(checkpoint.metadata_path.exists())
        pointer = json.loads((self.directory / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["revision"], 0)
        self.assertEqual(pointer["lens_file"], "revision-000000.len")
        loaded = self.store.load_current(self.directory)
        self.assertEqual(loaded.lens_sha256, hash_file(lens))
        self.assertEqual(loaded.snapshot.units, "mm")
        self.assertEqual(loaded.snapshot.zooms[0].surfaces[0].glass, "N-BK7")
        self.assertEqual(loaded.source_path, "C:/lens.len")

    def test_publishing_replaces_only_the_pointer(self):
        first = self.write_lens("revision-000000.len", "one")
        self.store.publish(self.directory, self.lens_id, 0, first, self.snapshot, source_path=None)
        second = self.write_lens("revision-000001.len", "two")
        self.store.publish(self.directory, self.lens_id, 1, second, self.snapshot, source_path=None)
        self.assertTrue(first.exists())
        self.assertEqual(first.read_text(encoding="utf-8"), "one")
        self.assertEqual(self.store.load_current(self.directory).revision, 1)
        self.assertEqual(self.store.load_revision(self.directory, 0).revision, 0)

    def test_the_pointer_is_never_left_half_written(self):
        lens = self.write_lens("revision-000000.len")
        self.store.publish(self.directory, self.lens_id, 0, lens, self.snapshot, source_path=None)
        leftovers = list(self.directory.glob(".current.json.*.tmp"))
        self.assertFalse(leftovers)
        payload = json.loads((self.directory / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["revision"], 0)

    def test_a_missing_pointer_is_a_checkpoint_error(self):
        with self.assertRaises(CheckpointError):
            self.store.load_current(self.directory)

    def test_a_corrupted_pointer_is_a_checkpoint_error(self):
        (self.directory / "current.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.store.load_current(self.directory)

    def test_an_unknown_format_version_is_refused(self):
        lens = self.write_lens("revision-000000.len")
        self.store.publish(self.directory, self.lens_id, 0, lens, self.snapshot, source_path=None)
        path = self.directory / "current.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["format_version"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.store.load_current(self.directory)
        self.assertTrue(lens.exists())

    def test_an_old_format_checkpoint_is_refused_without_rewriting_it(self):
        lens = self.write_lens("revision-000000.len")
        self.store.publish(self.directory, self.lens_id, 0, lens, self.snapshot, source_path=None)
        path = self.directory / "current.json"
        for version in (1, 2, 3):
            with self.subTest(format_version=version):
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["format_version"] = version
                original = json.dumps(payload)
                path.write_text(original, encoding="utf-8")
                with self.assertRaises(CheckpointError) as caught:
                    self.store.load_current(self.directory)
                self.assertIn("Reopen the source lens", caught.exception.hint)
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                self.assertTrue(lens.exists())

    def test_a_missing_or_empty_lens_file_is_refused(self):
        with self.assertRaises(CheckpointError):
            self.store.publish(
                self.directory, self.lens_id, 0, self.directory / "absent.len", self.snapshot,
                source_path=None,
            )
        empty = self.write_lens("revision-000000.len", "")
        with self.assertRaises(CheckpointError):
            self.store.publish(self.directory, self.lens_id, 0, empty, self.snapshot, source_path=None)

    def test_a_changed_lens_file_is_caught_by_its_hash(self):
        lens = self.write_lens("revision-000000.len", "one")
        self.store.publish(self.directory, self.lens_id, 0, lens, self.snapshot, source_path=None)
        lens.write_text("two", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.store.load_current(self.directory)

    def test_metadata_cannot_point_outside_its_directory(self):
        lens = self.write_lens("revision-000000.len")
        self.store.publish(self.directory, self.lens_id, 0, lens, self.snapshot, source_path=None)
        path = self.directory / "current.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["lens_file"] = "../elsewhere.len"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.store.load_current(self.directory)

    def test_two_backends_and_two_lenses_do_not_collide(self):
        other_store = LensCheckpointStore(self.root / "checkpoints", backend_id="backend-b")
        other_id, other_dir = other_store.create_lens()
        self.assertNotEqual(self.directory, other_dir)
        mine = self.write_lens("revision-000000.len", "mine")
        theirs = other_dir / "revision-000000.len"
        theirs.write_text("theirs", encoding="utf-8")
        self.store.publish(self.directory, self.lens_id, 0, mine, self.snapshot, source_path=None)
        other_store.publish(other_dir, other_id, 0, theirs, self.snapshot, source_path=None)
        self.assertEqual(self.store.load_current(self.directory).lens_path, mine)
        self.assertEqual(other_store.load_current(other_dir).lens_path, theirs)
        second_id, second_dir = self.store.create_lens()
        self.assertNotEqual(second_id, self.lens_id)
        self.assertNotEqual(second_dir, self.directory)

    def test_transaction_and_restore_point_names_are_unique(self):
        first = self.store.restore_point_path(self.directory, "rp-0001")
        second = self.store.restore_point_path(self.directory, "rp-0001", unique="tx-000001")
        self.assertNotEqual(first, second)
        record = self.store.write_transaction(self.directory, "tx-000001", {"state": "started"})
        self.assertTrue(record.exists())
        written = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(written["transaction_id"], "tx-000001")
        self.assertEqual(written["state"], "started")


class SnapshotComparison(unittest.TestCase):
    """The verification snapshot is what makes "unchanged" provable."""

    def snapshot(self, *, thickness: float = 5.0, glass: str = "N-BK7", micrometer: float = 0.5876):
        return LensSnapshot(
            units="mm",
            dimension_code=2,
            stop_surface=1,
            surface_count=2,
            zoom_positions=1,
            reference_wavelength=1,
            aperture_kind="epd",
            aperture_value=50.0,
            zooms=[
                ZoomState(
                    position=1,
                    surfaces=[
                        SurfaceState(number=1, radius=10.0, thickness=thickness, glass=glass)
                    ],
                )
            ],
            wavelengths=[WavelengthState(number=1, micrometers=micrometer, weight=1.0)],
        )

    def test_identical_snapshots_have_no_differences(self):
        self.assertEqual(compare_snapshots(self.snapshot(), self.snapshot()), [])

    def test_native_relation_changes_are_reported_even_when_values_match(self):
        before = self.snapshot()
        before.relation_data_complete = True
        before.solves = ["PIM"]
        before.pickups = ["PIK RDY S1 Z1 RDY S2 Z1 1.000000 4.000000"]
        after = self.snapshot()
        after.relation_data_complete = True
        after.solves = ["PIM"]
        problems = compare_snapshots(before, after)
        self.assertEqual([item["where"] for item in problems], ["native pickups"])
        after.pickups = list(before.pickups)
        after.solves = []
        self.assertEqual(
            [item["where"] for item in compare_snapshots(before, after)],
            ["native solves"],
        )


    def test_fields_and_wavelengths_that_appeared_are_reported(self):
        """The comparison walked the reference only, so an extra field passed (review M7)."""
        from codev_mcp.checkpoints import FieldState

        before = self.snapshot()
        before.zooms[0].fields = [FieldState(number=1, x_angle=0.0, y_angle=0.0, weight=1.0)]
        after = self.snapshot()
        after.zooms[0].fields = [FieldState(number=1, x_angle=0.0, y_angle=0.0, weight=1.0),
                                 FieldState(number=2, x_angle=0.0, y_angle=7.0, weight=1.0)]
        after.wavelengths.append(WavelengthState(number=2, micrometers=0.4861, weight=1.0))
        problems = compare_snapshots(before, after)
        self.assertEqual([item["where"] for item in problems], ["field 2 zoom 1", "wavelength 2"])
        self.assertEqual({item["detail"] for item in problems}, {"unexpected"})
        reverse = [item["where"] for item in compare_snapshots(after, before)]
        self.assertIn("wavelength 2", reverse)
        self.assertTrue(any(where.startswith("field 2 zoom 1") for where in reverse))

    def test_a_round_trip_difference_is_not_reported(self):
        self.assertEqual(
            compare_snapshots(self.snapshot(thickness=5.0), self.snapshot(thickness=5.0000000000000004)),
            [],
        )

    def test_a_requested_change_is_allowed_only_at_the_requested_value(self):
        before = self.snapshot()
        after = self.snapshot(thickness=9.5)
        key = ("surface", 1, 1, "thickness")
        self.assertEqual(
            compare_snapshots(
                before, after, allowed_changes=[key], expected_values=change_expectations(after, [key])
            ),
            [],
        )
        wrong = self.snapshot(thickness=99.0)
        problems = compare_snapshots(
            before, wrong, allowed_changes=[key], expected_values=change_expectations(after, [key])
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("thickness", problems[0]["where"])

    def test_a_second_change_is_always_reported(self):
        before = self.snapshot()
        after = self.snapshot(thickness=9.5, glass="SK16")
        key = ("surface", 1, 1, "thickness")
        problems = compare_snapshots(
            before, after, allowed_changes=[key], expected_values=change_expectations(after, [key])
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("glass", problems[0]["where"])

    def test_a_glass_difference_keeps_the_catalog(self):
        before = self.snapshot(glass="N-BK7_SCHOTT")
        after = self.snapshot(glass="N-BK7_OHARA")
        problems = compare_snapshots(before, after)
        self.assertEqual(len(problems), 1)
        self.assertIn("glass", problems[0]["where"])

    def test_a_missing_wavelength_is_reported(self):
        before = self.snapshot()
        after = self.snapshot()
        after.wavelengths = []
        problems = compare_snapshots(before, after)
        self.assertTrue(any("wavelength 1" in problem["where"] for problem in problems))

    def test_existing_system_aperture_value_is_an_exact_allowed_change(self):
        before = self.snapshot()
        before.aperture_values = {"1": 50.0}
        after = self.snapshot()
        after.aperture_value = 45.0
        after.aperture_values = {"1": 45.0}
        key = ("aperture", 0, 1, "value")
        expected = change_expectations(after, [key])
        self.assertEqual(
            compare_snapshots(
                before, after, allowed_changes=[key], expected_values=expected
            ),
            [],
        )
        after.aperture_commands.append("ADX S1 0")
        self.assertTrue(any(
            problem["where"] == "native surface aperture commands"
            for problem in compare_snapshots(before, after, allowed_changes=[key], expected_values=expected)
        ))
        after.aperture_commands.pop()
        after.zoom_aperture_commands.append("CIR S1 10 20 30")
        self.assertTrue(any(
            problem["where"] == "native zoom aperture commands"
            for problem in compare_snapshots(before, after, allowed_changes=[key], expected_values=expected)
        ))
        wrong_kind = self.snapshot()
        wrong_kind.aperture_kind = "fno"
        wrong_kind.aperture_value = 45.0
        wrong_kind.aperture_values = {"1": 45.0}
        problems = compare_snapshots(
            before, wrong_kind, allowed_changes=[key], expected_values=expected
        )
        self.assertTrue(any(problem["where"] == "aperture type" for problem in problems))

    def test_only_the_radius_of_one_safe_clear_aperture_may_change(self):
        def aperture_snapshot(radius: float, *, obscuration: float | None = None) -> LensSnapshot:
            items = [
                SurfaceApertureState(
                    kind="clear",
                    shape="circular",
                    radius=radius,
                    x_semi_aperture=radius,
                    y_semi_aperture=radius,
                )
            ]
            if obscuration is not None:
                items.append(
                    SurfaceApertureState(
                        kind="obscuration",
                        shape="circular",
                        radius=obscuration,
                        x_semi_aperture=obscuration,
                        y_semi_aperture=obscuration,
                    )
                )
            return LensSnapshot(
                units="mm",
                aperture_kind="epd",
                aperture_usage="user_and_default",
                aperture_value=50.0,
                aperture_values={"1": 50.0},
                aperture_commands=[f"CIR S1 CLR {radius}"] + (
                    [] if obscuration is None else [f"CIR S1 OBS {obscuration}"]
                ),
                zoom_positions=1,
                zooms=[
                    ZoomState(
                        position=1,
                        surfaces=[SurfaceState(number=1, apertures=items)],
                    )
                ],
            )

        before = aperture_snapshot(15.0)
        after = aperture_snapshot(14.0)
        key = ("surface", 1, 1, "clear_aperture_radius")
        expected = change_expectations(after, [key])
        self.assertEqual(
            compare_snapshots(
                before, after, allowed_changes=[key], expected_values=expected
            ),
            [],
        )
        complex_after = aperture_snapshot(14.0, obscuration=2.0)
        problems = compare_snapshots(
            before, complex_after, allowed_changes=[key], expected_values=expected
        )
        self.assertTrue(any("explicit apertures" in problem["where"] for problem in problems))

    def test_a_shared_parameter_change_covers_every_zoom_position(self):
        def two_zoom(thickness: float) -> LensSnapshot:
            return LensSnapshot(
                units="mm",
                zoom_positions=2,
                zooms=[
                    ZoomState(position=1, surfaces=[SurfaceState(number=1, thickness=thickness)]),
                    ZoomState(position=2, surfaces=[SurfaceState(number=1, thickness=thickness)]),
                ],
            )

        before = two_zoom(5.0)
        after = two_zoom(7.0)
        requested = [("surface", 1, 2, "thickness")]
        expanded = expand_allowed_changes(before, requested)
        self.assertEqual(sorted(entry[2] for entry in expanded), [1, 2])
        problems = compare_snapshots(
            before,
            after,
            allowed_changes=expanded,
            expected_values=change_expectations(after, expanded + expectation_keys(after, expanded)),
        )
        self.assertEqual(problems, [])

    def test_a_zoomed_parameter_only_changes_where_it_was_asked(self):
        def zoomed(first: float, second: float) -> LensSnapshot:
            return LensSnapshot(
                units="mm",
                zoom_positions=2,
                zooms=[
                    ZoomState(position=1, surfaces=[SurfaceState(number=1, thickness=first)]),
                    ZoomState(position=2, surfaces=[SurfaceState(number=1, thickness=second)]),
                ],
            )

        before = zoomed(5.0, 6.0)
        after = zoomed(5.0, 8.0)
        requested = [("surface", 1, 2, "thickness")]
        expanded = expand_allowed_changes(before, requested)
        self.assertEqual(expanded, requested)
        problems = compare_snapshots(
            before,
            after,
            allowed_changes=expanded,
            expected_values=change_expectations(after, expanded + expectation_keys(after, expanded)),
        )
        self.assertEqual(problems, [])

    def test_a_radius_request_admits_the_plane_to_curved_flag_change_only(self):
        def lens(radius, flat):
            return LensSnapshot(units="mm", zoom_positions=1, zooms=[ZoomState(position=1, surfaces=[
                SurfaceState(number=1, radius=radius, radius_infinite=flat, thickness=5.0)])])

        before, after = lens(None, True), lens(80.0, False)
        for parameter, admitted in (("radius", True), ("thickness", False)):
            requested = cps.with_radius_flags([("surface", 1, 1, parameter)])
            problems = compare_snapshots(before, after, allowed_changes=requested,
                                         expected_values=change_expectations(
                                             after, requested + expectation_keys(after, requested)))
            with self.subTest(parameter=parameter):
                self.assertEqual(problems == [], admitted, problems)
        self.assertEqual(cps.with_radius_flags([("surface", 1, 1, "thickness")]), [("surface", 1, 1, "thickness")])
        self.assertEqual(cps.with_radius_flags([("field", 1, 1, "radius")]), [("field", 1, 1, "radius")])


class OpenLensCheckpoint(BackendTestCase):
    def test_opening_a_lens_publishes_revision_zero(self):
        details = self.status()
        self.assertEqual(details["lens_state"], "ready")
        self.assertEqual(details["committed_revision"], 0)
        self.assertTrue(details["lens_id"])
        self.assertTrue(self.checkpoint_path().exists())
        self.assertEqual(self.checkpoint_path().name, "revision-000000.len")
        self.assertEqual(details["recovery_count"], 0)

    def test_the_checkpoint_records_the_source_file_and_a_hash(self):
        payload = self.checkpoint_json(0)
        self.assertEqual(payload["format_version"], cps.CHECKPOINT_FORMAT_VERSION)
        self.assertEqual(payload["source_path"], str(self.lens_path))
        self.assertEqual(payload["lens_sha256"], hash_file(self.checkpoint_path()))
        self.assertEqual(payload["lens_size"], self.checkpoint_path().stat().st_size)
        self.assertTrue(payload["snapshot"]["zooms"])
        self.assertEqual(payload["snapshot"]["aperture_kind"], "epd")
        self.assertEqual(payload["snapshot"]["aperture_values"], {"1": 50.0})
        self.assertIn("apertures", payload["snapshot"]["zooms"][0]["surfaces"][0])
        self.assertTrue(payload["snapshot"]["relation_data_complete"])
        self.assertEqual(payload["snapshot"]["solves"], [])
        self.assertEqual(payload["snapshot"]["pickups"], [])

    def test_incomplete_relation_listing_cannot_be_snapshotted(self):
        original = self.session.command

        def incomplete(text: str) -> str:
            output = original(text)
            return output.replace("No pickups defined in system", "") if text == "lis" else output

        self.session.command = incomplete
        self.addCleanup(lambda: setattr(self.session, "command", original))
        with self.assertRaises(CheckpointError):
            read_snapshot(self.session, self.backend.get_lens())

    def test_missing_variable_control_columns_cannot_be_snapshotted(self):
        original = self.session.command

        def incomplete(text: str) -> str:
            output = original(text)
            return output.replace("           CCY   THC   GLC", "") if text == "lis" else output

        self.session.command = incomplete
        self.addCleanup(lambda: setattr(self.session, "command", original))
        with self.assertRaises(CheckpointError):
            read_snapshot(self.session, self.backend.get_lens())

    def test_the_source_lens_is_not_the_working_copy(self):
        self.assertNotEqual(self.checkpoint_path(), self.lens_path)
        self.assertEqual(self.lens_path.read_text(encoding="utf-8"), "! lens placeholder\n")

    def test_two_lenses_use_separate_directories(self):
        first = self.lens_dir()
        other = self.root / "second.len"
        other.write_text("! second\n", encoding="utf-8")
        self.backend.open_lens(str(other))
        second = self.lens_dir()
        self.assertNotEqual(first, second)
        self.assertTrue((first / "current.json").exists())
        self.assertTrue((second / "current.json").exists())
        self.assertEqual(self.status()["committed_revision"], 0)

    def test_a_failed_open_restores_the_previous_lens(self):
        original_title = self.backend.get_lens().title
        previous_id = self.status()["lens_id"]
        broken = self.root / "broken.len"
        broken.write_text("! broken\n", encoding="utf-8")
        opening = {"active": False}
        original = self.backend._save_lens_file

        def failing_save(session, path, action):
            if opening["active"]:
                raise RuntimeError("save failed")
            return original(session, path, action)

        self.backend._save_lens_file = failing_save
        self.addCleanup(lambda: setattr(self.backend, "_save_lens_file", original))
        opening["active"] = True
        with self.assertRaises(SessionInvalidError):
            self.backend.open_lens(str(broken))
        opening["active"] = False
        self.assertEqual(self.status()["lens_id"], previous_id)
        self.assertEqual(self.status()["lens_state"], "ready")
        self.assertEqual(self.backend.get_lens().title, original_title)


class FirstOpenFails(unittest.TestCase):
    """A first open that cannot be published leaves nothing to trust."""

    def setUp(self) -> None:
        self._temp = workspace_temp_directory("open-fail")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.working = self.root / "run"
        self.lens_path = self.root / "dbgauss.len"
        self.lens_path.write_text("! placeholder\n", encoding="utf-8")
        self.session = fresh_session()
        self.backend = ComBackend(working_directory=self.working, session=self.session)

    def test_a_first_open_that_cannot_be_published_returns_to_an_empty_session(self):
        original = self.backend._save_lens_file

        def failing_save(session, path, action):
            raise RuntimeError("the engine refused to save the lens")

        self.backend._save_lens_file = failing_save
        with self.assertRaises(ComputationError) as caught:
            self.backend.open_lens(str(self.lens_path))
        self.assertEqual(caught.exception.details["lens_state"], "empty")
        # Nothing established a checkpoint, so the engine that may hold a partly
        # loaded lens is dropped; nothing was open, so the service is empty again
        # instead of refusing every later call (review M8).
        self.assertEqual(self.backend._lens_state.name, "EMPTY")
        self.assertIsNone(self.backend._committed_revision)
        self.assertIsNone(self.backend._session)
        self.assertFalse(self.session.started)
        self.assertEqual(self.backend.get_status().details["lens_state"], "empty")
        replacement = fresh_session()
        self.backend._session_factory = lambda: replacement
        self.backend._save_lens_file = original
        self.assertTrue(self.backend.open_lens(str(self.lens_path)).surfaces)
        self.assertEqual(self.backend.get_status().details["lens_state"], "ready")

    def test_an_unconfirmed_release_of_the_dropped_session_still_invalidates(self):
        def failing_save(session, path, action):
            raise RuntimeError("the engine refused to save the lens")

        self.backend._save_lens_file = failing_save
        self.session.stop = lambda: False
        with self.assertRaises(SessionInvalidError):
            self.backend.open_lens(str(self.lens_path))
        self.assertEqual(self.backend.get_status().details["lens_state"], "invalid")

class InterruptedBatchReport(BackendTestCase):
    def test_the_report_of_an_interrupted_batch_matches_the_recovery(self):
        """The warning said writes stay refused, yet the next call restores and accepts them (review L3)."""
        def interrupt(name: str) -> None:
            if name == "before_edit_command":
                raise RuntimeError("injected interruption")

        self.backend.fault_hook = interrupt
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertFalse(result.session_valid)
        self.assertTrue(any("restores the lens" in warning for warning in result.warnings))
        self.assertFalse(any("restart the service" in warning for warning in result.warnings))
        self.backend.fault_hook = None
        retried = self.backend.update_lens(thickness_edit(9.5))
        self.assertTrue(retried.outcomes[0].applied)


class CommittedUpdate(BackendTestCase):
    def test_a_successful_update_raises_the_committed_revision(self):
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertTrue(result.outcomes[0].applied)
        self.assertFalse(result.rolled_back)
        details = self.status()
        self.assertEqual(details["committed_revision"], 1)
        self.assertEqual(Path(str(details["checkpoint_path"])).name, "revision-000001.len")
        self.assertEqual(self.checkpoint_json(1)["revision"], 1)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 9.5)

    def test_two_batches_keep_both_revisions(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.backend.update_lens(thickness_edit(11.25))
        details = self.status()
        self.assertEqual(details["committed_revision"], 2)
        first = self.lens_dir() / "revision-000001.len"
        second = self.lens_dir() / "revision-000002.len"
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertNotEqual(hash_file(first), hash_file(second))
        self.assertEqual(self.checkpoint_json(2)["revision"], 2)
        # The pointer, not a timestamp, decides which revision is current.
        pointer = json.loads((self.lens_dir() / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["revision"], 2)
        self.assertEqual(pointer["lens_file"], "revision-000002.len")

    def test_a_glass_edit_keeps_the_catalog_in_the_checkpoint(self):
        result = self.backend.update_lens(
            UpdateRequest(edits=[ParameterEdit(surface=1, parameter="glass", value="SK16")])
        )
        self.assertTrue(result.outcomes[0].applied)
        lens = self.backend.get_lens()
        self.assertEqual(lens.surfaces[1].glass, "SK16_OHARA")
        snapshot = self.checkpoint_json(1)["snapshot"]
        glass = [row["glass"] for row in snapshot["zooms"][0]["surfaces"] if row["number"] == 1][0]
        self.assertEqual(glass, "SK16_OHARA")

    def test_a_solve_that_re_derives_another_parameter_does_not_fail_the_batch(self):
        original_set = self.session._set_surface

        def solved_set(item, number, zoom, value):
            outcome = original_set(item, number, zoom, value)
            if item == "THI" and number == 1:
                # The image distance is solved, so CODE V re-derives it when the
                # spacing in front of it changes.
                self.session.surfaces[3]["thickness"] = 63.0 - (value - 9.12345599999)
            return outcome

        self.session.solves = {3: "MAR"}
        self.session._set_surface = solved_set
        self.addCleanup(lambda: setattr(self.session, "_set_surface", original_set))
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertTrue(result.outcomes[0].applied, result.warnings)
        self.assertFalse(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertTrue(
            any("re-derived the solved parameters" in warning for warning in result.warnings),
            result.warnings,
        )
        self.assertAlmostEqual(self.backend.get_lens().surfaces[3].thickness, 63.0 - 0.37654400001)

    def test_a_change_without_a_solve_is_still_refused(self):
        original_set = self.session._set_surface

        def sneaky_set(item, number, zoom, value):
            outcome = original_set(item, number, zoom, value)
            if item == "THI" and number == 1:
                self.session.surfaces[3]["thickness"] = 42.0
            return outcome

        self.session._set_surface = sneaky_set
        self.addCleanup(lambda: setattr(self.session, "_set_surface", original_set))
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 0)
        self.assertTrue(
            any("changed more than the requested" in warning for warning in result.warnings),
            result.warnings,
        )
    def test_the_update_records_its_transaction(self):
        self.backend.update_lens(thickness_edit(9.5))
        records = sorted((self.lens_dir() / "transactions").glob("tx-*.json"))
        self.assertTrue(records)
        payload = json.loads(records[-1].read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "committed")
        self.assertEqual(payload["to_revision"], 1)
        self.assertEqual(payload["lens_id"], self.status()["lens_id"])
        self.assertEqual(payload["edits"][0]["parameter"], "thickness")

    def test_a_restore_point_file_is_kept_per_transaction(self):
        self.backend.update_lens(thickness_edit(9.5))
        points = sorted((self.lens_dir() / "restore-points").glob("rp-*.len"))
        self.assertTrue(points)
        self.assertTrue(all(point.stat().st_size > 0 for point in points))


class FailedUpdate(BackendTestCase):
    def test_a_silently_ignored_edit_does_not_publish_a_revision(self):
        self.session.pickups = {1: "PIC"}
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertFalse(result.outcomes[0].applied)
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 0)
        self.assertFalse((self.lens_dir() / "revision-000001.len").exists())
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 9.12345599999)

    def test_a_failed_batch_after_a_committed_one_returns_to_the_first(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.session.pickups = {2: "PIC"}
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(surface=1, parameter="thickness", value=15.0),
                    ParameterEdit(surface=2, parameter="thickness", value=2.5),
                ]
            )
        )
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertTrue(result.rolled_back)
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.surfaces[1].thickness, 9.5)
        self.assertAlmostEqual(lens.surfaces[2].thickness, 4.25)
        prepared = {
            (outcome.edit.surface, outcome.edit.parameter): outcome.previous_value
            for outcome in result.outcomes
        }
        self.assertAlmostEqual(prepared[(2, "thickness")], 4.25)

    def test_a_rollback_check_that_reads_a_third_value_marks_the_session_invalid(self):
        original_read = read_snapshot
        calls = {"count": 0}

        def tampered(session, lens, listing=None):
            snapshot = original_read(session, lens, listing)
            calls["count"] += 1
            if calls["count"] == 1:
                # The rollback is compared against the state read before the
                # batch, so a third, wrong value must be caught here.
                for surface in snapshot.zooms[0].surfaces:
                    if surface.number == 1:
                        surface.thickness = 123.0
            return snapshot

        self.session.pickups = {1: "PIC"}
        with unittest.mock.patch.object(backend_module, "read_snapshot", tampered):
            result = self.backend.update_lens(thickness_edit(9.5))
        self.assertFalse(result.rolled_back)
        self.assertFalse(result.session_valid)
        self.assertEqual(self.status()["lens_state"], "invalid")
        with self.assertRaises(SessionInvalidError):
            self.backend.update_lens(thickness_edit(10.0))

    def test_an_unreadable_state_after_editing_rolls_back(self):
        original = self.session.evaluate
        calls = {"count": 0}

        def failing(item):
            if item.startswith("(RDY S3"):
                calls["count"] += 1
                if calls["count"] > 1:
                    raise SessionInvalidError("read failed")
            return original(item)

        self.session.evaluate = failing
        self.addCleanup(lambda: setattr(self.session, "evaluate", original))
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertFalse(result.rolled_back)
        self.assertFalse(result.session_valid)
        self.assertEqual(self.status()["lens_state"], "invalid")


class RebuiltEngine(BackendTestCase):
    """An engine exit restores the last committed lens, never the source."""

    def test_recovery_restores_the_second_batch_not_the_first(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.backend.update_lens(thickness_edit(11.25))
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True

        lens = self.backend.get_lens()

        self.assertAlmostEqual(lens.surfaces[1].thickness, 11.25)
        details = self.status()
        self.assertEqual(details["recovery_count"], 1)
        self.assertEqual(details["committed_revision"], 2)
        self.assertEqual(details["lens_state"], "ready")
        self.assertEqual(details["last_recovery"]["result"], "succeeded")
        self.assertEqual(details["last_recovery"]["revision"], 2)
        self.assertIn("revision-000002.len", str(details["checkpoint_path"]))
        self.assertTrue(self.checkpoint_path().exists())

    def test_recovery_refuses_a_lost_pickup_with_unchanged_numeric_values(self):
        replacement = fresh_session()
        original = replacement.command

        def changed_listing(text: str) -> str:
            output = original(text)
            if text == "lis":
                return output.replace(
                    "No pickups defined in system",
                    "PICKUPS\r\n PIK RDY S1 Z1 RDY S2 Z1 1.000000 4.000000",
                )
            return output

        replacement.command = changed_listing
        self.install_replacement(replacement)
        self.session.engine_dead = True
        with self.assertRaises(CheckpointVerificationError):
            self.backend.get_lens()
        self.assertEqual(self.status()["lens_state"], "invalid")

    def test_recovery_does_not_read_the_source_file(self):
        self.backend.update_lens(thickness_edit(9.5))
        # The user may have deleted or rewritten the original lens; the
        # checkpoint is what the service committed to.
        self.lens_path.unlink()
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.surfaces[1].thickness, 9.5)
        self.assertFalse(
            any(str(self.lens_path) in command for command in replacement.commands),
            f"the source file was read: {replacement.commands}",
        )

    def test_recovery_keeps_the_reported_source_path(self):
        self.backend.update_lens(thickness_edit(9.5))
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        self.backend.get_lens()
        self.assertEqual(self.status()["source_path"], str(self.lens_path))
        self.assertEqual(self.checkpoint_json(1)["source_path"], str(self.lens_path))

    def test_a_corrupted_checkpoint_is_marked_invalid_not_rolled_back(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.checkpoint_path().write_text("corrupted", encoding="utf-8")
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()
        self.assertEqual(self.backend._lens_state.name, "INVALID")
        # The corrupt checkpoint is never replaced by the source file or by an
        # older revision.
        self.assertFalse(any("res " in command and "dbgauss" in command for command in replacement.commands))

    def test_a_deleted_checkpoint_marks_the_session_invalid(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.checkpoint_path().unlink()
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        with self.assertRaises(CheckpointError):
            self.backend.get_lens()
        self.assertEqual(self.status()["lens_state"], "invalid")

    def test_status_queries_never_bypass_the_invalid_state(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.checkpoint_path().write_text("corrupted", encoding="utf-8")
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()
        for _ in range(3):
            status = self.backend.get_status()
            self.assertEqual(status.details["lens_state"], "invalid")
            self.assertFalse(status.lens_open)
        self.assertEqual(status.details["recovery_count"], 0)
        with self.assertRaises(SessionInvalidError):
            self.backend.update_lens(thickness_edit(12.0))
        with self.assertRaises(SessionInvalidError):
            self.backend.save_lens_as(str(self.root / "refused.len"))
        with self.assertRaises(SessionInvalidError):
            self.backend.run_analysis(
                AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
            )
        self.assertFalse((self.root / "refused.len").exists())

    def test_an_invalid_session_is_not_cleared_by_a_rebuild(self):
        # Fake wait returns immediately; bound the confirmation loop to avoid
        # retaining millions of call events while testing the same refusal.
        self.backend.cancel_confirm_seconds = 0.01
        self.session.stop_takes_effect = False
        self.session.async_ticks = 5
        self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions())
        )
        self.backend.cancel_analysis()
        self.assertEqual(self.backend._lens_state.name, "INVALID")
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        for _ in range(2):
            # A rebuild must not clear the invalid state; the refusal names the
            # running task first, then the invalid session.
            with self.assertRaises((SessionInvalidError, ParameterError)):
                self.backend.get_lens()
            with self.assertRaises(SessionInvalidError):
                self.backend.save_lens_as(str(self.root / "refused.len"))
            self.assertEqual(self.backend._lens_state.name, "INVALID")

    def test_an_interrupted_analysis_keeps_its_failure_record(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.SPOT_DIAGRAM, options=AnalysisOptions(ray_grid=3))
        )
        self.assertEqual(task.state, TaskState.RUNNING)
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        # The lens is still refused while the interrupted task is recorded, so
        # the engine exit is noticed by a call that does not need the lens.
        self.backend.get_analysis()
        self.assertEqual(self.backend._task.state, TaskState.FAILED)
        # The lens is restored on the next call; the failure record stays.
        self.backend.get_lens()
        snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.task_id, task.task_id)
        self.assertEqual(snapshot.task.state, TaskState.FAILED)
        self.assertEqual(snapshot.task.error.kind.value, "session_invalid")
        self.assertTrue(snapshot.task.error.details["interrupted"])
        self.assertIn("engine exited", snapshot.task.error.message)

    def test_analysis_results_do_not_survive_a_lens_change(self):
        first = self.backend.update_lens(thickness_edit(9.5))
        self.assertTrue(first.outcomes[0].applied, first.warnings)
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertEqual(self.status()["analysis_revision"], 1)
        self.assertIsNotNone(self.backend.get_analysis().first_order)
        second = self.backend.update_lens(thickness_edit(11.25))
        self.assertTrue(second.outcomes[0].applied, second.warnings)
        self.assertEqual(self.status()["committed_revision"], 2)
        snapshot = self.backend.get_analysis()
        # The result of the older revision is kept as history, but it is never
        # presented as the analysis of the lens that is open now.
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(snapshot.first_order)
        self.assertTrue(
            any("history only" in warning for warning in snapshot.task.warnings),
            snapshot.task.warnings,
        )
        # The results are real CODE V results, so the source stays codev; the
        # task says that they belong to an earlier revision.
        self.assertEqual(snapshot.source.value, "codev")
        self.assertTrue(snapshot.task.history_only)
        self.assertEqual(self.status()["analysis_revision"], 1)
        # A fresh analysis of the open lens is reported as a real result again.
        rerun = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(rerun.state, TaskState.SUCCEEDED)
        fresh = self.backend.get_analysis()
        self.assertEqual(fresh.source.value, "codev")
        self.assertFalse(fresh.task.history_only)
        self.assertFalse(fresh.task.warnings)

class EngineExitDuringUpdate(BackendTestCase):
    """The engine can die between the edits and the checkpoint commit."""

    def test_a_commit_that_cannot_be_published_keeps_the_previous_revision(self):
        self.backend.update_lens(thickness_edit(9.5))
        first = self.checkpoint_path()
        original = self.backend._save_lens_file
        saves = {"count": 0}

        def failing_save(session, path, action):
            saves["count"] += 1
            if saves["count"] == 1:
                # The restore point of the second batch is still written; the
                # checkpoint candidate that follows is what fails.
                return original(session, path, action)
            if "restore-points" not in str(path):
                raise RuntimeError("the engine refused to save the checkpoint")
            return original(session, path, action)

        self.backend._save_lens_file = failing_save
        self.addCleanup(lambda: setattr(self.backend, "_save_lens_file", original))
        result = self.backend.update_lens(thickness_edit(11.25))
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertEqual(self.checkpoint_path(), first)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 9.5)
        self.assertFalse((self.lens_dir() / "revision-000002.json").exists())

    def test_a_checkpoint_pointer_that_cannot_be_replaced_keeps_the_previous_one(self):
        self.backend.update_lens(thickness_edit(9.5))
        original = cps.write_json_atomic

        def failing_pointer(path, payload):
            if Path(path).name == "current.json":
                raise OSError("disk full")
            return original(path, payload)

        with unittest.mock.patch.object(cps, "write_json_atomic", failing_pointer):
            result = self.backend.update_lens(thickness_edit(11.25))
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 9.5)
        self.assertFalse("history only" in " ".join(result.warnings))

    def test_an_engine_exit_while_committing_is_reported_as_a_failed_batch(self):
        self.backend.update_lens(thickness_edit(9.5))
        session = self.session
        original = session.command

        def dying_command(text):
            if text.startswith("sav ") and "revision-000002" in text:
                session.engine_dead = True
                raise SessionInvalidError("the engine is gone")
            return original(text)

        session.command = dying_command
        self.addCleanup(lambda: setattr(session, "command", original))
        result = self.backend.update_lens(thickness_edit(11.25))
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertFalse(result.session_valid)
        # The engine is dead rather than wrong: a rebuild restores the last
        # committed revision, which is still the first batch.
        replacement = fresh_session()
        self.install_replacement(replacement)
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.surfaces[1].thickness, 9.5)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertEqual(self.status()["recovery_count"], 1)

    def test_an_engine_exit_during_the_edits_does_not_replay_the_batch(self):
        self.backend.fault_hook = lambda name: self.session.engine_dead.__setattr__("__bool__", lambda: True)
        self.backend.fault_hook = None  # the hook above is only a smoke check
        session = self.session
        replacement = self.rebuild_with()
        original = session.command

        def dying_command(text):
            if text.startswith("THI "):
                session.engine_dead = True
                raise SessionInvalidError("the engine is gone")
            return original(text)

        session.command = dying_command
        self.addCleanup(lambda: setattr(session, "command", original))
        result = self.backend.update_lens(thickness_edit(9.5))
        self.assertFalse(result.session_valid)
        # Nothing was confirmed about the lens, but the last committed revision
        # still describes it, so a rebuilt engine can be restored from it.
        self.assertEqual(self.backend._lens_state.name, "RECOVERING")
        self.assertEqual(replacement.commands, [])
        restored = self.backend.get_lens()
        self.assertAlmostEqual(restored.surfaces[1].thickness, 9.12345599999)
        # The batch that was in flight is gone: the lens is back on the last
        # committed revision instead of the value that was being applied.
        self.assertFalse(any("THI S1" in command and "9.5" in command for command in replacement.commands))
        self.assertEqual(self.status()["lens_state"], "ready")
        self.assertEqual(self.status()["recovery_count"], 1)

    def test_a_fault_hook_before_metadata_keeps_the_previous_revision(self):
        self.backend.update_lens(thickness_edit(9.5))
        calls = {"count": 0}

        def hook(name: str) -> None:
            if name == "before_metadata":
                calls["count"] += 1
                raise RuntimeError("injected fault")

        self.backend.fault_hook = hook
        result = self.backend.update_lens(thickness_edit(11.25))
        self.backend.fault_hook = None
        self.assertEqual(calls["count"], 1)
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 9.5)
        self.assertFalse((self.lens_dir() / "revision-000002.json").exists())

    def test_a_commit_that_matches_the_state_after_the_fault_is_still_kept(self):
        # A candidate whose restore reads back the requested values is the only
        # thing that may be published; a mismatching candidate is refused.
        self.backend.update_lens(thickness_edit(9.5))
        original = backend_module.read_snapshot
        calls = {"count": 0}

        def tampered(session, lens, listing=None):
            snapshot = original(session, lens, listing)
            calls["count"] += 1
            if calls["count"] == 3:
                for surface in snapshot.zooms[0].surfaces:
                    if surface.number == 1:
                        surface.thickness = 42.0
            return snapshot

        with unittest.mock.patch.object(backend_module, "read_snapshot", tampered):
            result = self.backend.update_lens(thickness_edit(11.25))
        self.assertTrue(result.rolled_back)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertFalse((self.lens_dir() / "revision-000002.json").exists())

    def test_the_engine_dying_during_an_open_keeps_the_previous_lens(self):
        session = self.session
        original = session.command

        def dying_command(text):
            if text.startswith("sav "):
                session.engine_dead = True
                raise SessionInvalidError("the engine is gone")
            return original(text)

        session.command = dying_command
        self.addCleanup(lambda: setattr(session, "command", original))
        other = self.root / "other.len"
        other.write_text("! other\n", encoding="utf-8")
        previous_id = self.status()["lens_id"]
        replacement = self.rebuild_with()
        # The lens that was open is restored from its own checkpoint rather than
        # from the source file, and the half opened lens is never exposed.
        with self.assertRaises(SessionInvalidError):
            self.backend.open_lens(str(other))
        self.assertEqual(self.backend._lens_state.name, "READY")
        self.assertEqual(self.status()["lens_id"], previous_id)
        self.assertEqual(self.status()["recovery_count"], 1)
        self.assertTrue(any("revision-000000.len" in text for text in replacement.commands))

class ReviewFixGuards(BackendTestCase):
    """Regression tests for the review findings on the recovery work."""

    # 1. open_lens must not lift an invalid state, not even through its own
    #    failure recovery path.

    def test_open_lens_is_refused_while_the_session_is_invalid(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.checkpoint_path().write_text("corrupted", encoding="utf-8")
        replacement = self.rebuild_with()
        self.session.engine_dead = True
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()
        self.assertEqual(self.backend._lens_state.name, "INVALID")
        other = self.root / "other.len"
        other.write_text("! other\n", encoding="utf-8")
        with self.assertRaises(SessionInvalidError) as caught:
            self.backend.open_lens(str(other))
        self.assertIn("invalid", caught.exception.message)
        self.assertEqual(self.backend._lens_state.name, "INVALID")
        with self.assertRaises(SessionInvalidError):
            self.backend.get_lens()

    # 2. a committed revision must survive a failing log call after the pointer
    #    was replaced.


    # 3. a read back that raises is a failed edit, not a refusal.

    def test_a_confirm_error_rolls_the_batch_back(self):
        original = self.backend._confirm
        calls = {"count": 0}

        def failing_confirm(plan, session):
            calls["count"] += 1
            if calls["count"] == 2 and plan.edit.surface == 2:
                raise RuntimeError("the read back failed")
            return original(plan, session)

        self.backend._confirm = failing_confirm
        self.addCleanup(lambda: setattr(self.backend, "_confirm", original))
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(surface=1, parameter="thickness", value=9.5),
                    ParameterEdit(surface=2, parameter="thickness", value=2.5),
                ]
            )
        )
        self.assertEqual(calls["count"], 2)
        self.assertFalse(any(outcome.applied for outcome in result.outcomes))
        self.assertTrue(result.rolled_back)
        self.assertIn("could not be read back", result.warnings[-2])
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.surfaces[1].thickness, 9.12345599999)
        self.assertAlmostEqual(lens.surfaces[2].thickness, 4.25)
        self.assertEqual(self.status()["committed_revision"], 0)

    # 4. a glass that cannot be read is a failed verification, never an equal
    #    empty value.

    def test_a_glass_read_that_raises_fails_the_verification(self):
        original = self.session.evaluate
        calls = {"count": 0}
        state = {"editing": False}

        def failing_glass(item):
            calls["count"] += 1
            if state["editing"] and calls["count"] > 50 and "GLA" in item:
                raise SessionInvalidError("the glass item could not be read")
            return original(item)

        self.session.evaluate = failing_glass
        self.addCleanup(lambda: setattr(self.session, "evaluate", original))
        # Force the rollback path; the glass read then fails while the restored
        # state is being verified, and that must never compare as equal.
        self.backend._confirm = lambda plan, session: (False, "BSM24")
        state["editing"] = True
        result = self.backend.update_lens(thickness_edit(9.5))
        state["editing"] = False
        self.assertFalse(result.rolled_back)
        self.assertFalse(result.session_valid)
        self.assertEqual(self.status()["lens_state"], "invalid")


    # 5. the recovery has to follow current.json and the lens identity.

    def test_a_recovery_refuses_a_pointer_that_names_another_revision(self):
        self.backend.update_lens(thickness_edit(9.5))
        self.backend.update_lens(thickness_edit(11.25))
        pointer = self.lens_dir() / "current.json"
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        payload["revision"] = 1
        payload["lens_file"] = "revision-000001.len"
        pointer.write_text(json.dumps(payload), encoding="utf-8")
        replacement = self.rebuild_with()
        self.session.engine_dead = True
        with self.assertRaises(CheckpointError):
            self.backend.get_lens()
        self.assertEqual(self.status()["lens_state"], "invalid")

    def test_a_recovery_refuses_a_pointer_of_another_lens(self):
        self.backend.update_lens(thickness_edit(9.5))
        pointer = self.lens_dir() / "current.json"
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        payload["lens_id"] = "not-this-lens"
        pointer.write_text(json.dumps(payload), encoding="utf-8")
        replacement = self.rebuild_with()
        self.session.engine_dead = True
        with self.assertRaises(CheckpointError):
            self.backend.get_lens()
        self.assertEqual(self.status()["lens_state"], "invalid")

    # 6. analysis results belong to one lens and one revision.

    def test_analysis_results_are_bound_to_the_lens_identity(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        details = self.status()
        self.assertEqual(details["analysis_lens_id"], details["lens_id"])
        self.assertIsNotNone(self.backend.get_analysis().first_order)
        # A different lens of the same backend must not inherit the result.
        other = self.root / "other.len"
        other.write_text("! other\n", encoding="utf-8")
        self.backend.open_lens(str(other))
        snapshot = self.backend.get_analysis()
        self.assertTrue(snapshot.task.history_only)
        self.assertFalse(snapshot.task.warnings == [])
        self.assertIsNotNone(snapshot.first_order)
        self.assertEqual(snapshot.source.value, "codev")

    def test_engine_loss_keeps_a_finished_analysis_result(self):
        task = self.backend.run_analysis(
            AnalysisRequest(kind=AnalysisKind.FIRST_ORDER, options=AnalysisOptions())
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        replacement = self.rebuild_with()
        self.session.engine_dead = True
        self.backend.get_lens()
        snapshot = self.backend.get_analysis()
        self.assertEqual(snapshot.task.task_id, task.task_id)
        self.assertEqual(snapshot.task.state, TaskState.SUCCEEDED)
        self.assertIsNotNone(snapshot.first_order)
        self.assertFalse(snapshot.task.history_only)

    # 7. a batch that edits one parameter twice only requires the last value.

    def test_a_repeated_edit_only_requires_the_last_value(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(surface=1, parameter="thickness", value=9.5),
                    ParameterEdit(surface=1, parameter="thickness", value=11.25),
                ]
            )
        )
        self.assertTrue(all(outcome.applied for outcome in result.outcomes), result.warnings)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 11.25)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertTrue(
            any("more than once" in warning for warning in result.warnings),
            result.warnings,
        )


    # 8. the acceptance script must never delete a directory it does not own.
    def test_a_log_that_keeps_failing_after_the_commit_does_not_roll_back(self):
        """A log sink that rejects every message must not undo a commit."""
        failures = {"count": 0}

        def dead_sink(message: str) -> None:
            if "committed at" in str(message):
                failures["count"] += 1
                raise OSError("the log sink is gone")

        self.backend.log = dead_sink
        try:
            result = self.backend.update_lens(thickness_edit(12.0))
            # The committed branch must survive even the diagnostics failing.
            lens = self.backend.get_lens()
            details = self.status()
        finally:
            self.backend.log = lambda message: None
        self.assertGreaterEqual(failures["count"], 1)
        self.assertTrue(result.outcomes[0].applied, result.warnings)
        self.assertFalse(result.rolled_back)
        self.assertEqual(details["committed_revision"], 1)
        pointer = json.loads((self.lens_dir() / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["revision"], 1)
        self.assertAlmostEqual(lens.surfaces[1].thickness, 12.0)
        self.assertAlmostEqual(self.session.surfaces[1]["thickness"], 12.0)

    def test_a_read_failure_after_the_commit_reports_a_committed_batch(self):
        """The follow up read must not turn a committed batch into a failure."""
        original = self.backend._read_lens
        calls = {"count": 0, "failed": False}

        def failing_read(zoom_position=None):
            calls["count"] += 1
            # The commit itself reads the candidate while the revision is
            # still the old one; only the follow up read fails, and only once.
            if self.backend._committed_revision == 1 and not calls["failed"]:
                calls["failed"] = True
                raise ComputationError("the lens could not be read after the commit")
            return original(zoom_position=zoom_position)

        self.backend._read_lens = failing_read
        self.addCleanup(lambda: setattr(self.backend, "_read_lens", original))
        result = self.backend.update_lens(thickness_edit(12.0))
        self.assertTrue(calls["failed"], "the follow up read was never exercised")
        self.assertTrue(result.outcomes[0].applied, result.warnings)
        self.assertFalse(result.rolled_back)
        # The lens is not confirmed, so the next call restores the committed
        # revision; the committed batch itself is reported as applied.
        self.assertEqual(self.backend._lens_state.name, "RECOVERING")
        lens = self.backend.get_lens()
        self.assertAlmostEqual(lens.surfaces[1].thickness, 12.0)
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertAlmostEqual(self.session.surfaces[1]["thickness"], 12.0)


    def test_a_shared_parameter_edited_at_two_zoom_positions_collapses(self):
        session = fresh_session(zoom_positions=2)
        self.backend._session = session
        self.backend._lens = None
        self.backend.open_lens(str(self.lens_path))
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="thickness", value=10.0, zoom_position=1
                    ),
                    ParameterEdit(
                        surface=1, parameter="thickness", value=12.0, zoom_position=2
                    ),
                ]
            )
        )
        self.assertTrue(all(outcome.applied for outcome in result.outcomes), result.warnings)
        self.assertFalse(result.rolled_back)
        self.assertAlmostEqual(session.surfaces[1]["thickness"], 12.0)
        self.assertEqual(self.status()["committed_revision"], 1)

    def test_a_reference_changed_twice_collapses_to_the_last_wavelength(self):
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        target="wavelength", wavelength=1, parameter="is_reference", value=1
                    ),
                    ParameterEdit(
                        target="wavelength", wavelength=2, parameter="is_reference", value=2
                    ),
                ]
            )
        )
        self.assertTrue(all(outcome.applied for outcome in result.outcomes), result.warnings)
        self.assertFalse(result.rolled_back)
        reference = next(
            item for item in self.backend.get_lens().wavelengths if item.is_reference
        )
        self.assertEqual(reference.number, 2)
        self.assertEqual(self.status()["committed_revision"], 1)

    def test_really_zoomed_parameters_are_required_separately(self):
        session = fresh_session(zoom_positions=2)
        session.zoom_surface(1, "thickness", {1: 8.0, 2: 12.0})
        self.backend._session = session
        self.backend._lens = None
        self.backend.open_lens(str(self.lens_path))
        result = self.backend.update_lens(
            UpdateRequest(
                edits=[
                    ParameterEdit(
                        surface=1, parameter="thickness", value=9.0, zoom_position=1
                    ),
                    ParameterEdit(
                        surface=1, parameter="thickness", value=13.0, zoom_position=2
                    ),
                ]
            )
        )
        self.assertTrue(all(outcome.applied for outcome in result.outcomes), result.warnings)
        values = session.surfaces[1]["zoom"]["thickness"]
        self.assertAlmostEqual(values[1], 9.0)
        self.assertAlmostEqual(values[2], 13.0)
        self.assertEqual(self.status()["committed_revision"], 1)

    def test_a_failing_transaction_record_after_the_commit_keeps_applied(self):
        """Disk writes and the log sink failing together must not undo a commit."""
        original = self.backend.checkpoint_store.write_transaction
        broken = {"log": False}

        def writer(directory, transaction_id, payload):
            if payload.get("state") in {"committed", "committed_with_warnings"}:
                raise OSError("disk full writing the committed transaction")
            return original(directory, transaction_id, payload)

        def logger(message: str) -> None:
            if "committed at" in str(message):
                broken["log"] = True
            if broken["log"]:
                raise OSError("log sink unavailable")

        self.backend.checkpoint_store.write_transaction = writer
        self.backend.log = logger
        try:
            result = self.backend.update_lens(thickness_edit(12.0))
            lens = self.backend.get_lens()
            details = self.status()
        finally:
            self.backend.log = lambda message: None
            self.backend.checkpoint_store.write_transaction = original
        self.assertTrue(broken["log"], "the broken log sink was never exercised")
        self.assertTrue(result.outcomes[0].applied, result.warnings)
        self.assertFalse(result.rolled_back)
        self.assertTrue(result.session_valid)
        self.assertEqual(details["lens_state"], "ready")
        self.assertEqual(details["committed_revision"], 1)
        pointer = json.loads((self.lens_dir() / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["revision"], 1)
        self.assertAlmostEqual(lens.surfaces[1].thickness, 12.0)
        self.assertAlmostEqual(self.session.surfaces[1]["thickness"], 12.0)
        # The record could not be written, which is reported, but the commit
        # itself is unchanged.
        self.assertTrue(
            any(
                "record of the committed revision could not be written" in warning
                for warning in result.warnings
            ),
            result.warnings,
        )
        self.assertFalse(
            any("could not be written (" in warning for warning in result.warnings),
            result.warnings,
        )
        records = sorted((self.lens_dir() / "transactions").glob("tx-*.json"))
        final = json.loads(records[-1].read_text(encoding="utf-8"))
        # The committed record could not reach the disk, so the last record that
        # exists is the editing one; that is reported instead of being hidden.
        self.assertIn(final["state"], {"editing", "committed", "committed_with_warnings"})
        if final["state"].startswith("committed"):
            self.assertEqual(final["to_revision"], 1)

    def test_a_failed_batch_never_reports_the_previous_batch_success(self):
        """A pre-commit failure must not answer with an earlier commit."""
        first = self.backend.update_lens(thickness_edit(10.0))
        self.assertTrue(first.outcomes[0].applied, first.warnings)
        self.assertEqual(self.status()["committed_revision"], 1)

        def unexpected_hook(name: str) -> None:
            if name == "before_edit_command":
                raise RuntimeError("unexpected pre-commit failure")

        self.backend.fault_hook = unexpected_hook
        try:
            second = self.backend.update_lens(thickness_edit(12.0))
        finally:
            self.backend.fault_hook = None
        self.assertIsNot(second, first, "the previous result was reused")
        self.assertEqual(second.outcomes[0].edit.value, 12.0)
        self.assertFalse(second.outcomes[0].applied)
        self.assertIn("unexpected pre-commit failure", second.outcomes[0].rejected_reason or "")
        self.assertEqual(second.restore_point, "rp-0002")
        self.assertEqual(self.status()["committed_revision"], 1)
        self.assertAlmostEqual(self.session.surfaces[1]["thickness"], 10.0)

    def test_a_failed_batch_after_a_lens_switch_never_reports_the_old_lens(self):
        """The commit fact of the previous lens must not outlive the switch."""
        first = self.backend.update_lens(thickness_edit(10.0))
        self.assertTrue(first.outcomes[0].applied, first.warnings)
        other = self.root / "other.len"
        other.write_text("! other\n", encoding="utf-8")
        self.backend.open_lens(str(other))
        self.assertEqual(self.status()["committed_revision"], 0)

        def unexpected_hook(name: str) -> None:
            if name == "before_edit_command":
                raise RuntimeError("unexpected pre-commit failure")

        self.backend.fault_hook = unexpected_hook
        try:
            second = self.backend.update_lens(thickness_edit(12.0))
        finally:
            self.backend.fault_hook = None
        self.assertIsNot(second, first)
        self.assertFalse(second.outcomes[0].applied)
        self.assertEqual(second.outcomes[0].edit.value, 12.0)
        self.assertEqual(self.status()["committed_revision"], 0)
        self.assertNotEqual(self.status()["lens_id"], None)

    def test_success_then_failure_keeps_the_committed_revision_and_lens(self):
        first = self.backend.update_lens(thickness_edit(10.0))
        self.assertTrue(first.outcomes[0].applied, first.warnings)
        second = self.backend.update_lens(thickness_edit(12.0))
        self.assertTrue(second.outcomes[0].applied, second.warnings)
        self.assertEqual(self.status()["committed_revision"], 2)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 12.0)

        def unexpected_hook(name: str) -> None:
            if name == "before_edit_command":
                raise RuntimeError("unexpected pre-commit failure")

        self.backend.fault_hook = unexpected_hook
        try:
            third = self.backend.update_lens(thickness_edit(14.0))
        finally:
            self.backend.fault_hook = None
        self.assertFalse(third.outcomes[0].applied)
        self.assertEqual(third.restore_point, "rp-0003")
        self.assertEqual(self.status()["committed_revision"], 2)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 12.0)
        self.assertAlmostEqual(self.session.surfaces[1]["thickness"], 12.0)

    def test_a_batch_after_a_post_commit_fault_read_failure_still_succeeds(self):
        """A commit fact is per transaction: the next batch works normally."""
        original = self.backend._read_lens
        calls = {"count": 0, "failed": False}

        def failing_read(zoom_position=None):
            calls["count"] += 1
            if self.backend._committed_revision == 1 and not calls["failed"]:
                calls["failed"] = True
                raise ComputationError("the lens could not be read after the commit")
            return original(zoom_position=zoom_position)

        self.backend._read_lens = failing_read
        try:
            first = self.backend.update_lens(thickness_edit(12.0))
            self.assertTrue(first.outcomes[0].applied, first.warnings)
            second = self.backend.update_lens(thickness_edit(13.0))
        finally:
            self.backend._read_lens = original
        self.assertTrue(calls["failed"])
        self.assertTrue(second.outcomes[0].applied, second.warnings)
        self.assertFalse(second.rolled_back)
        self.assertIsNot(second, first)
        self.assertEqual(self.status()["committed_revision"], 2)
        self.assertAlmostEqual(self.backend.get_lens().surfaces[1].thickness, 13.0)
        self.assertAlmostEqual(self.session.surfaces[1]["thickness"], 13.0)


VIGNETTED_FIELDS = [
    {"x": 0.0, "y": 0.0, "weight": 1.0},
    {"x": 0.0, "y": 10.0, "weight": 1.0, "vuy": 0.2, "vly": 0.3},
]


class VignettedLens(BackendTestCase):
    """D3: vignetting factors are read, checkpointed and verified on recovery."""

    session_kwargs = {"fields": [dict(item) for item in VIGNETTED_FIELDS]}

    def test_get_lens_reports_the_factors_without_cross_check_warnings(self):
        lens = self.backend.get_lens()
        self.assertEqual([(f.vux, f.vlx, f.vuy, f.vly) for f in lens.fields],
                         [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.2, 0.3)])
        self.assertFalse([w for w in lens.warnings if "vignetting" in w], lens.warnings)
        self.assertIn("VUY", lens.raw_listing)

    def test_the_checkpoint_keeps_the_factors_in_format_six(self):
        payload = self.checkpoint_json(0)
        self.assertEqual(payload["format_version"], 6)
        fields = payload["snapshot"]["zooms"][0]["fields"]
        self.assertEqual((fields[1]["vuy"], fields[1]["vly"], fields[1]["vux"]), (0.2, 0.3, 0.0))

    def test_factors_are_edited_read_back_and_checkpointed(self):
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(target="field", field=2, parameter="vuy", value=0.25),
            ParameterEdit(target="field", field=2, parameter="vly", value=0.35)]))
        self.assertTrue(all(o.applied for o in result.outcomes) and not result.rolled_back, result.warnings)
        self.assertEqual([o.previous_value for o in result.outcomes], [0.2, 0.3])
        self.assertIn("VUY F2 0.25", self.session.commands)
        lens = self.backend.get_lens()
        self.assertEqual((lens.fields[1].vuy, lens.fields[1].vly), (0.25, 0.35))
        self.assertFalse([w for w in lens.warnings if "vignetting" in w], lens.warnings)
        fields = self.checkpoint_json(1)["snapshot"]["zooms"][0]["fields"]
        self.assertEqual((fields[1]["vuy"], fields[1]["vly"]), (0.25, 0.35))

    def test_an_out_of_range_factor_is_refused_before_any_command(self):
        sent = len(self.session.commands)
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(target="field", field=2, parameter="vuy", value=1.0)]))
        self.assertFalse(result.outcomes[0].applied)
        self.assertFalse([c for c in self.session.commands[sent:] if c.startswith("VUY")])
        self.assertEqual(self.status()["committed_revision"], 0)

    def test_recovery_restores_and_verifies_the_factors(self):
        self.backend.update_lens(thickness_edit(9.5))
        replacement = fresh_session()
        self.install_replacement(replacement)
        self.session.engine_dead = True
        lens = self.backend.get_lens()
        self.assertEqual((lens.fields[1].vuy, lens.fields[1].vly), (0.2, 0.3))
        self.assertEqual(self.status()["last_recovery"]["result"], "succeeded")

    def test_recovery_refuses_a_restore_that_lost_the_factors(self):
        replacement = fresh_session()
        original_load = replacement._load

        def lossy_load(snapshot):
            original_load(snapshot)
            for item in replacement.fields:
                item.pop("vuy", None)
                item.pop("vly", None)
            replacement.listing = replacement._build_listing()

        replacement._load = lossy_load
        self.install_replacement(replacement)
        self.session.engine_dead = True
        with self.assertRaises(CheckpointVerificationError) as caught:
            self.backend.get_lens()
        self.assertIn("VUY vignetting", json.dumps(caught.exception.details))
        self.assertEqual(self.status()["lens_state"], "invalid")

    def test_an_edit_that_moves_a_factor_is_rolled_back(self):
        original = self.session._run_command

        def moving(text):
            output = original(text)
            if text.startswith("YAN F2"):
                self.session.fields[1]["vuy"] = 0.25
                self.session.listing = self.session._build_listing()
            return output

        self.session._run_command = moving
        self.addCleanup(lambda: setattr(self.session, "_run_command", original))
        result = self.backend.update_lens(UpdateRequest(edits=[
            ParameterEdit(target="field", field=2, parameter="y_angle", value=11.0)]))
        self.assertTrue(result.rolled_back, result.warnings)
        self.assertIn("VUY vignetting changed", result.warnings[0])
        self.assertEqual(self.status()["committed_revision"], 0)


class StaleVignettingItems(unittest.TestCase):
    """An item CODE V does not understand echoes the last value; open must refuse it."""

    def test_open_refuses_factors_that_disagree_with_the_listing(self):
        temp = workspace_temp_directory("vignetting")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        lens_path = root / "dbgauss.len"
        lens_path.write_text("! placeholder\n", encoding="utf-8")
        session = fresh_session(fields=[dict(item) for item in VIGNETTED_FIELDS])
        original = session._evaluate_body
        session._evaluate_body = lambda body: None if body.startswith("VLY") else original(body)
        backend = ComBackend(working_directory=root / "run", session=session)
        with self.assertRaises(ComputationError) as caught:
            backend.open_lens(str(lens_path))
        self.assertIn("vignetting", json.dumps(caught.exception.details))
        self.assertEqual(backend.get_status().details["lens_state"], "empty")

    def test_a_format_five_checkpoint_is_refused_with_a_vignetting_hint(self):
        temp = workspace_temp_directory("store-five")
        self.addCleanup(temp.cleanup)
        store = LensCheckpointStore(Path(temp.name) / "checkpoints", backend_id="b")
        lens_id, directory = store.create_lens()
        lens = directory / "revision-000000.len"
        lens.write_text("lens", encoding="utf-8")
        store.publish(directory, lens_id, 0, lens, LensSnapshot(units="mm"), source_path=None)
        pointer = directory / "current.json"
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        payload["format_version"] = 5
        pointer.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError) as caught:
            store.load_current(directory)
        self.assertIn("vignetting", caught.exception.hint)


if __name__ == "__main__":
    unittest.main()
