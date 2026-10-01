"""Tests for the CODE V listing parser.

All inline listings are hand-authored synthetic format fixtures.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from codev_mcp.listing import parse_listing, parse_spot_listing, vignetting_mismatches

DBGAUSS_LISTING = """     Synthetic compound parser lens
                RDY             THI     RMD       GLA           CCY   THC   GLC
> OBJ:        INFINITY        INFINITY                          100   100
    1:        64.12345        5.234567       BSM24_OHARA          0   100
    2:       170.76543        0.456789                            0     0
    3:        48.12345       11.234560       SK1_SCHOTT           0   100
    4:        INFINITY        4.654321       F15_SCHOTT         100   100
    5:        31.87654       13.765432                            0     0
  STO:        INFINITY       10.345678                          100     0
    7:       -36.12345        4.654321       F15_SCHOTT           0   100
    8:        INFINITY        9.876543       SK16_SCHOTT        100   100
    9:       -47.56789        0.456789                            0     0
   10:       510.12345        7.345678       SK16_SCHOTT          0   100
   11:       -70.45678       61.234567                            0   PIM
  IMG:        INFINITY        0.000000                          100     0

 SPECIFICATION DATA
    EPD       50.00000
    DIM             MM
    WL          656.30    587.60    486.10
    REF              2
    WTW              1         1         1
    XAN        0.00000       0.00000       0.00000
    YAN        0.00000      10.00000      14.00000
    WTF        1.00000       1.00000       1.00000
    VUY        0.00000       0.20000       0.40000
    VLY        0.00000       0.30000       0.40000
    POL            N

 REFRACTIVE INDICES
     GLASS CODE                    656.30       587.60       486.10

 SOLVES
    PIM

 No pickups defined in system

 INFINITE CONJUGATES
    EFL       100.0001
    BFL        61.2346
    FFL       -29.1234
    FNO         2.0000
    IMG DIS    61.2346
    OAL        81.9134
    PARAXIAL IMAGE
     HT        23.4567
    ANG        14.0000
    ENTRANCE PUPIL
     DIA       50.0000
     THI       53.1234
    EXIT PUPIL
     DIA       56.1234
     THI      -49.1234
