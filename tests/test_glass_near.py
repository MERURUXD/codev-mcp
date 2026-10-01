"""D8 nearby-glass listing: GLI column parsing, ranking and refusals (synthetic data)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codev_mcp import glass_near
from codev_mcp.glass_near import catalog_table, nearest, query_near

WAVES = ("1014.0", "852.1", "706.5", "656.3", "587.6", "546.1", "486.1", "435.8", "404.7", "365.0")


def listing(catalog: str, rows: list[tuple[str, str, tuple]]) -> str:
    """A GLI-shaped page with made-up glasses; values right-aligned under the header."""
    lines = ["    GLD", "", f"                   {catalog:<13} GLASSES ON DISC            28-Sep-26       PAGE  1", "",
             "   GLASS CODES     " + "    ".join(f"{w:>5}" for w in WAVES).replace(" 852.1", "852.1"), "-" * 106]
    header = lines[4]
    ends = [header.index(w) + len(w) for w in WAVES]
    for code, name, values in rows:
        row = f" {code} {name:<10}"
        for end, value in zip(ends, values):
            if value is None:
                continue
            text = f"{value:.5f}"
            row = row.ljust(end + 1 - len(text)) + text
        lines.append(row)
    return "\r\n".join(lines + ["Error:   Invalid lens - pupil not defined", "Command End:"]) + "\r\n"


def indices(nd: float, vd: float) -> tuple:
    dispersion = (nd - 1) / vd
    nc, nf = nd - 0.3 * dispersion, nd + 0.7 * dispersion
    return (nd - 0.01, nd - 0.006, nd - 0.004, round(nc, 5), nd, nd + 0.003, round(nf, 5), nd + 0.012, nd + 0.017, None)


class FakeSession:
    def __init__(self, **_kwargs):
        self.call_count = 0
        self.pages = {"SCHOTT": listing("SCHOTT", [("620603", "REFA", indices(1.62041, 60.3))]),
                      "TESTCAT": listing("CDGM", [])}

    def start(self):
        return "10.2;Build (207)"

    def command_raw(self, text):
        self.call_count += 1
        catalog = text.split()[-1]
        return self.pages[catalog]

    def output_is_truncated(self, _text):
        return False

    def stop(self):
        return True


class GlassNearTests(unittest.TestCase):
    def test_columns_are_read_by_position_and_vd_computed(self):
        text = listing("CDGM", [("620603", "CAND1", indices(1.62, 60.0)),
                                ("613586", "CAND2", indices(1.613, 58.6)),
                                ("700300", "GAP", (None,) * 4 + (1.7,) + (None,) * 5)])
        rows = catalog_table(text, "CDGM")
        by_name = {row["name"]: row for row in rows}
        self.assertAlmostEqual(by_name["CAND1"]["nd"], 1.62)
        self.assertAlmostEqual(by_name["CAND1"]["vd"], 60.0, delta=0.1)
        self.assertNotIn("nd", by_name["GAP"])
        ranked = nearest({"nd": 1.62041, "vd": 60.3}, rows, nd_scale=0.01, vd_scale=1.0, limit=5)
        self.assertEqual([row["name"] for row in ranked], ["CAND1", "CAND2"])
        self.assertLess(ranked[0]["distance"], ranked[1]["distance"])
        self.assertGreater(ranked[0]["vd_uncertainty"], 0)

    def test_listing_must_confirm_catalog_and_end(self):
        text = listing("CDGM", [("620603", "CAND1", indices(1.62, 60.0))])
        with self.assertRaisesRegex(ValueError, "requested catalog"):
            catalog_table(text, "HOYA")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            catalog_table(text.replace("Command End:", ""), "CDGM")
        with self.assertRaisesRegex(ValueError, "587.6"):
            catalog_table(text.replace("587.6", "589.3"), "CDGM")

    def test_query_uses_only_named_catalogs(self):
        cdgm = listing("CDGM", [("620603", "CAND1", indices(1.62, 60.0)),
                                ("717295", "FAR", indices(1.717, 29.5))])

        class Session(FakeSession):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.pages["CDGM"] = cdgm

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(glass_near, "installed_catalog_file", return_value=Path(__file__)):
            result = query_near(glass="REFA_SCHOTT", catalogs=["CDGM"], limit=1, run_root=Path(tmp),
                                session_factory=Session)
            self.assertEqual(result["reference"]["code"], "620603")
            self.assertEqual([row["name"] for row in result["candidates"]["CDGM"]], ["CAND1"])
            self.assertIn("not optical equivalence", result["definitions"]["distance"])
            self.assertTrue((Path(result["run_directory"]) / "result.json").is_file())
            for kwargs, message in (({"glass": "REFA_SCHOTT", "catalogs": []}, "target catalogs"),
                                    ({"glass": "REFA", "catalogs": ["CDGM"]}, "NAME_CATALOG"),
                                    ({"nd": 1.6, "catalogs": ["CDGM"]}, "either"),
                                    ({"nd": 1.6, "vd": 60, "catalogs": ["MYCAT"]}, "pre-stored")):
                with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                    query_near(run_root=Path(tmp), session_factory=Session, **kwargs)


if __name__ == "__main__":
    unittest.main()
