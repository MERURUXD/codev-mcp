"""Synthetic GLD listings; vendor catalog data is never committed as a fixture."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codev_mcp.glass import (CATALOGS, GlassCandidate, catalog_rows, detail_matches,
                             query_glass, relative_index)
from codev_mcp.errors import ParameterError


def summary(catalog: str, name: str = "BK7", code: str = "517642") -> str:
    return (f"   {catalog}   GLASSES ON DISC  PAGE 1\n"
            "GLASS CODES   587.6  546.1\n"
            f" {code} {name} 1.51680 1.51872\nCommand End:\n")


DETAIL = "BK7 - 517642 Schott\nREFRACTIVE INDICES\nCommand End:\n"
RELATIVE = ("PARTIAL DISPERSION DATA FOR SCHOTT\n"
            "WVL(1) = 550.00 NM WVL(3) = 587.56 NM\n"
            "GLASS CODES DP DPF N(1) N(2) N(3) N(4) PRICE\n"
            "BK7 517642 87-31575 1.518522 1.522376 1.516800 1.514322 0.00\n")


class FakeSession:
    instances: list["FakeSession"] = []
    wrong_catalog = False
    failed_catalog = None
    only_schott_has_bk7 = False

    def __init__(self, *, starting_directory):
        self.commands: list[str] = []
        self.call_count = 0
        self.closed = False
        self.instances.append(self)

    def start(self):
        return "10.2;Build (207)"

    def command_raw(self, command):
        self.commands.append(command)
        self.call_count += 1
        if command.startswith("gld;gli "):
            requested = command.split()[-1]
            if self.wrong_catalog:
                return summary("OHARA") + summary("SCHOTT")
            if requested == self.failed_catalog:
                return "GLI query failed\nCommand End:\n"
            name = "BK7" if requested == "SCHOTT" or (
                requested == "OHARA" and not self.only_schott_has_bk7) else "F2"
            return summary(requested, name)
        if command == "gld;gpr SCHOTT BK7":
            return DETAIL
        if command.startswith("gld;rel SCHOTT BK7 "):
            return RELATIVE
        raise AssertionError(command)

    def output_is_truncated(self, text):
        return False

    def stop(self):
        self.closed = True


class GlassTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # The real registration lookup returns a resolved path; match that
        # contract even when Windows TEMP uses an 8.3 directory alias.
        self.root = Path(self.temp.name).resolve()
        path = self.root / "glass" / "glass.cat"
        path.parent.mkdir()
        path.write_bytes(b"v2.codevglass.cat\0synthetic")
        FakeSession.instances = []
        FakeSession.wrong_catalog = False
        FakeSession.failed_catalog = None
        FakeSession.only_schott_has_bk7 = False
        registered = patch("codev_mcp.glass.registered_install_root", return_value=self.root)
        registered.start()
        self.addCleanup(registered.stop)

    def query(self, name="BK7", catalog="SCHOTT", wavelength_nm=550):
        return query_glass(name, catalog=catalog, wavelength_nm=wavelength_nm,
                           install_root=self.root, run_root=self.root / "runs",
                           session_factory=FakeSession)

    def test_exact_catalog_and_index(self):
        result = self.query()
        self.assertEqual(result.status, "found")
        self.assertEqual(result.candidates[0].catalog, "SCHOTT")
        self.assertAlmostEqual(result.index.refractive_index, 1.518522)
        self.assertEqual(result.com_calls, 3)
        self.assertTrue(FakeSession.instances[0].closed)
        self.assertTrue(Path(result.detail_raw_path).is_file())
        self.assertTrue(all(command.startswith("gld;") for command in FakeSession.instances[0].commands))

    def test_duplicate_name_needs_catalog(self):
        result = self.query(catalog=None, wavelength_nm=None)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({row.catalog for row in result.candidates}, {"SCHOTT", "OHARA"})
        self.assertEqual(result.com_calls, len(CATALOGS))
        self.assertTrue(FakeSession.instances[0].closed)

    def test_missing_name_lists_suggestions_only(self):
        result = self.query(name="BK8", wavelength_nm=None)
        self.assertEqual(result.status, "not_found")
        self.assertFalse(result.candidates)
        self.assertTrue(any(row.name == "BK7" for row in result.suggestions))
        self.assertEqual(result.com_calls, 1)

    def test_unavailable_catalog_does_not_start_com(self):
        result = self.query(catalog="UNKNOWN")
        self.assertEqual(result.status, "catalog_unavailable")
        self.assertEqual(FakeSession.instances, [])
        (self.root / "glass" / "glass.cat").unlink()
        result = self.query()
        self.assertEqual(result.status, "catalog_unavailable")
        self.assertEqual(FakeSession.instances, [])

    def test_catalog_fallback_and_wrong_detail_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "did not confirm"):
            catalog_rows(summary("OHARA") + summary("SCHOTT"), "SCHOTT")
        FakeSession.wrong_catalog = True
        with self.assertRaisesRegex(ValueError, "did not confirm"):
            self.query()
        self.assertTrue(FakeSession.instances[0].closed)
        self.assertFalse(detail_matches("BK7 - 517642 Ohara", GlassCandidate(
            catalog="SCHOTT", name="BK7", code="517642")))

    def test_failed_catalog_cannot_produce_a_unique_match(self):
        FakeSession.failed_catalog = "OHARA"
        with self.assertRaisesRegex(ValueError, "OHARA"):
            self.query(catalog=None, wavelength_nm=None)
        self.assertTrue(FakeSession.instances[0].closed)
        self.assertNotIn("gld;gpr SCHOTT BK7", FakeSession.instances[0].commands)

    def test_catalog_failure_after_match_still_rejects_result(self):
        FakeSession.only_schott_has_bk7 = True
        FakeSession.failed_catalog = "SPECIAL"
        with self.assertRaisesRegex(ValueError, "SPECIAL"):
            self.query(catalog=None, wavelength_nm=None)
        commands = FakeSession.instances[0].commands
        self.assertIn("gld;gli SCHOTT", commands)
        self.assertNotIn("gld;gpr SCHOTT BK7", commands)
        self.assertTrue(FakeSession.instances[0].closed)

    def test_incomplete_listing_is_not_an_empty_catalog(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            catalog_rows(summary("SCHOTT").replace("Command End:", ""), "SCHOTT")
        with self.assertRaisesRegex(ValueError, "no verifiable"):
            catalog_rows("SCHOTT GLASSES ON DISC\nCommand End:\n", "SCHOTT")

    def test_install_root_must_match_registered_com_installation(self):
        other = self.root / "another-install"
        (other / "glass").mkdir(parents=True)
        (other / "glass" / "glass.cat").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "differs from the registered"):
            query_glass("BK7", catalog="SCHOTT", install_root=other,
                        run_root=self.root / "runs", session_factory=FakeSession)
        self.assertEqual(FakeSession.instances, [])

    def test_catalog_change_during_query_fails(self):
        catalog_file = self.root / "glass" / "glass.cat"

        class ChangingSession(FakeSession):
            def command_raw(self, command):
                answer = super().command_raw(command)
                catalog_file.write_bytes(b"changed")
                return answer

        with self.assertRaisesRegex(ValueError, "changed during"):
            query_glass("BK7", catalog="SCHOTT", install_root=self.root,
                        run_root=self.root / "runs", session_factory=ChangingSession)
        self.assertTrue(FakeSession.instances[0].closed)

    def test_cleanup_refusal_cannot_return_found(self):
        class RefusedCleanup(FakeSession):
            def stop(self):
                super().stop()
                return False

        with self.assertRaisesRegex(RuntimeError, "cleanup unconfirmed"):
            query_glass("BK7", catalog="SCHOTT", install_root=self.root,
                        run_root=self.root / "runs", session_factory=RefusedCleanup)

    def test_index_absence_and_input_validation(self):
        candidate = GlassCandidate(catalog="SCHOTT", name="BK7", code="517642")
        self.assertFalse(relative_index(RELATIVE.replace("1.518522", "-----"), candidate, 550).available)
        self.assertFalse(relative_index(RELATIVE, candidate, 551).available)
        for name, wave in (("BK7;DEL", 550), ("BK7", float("nan")), ("BK7", 0)):
            with self.subTest(name=name, wave=wave), self.assertRaises((ValueError, ParameterError)):
                self.query(name=name, wavelength_nm=wave)
        self.assertEqual(FakeSession.instances, [])


if __name__ == "__main__":
    unittest.main()