Command End:
"""


class ParseListing(unittest.TestCase):
    def test_project_owned_native_singlet(self):
        text = (Path(__file__).parent / "data" / "project-owned" / "lis.txt").read_text(encoding="utf-8")
        listing = parse_listing(text)
        self.assertEqual(len(listing.surfaces), 4)
        self.assertEqual(listing.surfaces[1].label, "STO")
        self.assertEqual(listing.surfaces[1].glass, "BK7_SCHOTT")
        self.assertEqual(listing.specification.wavelengths_nm, [650, 550, 450])
        self.assertEqual(listing.specification.field_angles_y, [0, 3, 6])
        self.assertIn("PIM", listing.solves)
        self.assertTrue(listing.relation_data_complete)

    def test_no_explicit_aperture_block_is_complete_default_only_state(self):
        listing = parse_listing(
            "A lens\nSPECIFICATION DATA\n   EPD 50\nCommand End:\n"
        )
        self.assertEqual(listing.aperture_usage, "default_only")
        self.assertEqual(listing.apertures, [])
        self.assertTrue(listing.aperture_data_complete)

    def setUp(self) -> None:
        self.listing = parse_listing(DBGAUSS_LISTING)

    def test_title(self):
        self.assertEqual(self.listing.title, "Synthetic compound parser lens")

    def test_surface_rows(self):
        self.assertEqual(len(self.listing.surfaces), 13)
        self.assertEqual(self.listing.surfaces[0].label, "OBJ")
        self.assertEqual(self.listing.surfaces[6].label, "STO")
        self.assertEqual(self.listing.surfaces[-1].label, "IMG")
        self.assertAlmostEqual(self.listing.surfaces[1].radius, 64.12345)
        self.assertAlmostEqual(self.listing.surfaces[1].thickness, 5.234567)
        self.assertEqual(self.listing.surfaces[1].glass, "BSM24_OHARA")
        self.assertIsNone(self.listing.surfaces[0].radius)
        self.assertIsNone(self.listing.surfaces[0].thickness)

    def test_surface_labels_use_the_printed_numbers(self):
        labeled = {row.label: row for row in self.listing.surfaces}
        self.assertIn("10", labeled)
        self.assertIn("11", labeled)

    def test_specification_block(self):
        specification = self.listing.specification
        self.assertEqual(specification.aperture_kind, "epd")
        self.assertAlmostEqual(specification.aperture_value, 50.0)
        self.assertEqual(specification.dimension, "MM")
        self.assertEqual(specification.wavelengths_nm, [656.30, 587.60, 486.10])
        self.assertEqual(specification.reference_wavelength, 2)
        self.assertEqual(specification.field_angles_y, [0.0, 10.0, 14.0])
        self.assertEqual(specification.vignetting, {"vuy": [0.0, 0.2, 0.4], "vly": [0.0, 0.3, 0.4]})

    def test_vignetting_cross_check(self):
        specification = self.listing.specification
        fields = [{"vux": 0.0, "vlx": 0.0, "vuy": a, "vly": b}
                  for a, b in ((0.0, 0.0), (0.2, 0.3), (0.4, 0.4))]
        self.assertEqual(vignetting_mismatches(specification, fields), [])
        fields[1]["vly"] = 0.30001
        self.assertIn("VLY items", vignetting_mismatches(specification, fields)[0])
        fields[1]["vly"] = 0.3
        fields[2]["vux"] = 0.1
        self.assertIn("VUX is not printed", vignetting_mismatches(specification, fields)[0])
        fields[2]["vux"] = None
        self.assertIn("could not be read", vignetting_mismatches(specification, fields)[0])
        self.assertIn("lists 3 values for 2 fields",
                      vignetting_mismatches(specification, fields[:2][:1] + fields[1:2])[1])

    def test_first_order_block(self):
        first_order = self.listing.first_order
        self.assertEqual(first_order.conjugate, "infinite")
        self.assertAlmostEqual(first_order.effective_focal_length, 100.0001)
        self.assertAlmostEqual(first_order.back_focal_length, 61.2346)
        self.assertAlmostEqual(first_order.front_focal_length, -29.1234)
        self.assertAlmostEqual(first_order.f_number, 2.0)
        self.assertAlmostEqual(first_order.image_distance, 61.2346)
        self.assertAlmostEqual(first_order.overall_length, 81.9134)
        self.assertAlmostEqual(first_order.paraxial_image_height, 23.4567)
        self.assertAlmostEqual(first_order.entrance_pupil_diameter, 50.0)
        self.assertAlmostEqual(first_order.entrance_pupil_distance, 53.1234)
        self.assertAlmostEqual(first_order.exit_pupil_diameter, 56.1234)
        self.assertAlmostEqual(first_order.exit_pupil_distance, -49.1234)

    def test_solves_and_pickups(self):
        self.assertEqual(self.listing.solves, ["PIM"])
        self.assertEqual(self.listing.pickups, [])
        self.assertTrue(self.listing.relation_data_complete)
        self.assertFalse(self.listing.has_pickups)

    def test_pickup_section_does_not_absorb_first_order_results(self):
        text = DBGAUSS_LISTING.replace(
            "No pickups defined in system",
            "PICKUPS\n   PIK RDY S1 Z1 RDY S2 Z1 1.000000 4.000000",
        )
        listing = parse_listing(text)
        self.assertEqual(listing.solves, ["PIM"])
        self.assertEqual(listing.pickups, ["PIK RDY S1 Z1 RDY S2 Z1 1.000000 4.000000"])
        self.assertTrue(listing.has_pickups)
        self.assertTrue(listing.relation_data_complete)

    def test_missing_pickup_status_is_an_incomplete_relation_read(self):
        text = DBGAUSS_LISTING.replace("No pickups defined in system", "")
        self.assertFalse(parse_listing(text).relation_data_complete)
        contradictory = DBGAUSS_LISTING.replace(
            "No pickups defined in system",
            "No pickups defined in system\nPICKUPS\n PIK RDY S1 Z1 RDY S2 Z1",
        )
        self.assertFalse(parse_listing(contradictory).relation_data_complete)

    def test_new_lens_explicitly_reports_no_solves(self):
        text = DBGAUSS_LISTING.replace("SOLVES\n    PIM", "No solves defined in system")
        listing = parse_listing(text)
        self.assertEqual(listing.solves, [])
        self.assertEqual(listing.pickups, [])
        self.assertTrue(listing.relation_data_complete)

    def test_fno_specified_system(self):
        text = DBGAUSS_LISTING.replace("    EPD       50.00000", "    FNO         2.00000")
        specification = parse_listing(text).specification
        self.assertEqual(specification.aperture_kind, "fno")
        self.assertAlmostEqual(specification.aperture_value, 2.0)

    def test_nao_is_not_collapsed_into_image_space_na(self):
        text = DBGAUSS_LISTING.replace("    EPD       50.00000", "    NAO         0.25000")
        specification = parse_listing(text).specification
        self.assertEqual(specification.aperture_kind, "nao")
        self.assertAlmostEqual(specification.aperture_value, 0.25)

    def test_explicit_apertures_keep_shape_type_and_decenter(self):
        block = """ APERTURE DATA/EDGE DEFINITIONS
    CA APE
    CIR S1                  16.797690
    CIR S1   OBS             4.479384
    ADX S1                   0.125000

"""
        text = DBGAUSS_LISTING.replace(" REFRACTIVE INDICES", block + " REFRACTIVE INDICES")
        listing = parse_listing(text)
        self.assertEqual(listing.aperture_usage, "user_only")
        self.assertTrue(listing.aperture_data_complete)
        self.assertEqual(len(listing.apertures), 2)
        clear, obscuration = listing.apertures
        self.assertEqual((clear.kind, clear.shape), ("clear", "circular"))
        self.assertAlmostEqual(clear.radius, 16.797690)
        self.assertAlmostEqual(clear.x_decenter, 0.125)
        self.assertEqual(obscuration.kind, "obscuration")

    def test_unknown_aperture_commands_fail_closed(self):
        block = """ APERTURE DATA/EDGE DEFINITIONS
    CA
    CIG S1 10 1 2 0

"""
        listing = parse_listing(
            DBGAUSS_LISTING.replace(" REFRACTIVE INDICES", block + " REFRACTIVE INDICES")
        )
        self.assertFalse(listing.aperture_data_complete)
        self.assertEqual(listing.aperture_unknown_lines, ["CIG S1 10 1 2 0"])

    def test_extra_native_aperture_operand_is_not_treated_as_a_radius(self):
        block = " APERTURE DATA/EDGE DEFINITIONS\n    CA\n    CIR S1 14 UNKNOWN\n\n"
        listing = parse_listing(
            DBGAUSS_LISTING.replace(" REFRACTIVE INDICES", block + " REFRACTIVE INDICES")
        )
        self.assertFalse(listing.aperture_data_complete)
        self.assertEqual(listing.apertures, [])
        self.assertEqual(listing.aperture_unknown_lines, ["CIR S1 14 UNKNOWN"])

    def test_native_zoom_aperture_rows_override_only_their_positions(self):
        text = DBGAUSS_LISTING.replace(
            " REFRACTIVE INDICES",
            " APERTURE DATA/EDGE DEFINITIONS\n    CA\n    CIR S1 10.000000\n\n REFRACTIVE INDICES",
        ).replace(
            " INFINITE CONJUGATES",
            " ZOOM DATA\n      POS 1      POS 2      POS 3\n    CIR S1 10.00000 20.00000 30.00000\n\n INFINITE CONJUGATES",
        )
        listing = parse_listing(text)
        self.assertTrue(listing.aperture_data_complete)
        self.assertEqual([listing.apertures_at(z)[0].radius for z in (1, 2, 3)], [10, 20, 30])
        self.assertEqual(listing.zoom_aperture_commands, ["CIR S1 10.00000 20.00000 30.00000"])

    def test_incomplete_zoom_aperture_row_fails_closed(self):
        text = DBGAUSS_LISTING.replace(
            " REFRACTIVE INDICES",
            " APERTURE DATA/EDGE DEFINITIONS\n    CA\n    CIR S1 10.000000\n\n REFRACTIVE INDICES",
        ).replace(
            " INFINITE CONJUGATES",
            " ZOOM DATA\n      POS 1      POS 2      POS 3\n    CIR S1 10.00000 20.00000\n\n INFINITE CONJUGATES",
        )
        listing = parse_listing(text)
        self.assertFalse(listing.aperture_data_complete)
        self.assertIsNone(listing.apertures_at(3)[0].radius)

    def test_empty_text_is_survivable(self):
        listing = parse_listing("")
        self.assertIsNone(listing.title)
        self.assertEqual(listing.surfaces, [])
        self.assertIsNone(listing.first_order.effective_focal_length)


class ParseSpotListing(unittest.TestCase):
    """Project-owned native SPO and synthetic failure cases."""

    def setUp(self) -> None:
        path = Path(__file__).parent / "data" / "project-owned" / "spot.txt"
        self.listing = parse_spot_listing(path.read_text(encoding="utf-8"))

    def test_finds_every_field(self):
        self.assertEqual([field.index for field in self.listing.fields], [1, 2, 3])
        self.assertEqual(self.listing.units, "MM")

    def test_reads_the_spot_sizes_and_the_ray_counts(self):
        first, second, third = self.listing.fields
        self.assertAlmostEqual(first.rms_diameter, 0.079174, places=6)
        self.assertAlmostEqual(first.hundred_diameter, 0.20941, places=5)
        self.assertEqual(first.rays, 1624)
        self.assertAlmostEqual(second.rms_diameter, 0.095694, places=6)
        self.assertAlmostEqual(second.hundred_diameter, 0.24345, places=5)
        self.assertEqual(second.rays, 1620)
        self.assertAlmostEqual(third.rms_diameter, 0.15258, places=5)
        self.assertAlmostEqual(third.hundred_diameter, 0.35518, places=5)
        self.assertEqual(third.rays, 1080)

    def test_reads_the_field_positions_and_the_centroid_displacements(self):
        first, second, third = self.listing.fields
        self.assertAlmostEqual(first.field_x, 0.0)
        self.assertAlmostEqual(first.field_y, 0.0)
        self.assertAlmostEqual(second.field_y, 3.0)
        self.assertAlmostEqual(third.field_y, 6.0)
        self.assertAlmostEqual(second.centroid_y, 0.11329e-01, places=9)
        self.assertAlmostEqual(second.hundred_center_y, 0.34435e-01, places=7)
        self.assertAlmostEqual(third.centroid_y, 0.22672e-01, places=7)
        self.assertAlmostEqual(first.centroid_x, 0.0)


if __name__ == "__main__":
    unittest.main()
