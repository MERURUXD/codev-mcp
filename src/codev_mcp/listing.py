"""Parser for the text that the CODE V "lis" command returns.

The database items read through EvaluateExpression are convenient but risky:
when an item name is not understood CODE V returns the previous result instead
of an error, so a wrong item can silently hand back a stale number. Parsing the
listing gives an independent source for the same quantities, and the COM backend
cross-checks the two before it reports lens data.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

KEY_VALUE = re.compile(r"^\s*([A-Z][A-Z0-9 ]*?)\s+(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)\s*$")
SURFACE_ROW = re.compile(r"^\s*(>?)\s*(OBJ|STO|IMG|\d+)\s*:")
NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?")
RMS_VALUE = re.compile(r"RMS\s*=\s*(-?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?)")
HUNDRED_VALUE = re.compile(r"100%\s*=\s*(-?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?)")
SPOT_UNITS = re.compile(r"([\d.]+E?[-+]?\d*)\s+(MM|CM|IN)\s*$")
SPOT_FIELD = re.compile(r"Field\s+(\d+),\s*\(\s*([-+]?[\d.]+),\s*([-+]?[\d.]+)\s*\)")
SPOT_RAYS = re.compile(r"(\d+)\s+Rays")
SPOT_VALUES = re.compile(
    r"X:\s*([-+]?[\d.E+-]+)\s+Y:\s*([-+]?[\d.E+-]+)\s+([-+]?[\d.E+-]+)\s*(MM|CM|IN)?"
)

APERTURE_KEYS = ("EPD", "FNO", "NAO", "NA")
APERTURE_COMMAND = re.compile(
    r"^\s*(CIR|REX|REY|ELX|ELY|ADX|ADY|ARO)\s+S(\d+)"
    r"(?:\s+(CLR|EDG|OBS|HOL))?(?:\s+L\s*'([^']{1,3})')?\s+(.+?)\s*$",
    re.IGNORECASE,
)
ORA_COMMAND = re.compile(
    r"^\s*ORA(?:\s+S(\d+))?(?:\s+L\s*'([^']{1,3})')?(?:\s+(CLR|EDG|OBS|HOL))?\s*$",
    re.IGNORECASE,
)


def parse_nominal_wavefront(text: str, expected_fields: int) -> dict:
    """Parse the verified CODE V 10.2 WAV NOM listing, failing closed on gaps."""
    if not all(marker in text for marker in (
        "W A V E F R O N T   A N A L Y S I S", "NUMBER OF RAYS",
        "FIELD                   RMS", "WEIGHTED RMS", "Command End:",
    )) or "BEST INDIVIDUAL FOCUS" in text:
        raise ValueError("WAV nominal-focus listing is missing or incomplete")
    rays: list[int] = []
    values: list[tuple[float, float]] = []
    field_coordinates_deg: list[tuple[float, float]] = []
    wavelengths: list[float] = []
    weighted: tuple[float, float] | None = None
    position: int | None = None
    pending_x: float | None = None
    for line in text.splitlines():
        if "POSITION" in line and position is None:
            match = re.search(r"\bPOSITION\s+(\d+)\s*$", line)
            if match:
                position = int(match.group(1))
        if "NUMBER OF RAYS" in line:
            rays = [int(token) for token in line.split("NUMBER OF RAYS", 1)[1].split()]
        elif "WAVELENGTHS" in line:
            wavelengths = [float(token) for token in line.split("WAVELENGTHS", 1)[1].split()]
        elif re.match(r"^\s*X\s+[-+\d.]", line):
            tokens = line.split()
            if len(tokens) != 4 or pending_x is not None:
                raise ValueError("Malformed or unpaired WAV X field row")
            pending_x = float(tokens[2])
        elif re.match(r"^\s*Y\s+[-+\d.]", line):
            tokens = line.split()
            if len(tokens) != 6 or pending_x is None:
                raise ValueError("Malformed or unpaired WAV Y field row")
            field_coordinates_deg.append((pending_x, float(tokens[2])))
            values.append((float(tokens[3]), float(tokens[5])))
            pending_x = None
        elif "WEIGHTED RMS" in line:
            tokens = line.split("WEIGHTED RMS", 1)[1].split()
            if len(tokens) != 2:
                raise ValueError("Malformed WAV weighted RMS row")
            weighted = (float(tokens[0]), float(tokens[1]))
    if (len(values) != expected_fields or len(field_coordinates_deg) != expected_fields
            or pending_x is not None or len(rays) != expected_fields
            or len(wavelengths) == 0 or weighted is None or position is None):
        raise ValueError("WAV field, ray, wavelength or composite data is incomplete")
    numeric = (wavelengths + [n for pair in values for n in pair]
               + [n for pair in field_coordinates_deg for n in pair] + list(weighted))
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("WAV contains a non-finite number")
    if any(ray <= 0 for ray in rays) or any(rms < 0 or not 0 <= strehl <= 1 for rms, strehl in values):
        raise ValueError("WAV contains an invalid field value")
    if weighted[0] < 0 or not 0 <= weighted[1] <= 1:
        raise ValueError("WAV contains an invalid composite value")
    return {"position": position, "rays": rays, "values": values,
            "field_coordinates_deg": field_coordinates_deg,
            "wavelengths_nm": wavelengths, "weighted": weighted}


FIE_WAVELENGTH = re.compile(r"^\s*WAVELENGTH\s+(\d+(?:\.\d+)?)\s+NM\s*$")
FIE_ROW = re.compile(r"^\s*" + r"\s+".join([r"(-?\d+\.\d+)"] * 7) + r"\s*$")


def parse_fie_distortion(text: str) -> dict:
    """Parse the verified CODE V 10.2 FIE reference-wavelength distortion table.

    Only the default rotationally symmetric listing is accepted: one wavelength
    table, angles in degrees, and eleven rows from the axis to relative field
    1.00. Chromatic (per-wavelength), full-field-display and object-height forms
    fail closed rather than being guessed.
    """
    if "ROTATIONALLY SYMMETRIC FIELD ABERRATIONS" not in text or "Command End:" not in text:
        raise ValueError("FIE rotationally symmetric listing is missing or incomplete")
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines)
               if "RELATIVE" in line and "ANGLE" in line and "DISTORTION" in line]
    if len(headers) != 1:
        raise ValueError("FIE listing needs exactly one distortion table")
    header = headers[0]
    if (header + 1 >= len(lines) or "FIELD HEIGHT" not in lines[header + 1]
            or "(DEG)" not in lines[header + 1] or "(PER CENT)" not in lines[header + 1]):
        raise ValueError("FIE distortion columns are not relative field, degrees and per cent")
    wavelength = [FIE_WAVELENGTH.match(line) for line in lines[:header]]
    wavelength = [match for match in wavelength if match]
    if not wavelength:
        raise ValueError("FIE distortion table has no wavelength line")
    rows = []
    for line in lines[header + 2:]:
        if not line.strip():
            if rows:
                break
            continue
        match = FIE_ROW.match(line)
        if not match:
            raise ValueError("Malformed FIE distortion row")
        tokens = match.groups()
        rows.append({"relative_field": float(tokens[0]), "angle_deg": float(tokens[1]),
                     "distortion_percent": float(tokens[6]), "distortion_text": tokens[6]})
    expected = [round(0.1 * step, 2) for step in range(11)]
    if [row["relative_field"] for row in rows] != expected:
        raise ValueError("FIE distortion rows do not cover relative field 0.00 to 1.00")
    return {"wavelength_nm": float(wavelength[-1].group(1)),
            "wavelength_text": wavelength[-1].group(1), "rows": rows}


#: LIS prints vignetting factors with five decimals.
VIGNETTING_LISTING_TOLERANCE = 5e-6 + 1e-9


def vignetting_mismatches(specification: "Specification", fields: list[dict[str, float | None]]) -> list[str]:
    """Compare database-item vignetting factors with the printed LIS rows.

    ``fields`` holds one ``{vux, vlx, vuy, vly}`` mapping per field, in field
    order. A printed row must list every field; a row that is not printed must
    correspond to factors that are all zero. An unknown database item echoes the
    previous result, so this is the check that the items were really read.
    """
    problems = []
    for key in ("vux", "vlx", "vuy", "vly"):
        values = [row.get(key) for row in fields]
        printed = specification.vignetting.get(key)
        if any(value is None for value in values):
            problems.append(f"{key.upper()} could not be read for every field")
        elif printed is None:
            if any(abs(value) > VIGNETTING_LISTING_TOLERANCE for value in values):
                problems.append(f"{key.upper()} is not printed but the items read {values}")
        elif len(printed) != len(values):
            problems.append(f"{key.upper()} row lists {len(printed)} values for {len(values)} fields")
        elif any(abs(a - b) > VIGNETTING_LISTING_TOLERANCE for a, b in zip(printed, values)):
            problems.append(f"{key.upper()} items {values} differ from the listing {printed}")
    return problems


def ordinary_surface_rows(text: str, surface_count: int) -> None:
    """Reject a native surface section with unmodelled shape or position data.

    The RMD column and any continuation line (aspheric coefficients, decenters,
    special surface types) mean the surface is not the plain sphere or plane
    that the public lens data describes.
    """
    lines = text.splitlines()
    header_index = next((i for i, line in enumerate(lines) if all(
        token in line for token in ("RDY", "THI", "RMD", "GLA", "CCY", "THC", "GLC")
    )), None)
    if header_index is None:
        raise ValueError("the native surface-column header is missing")
    spec_index = next((i for i in range(header_index + 1, len(lines))
                       if "SPECIFICATION DATA" in lines[i]), None)
    if spec_index is None:
        raise ValueError("the native surface section has no terminator")
    header = lines[header_index]
    rmd_start = header.index("RMD")
    rmd_end = (rmd_start + header.index("GLA")) // 2
    row = re.compile(r"^\s*>?\s*(?:OBJ|STO|IMG|\d+)\s*:")
    count = 0
    for line in lines[header_index + 1:spec_index]:
        if not line.strip():
            continue
        if not row.match(line) or line[rmd_start:rmd_end].strip():
            raise ValueError("special surface data is present in the native listing")
        count += 1
    if count != surface_count:
        raise ValueError("the native surface listing is incomplete")


@dataclass
class ListingSurface:
    """One row of the surface listing."""

    label: str
    radius_text: str
    thickness_text: str
    glass: str | None
    radius: float | None
    thickness: float | None


@dataclass
class Specification:
    aperture_kind: str | None = None
    aperture_value: float | None = None
    dimension: str | None = None
    wavelengths_nm: list[float] = field(default_factory=list)
    reference_wavelength: int | None = None
    field_angles_y: list[float] = field(default_factory=list)
    field_angles_x: list[float] = field(default_factory=list)
    field_kind: str | None = None
    #: VUX/VLX/VUY/VLY rows as printed; CODE V leaves out a row whose factors
    #: are all zero, so an absent key means "not printed", not "unknown".
    vignetting: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class ListingAperture:
    surface: int
    kind: str = "clear"
    shape: str = "unknown"
    label: str | None = None
    radius: float | None = None
    x_semi_aperture: float | None = None
    y_semi_aperture: float | None = None
    x_decenter: float = 0.0
    y_decenter: float = 0.0
    rotation_degrees: float = 0.0
    or_with_previous: bool = False
    raw_commands: list[str] = field(default_factory=list)
    zoom_parameters: dict[str, dict[int, float]] = field(default_factory=dict)


@dataclass
class FirstOrder:
    effective_focal_length: float | None = None
    back_focal_length: float | None = None
    front_focal_length: float | None = None
    f_number: float | None = None
    image_distance: float | None = None
    overall_length: float | None = None
    paraxial_image_height: float | None = None
    angle: float | None = None
    entrance_pupil_diameter: float | None = None
    entrance_pupil_distance: float | None = None
    exit_pupil_diameter: float | None = None
    exit_pupil_distance: float | None = None
    conjugate: str | None = None


@dataclass
class Listing:
    title: str | None = None
    surfaces: list[ListingSurface] = field(default_factory=list)
    specification: Specification = field(default_factory=Specification)
    first_order: FirstOrder = field(default_factory=FirstOrder)
    solves: list[str] = field(default_factory=list)
    pickups: list[str] = field(default_factory=list)
    has_pickups: bool = False
    relation_data_complete: bool = False
    aperture_usage: str = "unknown"
    apertures: list[ListingAperture] = field(default_factory=list)
    aperture_data_complete: bool = True
    aperture_unknown_lines: list[str] = field(default_factory=list)
    aperture_commands: list[str] = field(default_factory=list)
    zoom_aperture_commands: list[str] = field(default_factory=list)
    zoom_data_positions: int | None = None

    def apertures_at(self, position: int) -> list[ListingAperture]:
        result = []
        for entry in self.apertures:
            item = replace(entry)
            if not self.aperture_data_complete:
                # A partial zoom row must not make the first-position number
                # appear to be a verified value at another position.
                item.radius = None
                item.x_semi_aperture = None
                item.y_semi_aperture = None
                result.append(item)
                continue
            for command, values in entry.zoom_parameters.items():
                value = values.get(position)
                if value is None:
                    continue
                if command == "CIR":
                    item.radius = item.x_semi_aperture = item.y_semi_aperture = value
                elif command in {"REX", "ELX"}:
                    item.x_semi_aperture = value
                elif command in {"REY", "ELY"}:
                    item.y_semi_aperture = value
                elif command == "ADX":
                    item.x_decenter = value
                elif command == "ADY":
                    item.y_decenter = value
                elif command == "ARO":
                    item.rotation_degrees = value
            result.append(item)
        return result


@dataclass
class SpotFieldStatistic:
    """Spot size data for one field, as printed by the SPO option."""

    index: int
    field_x: float | None = None
    field_y: float | None = None
    centroid_x: float | None = None
    centroid_y: float | None = None
    rms_diameter: float | None = None
    hundred_diameter: float | None = None
    hundred_center_x: float | None = None
    hundred_center_y: float | None = None
    rays: int | None = None


@dataclass
class SpotListing:
    """Spot diagram statistics parsed from the SPO option output."""

    fields: list[SpotFieldStatistic] = field(default_factory=list)
    units: str | None = None
    defocus: float | None = None
    raw: str = ""


def _float(text: str) -> float | None:
    match = NUMBER.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _section(text: str, header: str, stop_headers: tuple[str, ...]) -> list[str]:
    """Return the lines of a section, excluding the header itself."""
    lines = text.splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside:
            if stripped.startswith(header):
                inside = True
            continue
        if any(stripped.startswith(stop) for stop in stop_headers):
            break
        collected.append(line)
    return collected


def parse_listing(text: str) -> Listing:
    listing = Listing()
    lines = text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("RDY", "! ", "Error:", "Command End", "File ", "Lens title")):
            continue
        listing.title = stripped
        break

    listing.surfaces = _parse_surfaces(text)
    listing.specification = _parse_specification(text)
    listing.first_order = _parse_first_order(text)
    (
        listing.aperture_usage,
        listing.apertures,
        listing.aperture_data_complete,
        listing.aperture_unknown_lines,
        listing.aperture_commands,
    ) = _parse_apertures(text)
    _parse_zoom_apertures(text, listing)

    listing.solves, listing.pickups, listing.relation_data_complete = _parse_relations(lines)
    listing.has_pickups = bool(listing.pickups)
    return listing


def _parse_relations(lines: list[str]) -> tuple[list[str], list[str], bool]:
    """Preserve the native solve/pickup commands, stopping before first order data.

    A missing section is different from an empty section. Checkpoint reads must
    refuse an incomplete LIS instead of treating missing relationships as none.
    Unknown command rows remain verbatim for equality checks and are excluded
    from future structural editing by its separate eligibility guard.
    """
    stripped = [line.strip() for line in lines]
    starts = [index for index, line in enumerate(stripped)
              if line in {"SOLVES", "No solves defined in system"}]
    if len(starts) != 1:
        return [], [], False
    solves: list[str] = []
    pickups: list[str] = []
    section = "solves" if stripped[starts[0]] == "SOLVES" else "no_solves"
    ended = False
    pickup_status_seen = False
    for line in stripped[starts[0] + 1:]:
        if not line:
            continue
        if line == "No pickups defined in system":
            if pickup_status_seen:
                return solves, pickups, False
            pickup_status_seen = True
            section = "none"
            continue
        if line == "PICKUPS":
            if pickup_status_seen:
                return solves, pickups, False
            pickup_status_seen = True
            section = "pickups"
            continue
        if line.startswith(("ZOOM DATA", "INFINITE CONJUGATES", "FINITE CONJUGATES", "Command End:")):
            ended = True
            break
        if section == "solves":
            solves.append(" ".join(line.split()))
        elif section == "no_solves":
            return solves, pickups, False
        elif section == "pickups":
            pickups.append(" ".join(line.split()))
        else:
            return solves, pickups, False
    return solves, pickups, ended and pickup_status_seen and section in {"none", "pickups"} and (
        section != "pickups" or bool(pickups)
    )


def _parse_zoom_apertures(text: str, listing: Listing) -> None:
    """Overlay native ZOOM DATA aperture rows; never assume Z1 is shared."""
    block = _section(text, "ZOOM DATA", ("INFINITE CONJUGATES", "FINITE CONJUGATES", "Command End"))
    if not block:
        return
    header = next((line for line in block if re.search(r"\bPOS\s+1\b", line)), None)
    positions = [int(value) for value in re.findall(r"\bPOS\s+(\d+)\b", header or "")]
    if not positions or positions != list(range(1, len(positions) + 1)):
        listing.aperture_data_complete = False
        listing.aperture_unknown_lines.append("ZOOM DATA position header is missing or invalid")
    else:
        listing.zoom_data_positions = len(positions)
    for line in block:
        stripped = line.strip()
        if not re.match(r"^(CIR|REX|REY|ELX|ELY|ADX|ADY|ARO|ORA|CIG)\b", stripped, re.I):
            continue
        listing.zoom_aperture_commands.append(stripped)
        match = APERTURE_COMMAND.match(stripped)
        if not match or not positions:
            listing.aperture_data_complete = False
            listing.aperture_unknown_lines.append(stripped)
            continue
        command, surface_text, kind_text, label, values_text = match.groups()
        values = values_text.split()
        if len(values) != len(positions) or any(NUMBER.fullmatch(value) is None for value in values):
            listing.aperture_data_complete = False
            listing.aperture_unknown_lines.append(stripped)
            continue
        kind = _aperture_kind(kind_text)
        candidates = [entry for entry in listing.apertures
                      if entry.surface == int(surface_text) and entry.kind == kind and entry.label == label]
        if len(candidates) != 1 or command.upper() in candidates[0].zoom_parameters:
            listing.aperture_data_complete = False
            listing.aperture_unknown_lines.append(stripped)
            continue
        candidates[0].zoom_parameters[command.upper()] = dict(zip(positions, map(float, values)))


def _aperture_kind(token: str | None) -> str:
    return {
        None: "clear",
        "CLR": "clear",
        "OBS": "obscuration",
        "EDG": "edge",
        "HOL": "hole",
    }[(token or "CLR").upper()]


def _parse_apertures(
    text: str,
) -> tuple[str, list[ListingAperture], bool, list[str], list[str]]:
    """Parse the native aperture command block without inventing defaults."""
    block = _section(
        text,
        "APERTURE DATA/EDGE DEFINITIONS",
        ("REFRACTIVE INDICES", "SOLVES", "INFINITE CONJUGATES", "FINITE CONJUGATES", "Command End"),
    )
    # CODE V omits the CA line for CA NO, whose native meaning is to use
    # default (automatically calculated) apertures. An absent aperture block
    # is therefore a complete empty set of explicit definitions.
    if not block:
        return "default_only", [], True, [], []
    usage = "default_only"
    entries: list[ListingAperture] = []
    unknown: list[str] = []

    def matching(surface: int, kind: str, label: str | None) -> ListingAperture | None:
        for entry in reversed(entries):
            if entry.surface == surface and entry.kind == kind and entry.label == label:
                return entry
        return None

    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "CA":
            usage = "user_and_default"
            continue
        if upper in {"CA N", "CA NO"}:
            usage = "default_only"
            continue
        if upper in {"CA A", "CA APE"}:
            usage = "user_only"
            continue
        ora = ORA_COMMAND.match(stripped)
        if ora:
            if ora.group(1) is None:
                target = entries[-1] if entries else None
            else:
                target = matching(
                    int(ora.group(1)), _aperture_kind(ora.group(3)), ora.group(2)
                )
            if target is None:
                unknown.append(stripped)
            else:
                target.or_with_previous = True
                target.raw_commands.append(stripped)
            continue
        match = APERTURE_COMMAND.match(stripped)
        if not match:
            unknown.append(stripped)
            continue
        command, surface_text, kind_text, label, value_text = match.groups()
        surface = int(surface_text)
        kind = _aperture_kind(kind_text)
        # An extra native operand may change the definition's meaning. Never
        # treat a numeric prefix as a complete, editable aperture command.
        if NUMBER.fullmatch(value_text.strip()) is None:
            unknown.append(stripped)
            continue
        value = float(value_text)
        command = command.upper()
        if command == "CIR":
            entry = ListingAperture(
                surface=surface,
                kind=kind,
                shape="circular",
                label=label,
                radius=value,
                x_semi_aperture=value,
                y_semi_aperture=value,
                raw_commands=[stripped],
            )
            entries.append(entry)
            continue
        if command in {"REX", "REY", "ELX", "ELY"}:
            shape = "rectangular" if command.startswith("RE") else "elliptical"
            entry = matching(surface, kind, label)
            if entry is None or entry.shape != shape:
                entry = ListingAperture(
                    surface=surface, kind=kind, shape=shape, label=label
                )
                entries.append(entry)
            if command.endswith("X"):
                entry.x_semi_aperture = value
            else:
                entry.y_semi_aperture = value
            entry.raw_commands.append(stripped)
            continue
        entry = matching(surface, kind, label)
        if entry is None:
            unknown.append(stripped)
            continue
        if command == "ADX":
            entry.x_decenter = value
        elif command == "ADY":
            entry.y_decenter = value
        else:
            entry.rotation_degrees = value
        entry.raw_commands.append(stripped)

    incomplete = any(
        entry.shape in {"rectangular", "elliptical"}
        and (entry.x_semi_aperture is None or entry.y_semi_aperture is None)
        for entry in entries
    )
    commands = [line.strip() for line in block if line.strip()]
    return usage, entries, not unknown and not incomplete, unknown, commands


def parse_spot_listing(text: str) -> SpotListing:
    """Parse the spot size annotations the SPO option prints.

    The real output looks like this, one block per field:

        Field  1, (  0.00,  0.00) degrees.  Focus  0.00000     1756 Rays
        Displacement of centroid                    Minimum RMS spot diameter
         X:   0.00000E+00     Y:   0.00000E+00         0.68900E-01 MM
        Displacement of center of 100% Spot         Minimum 100% spot diameter
         X:   0.00000E+00     Y:   0.00000E+00         0.15240E+00 MM

    Both sizes are diameters, which is why the caller reports them with
    size_is_radius true after halving them, and both displacements are measured
    from the chief ray at the reference wavelength.
    """
    listing = SpotListing(raw=text)
    current: SpotFieldStatistic | None = None
    expecting: str | None = None
    rms_values: list[float] = []
    hundred_values: list[float] = []
    for line in text.splitlines():
        field_match = SPOT_FIELD.search(line)
        if field_match:
            current = SpotFieldStatistic(
                index=int(field_match.group(1)),
                field_x=float(field_match.group(2)),
                field_y=float(field_match.group(3)),
            )
            rays_match = SPOT_RAYS.search(line)
            if rays_match:
                current.rays = int(rays_match.group(1))
            listing.fields.append(current)
            expecting = None
            continue
        if current is None:
            continue
        if "Displacement of centroid" in line:
            expecting = "rms"
            continue
        if "Displacement of center of 100%" in line:
            expecting = "hundred"
            continue
        values_match = SPOT_VALUES.match(line.strip())
        if values_match and expecting:
            first = float(values_match.group(1))
            second = float(values_match.group(2))
            size = float(values_match.group(3))
            if values_match.group(4):
                listing.units = values_match.group(4)
            if expecting == "rms":
                current.centroid_x = first
                current.centroid_y = second
                current.rms_diameter = size
            else:
                current.hundred_center_x = first
                current.hundred_center_y = second
                current.hundred_diameter = size
            expecting = None
            continue
        if listing.defocus is None and "DEFOCUSING" in line:
            listing.defocus = _float(line.split("DEFOCUSING", 1)[1])
        rms_match = RMS_VALUE.search(line)
        if rms_match:
            rms_values.append(float(rms_match.group(1)))
        hundred_match = HUNDRED_VALUE.search(line)
        if hundred_match:
            hundred_values.append(float(hundred_match.group(1)))
        if listing.units is None:
            units_match = SPOT_UNITS.search(line.strip())
            if units_match:
                listing.units = units_match.group(2)
        if listing.defocus is None and "DEFOCUSING" in line:
            listing.defocus = _float(line.split("DEFOCUSING", 1)[1])

    if not listing.fields:
        # Fallback for a listing that only carries the plain annotations, such
        # as the SPO descriptions printed in the reference manual.
        for index in range(max(len(rms_values), len(hundred_values))):
            listing.fields.append(
                SpotFieldStatistic(
                    index=index + 1,
                    rms_diameter=rms_values[index] if index < len(rms_values) else None,
                    hundred_diameter=(
                        hundred_values[index] if index < len(hundred_values) else None
                    ),
                )
            )
    return listing


def _parse_surfaces(text: str) -> list[ListingSurface]:
    surfaces: list[ListingSurface] = []
    for line in text.splitlines():
        match = SURFACE_ROW.match(line)
        if not match:
            continue
        label = match.group(2)
        body = line[match.end() :].strip()
        tokens = re.findall(r"INFINITY|[A-Za-z][A-Za-z0-9_.+-]*|-?\d+\.?\d*(?:[Ee][+-]?\d+)?", body)
        if len(tokens) < 2:
            continue
        radius_text, thickness_text = tokens[0], tokens[1]
        glass = None
        for token in tokens[2:]:
            if re.match(r"^[A-Za-z]", token) and token.upper() not in {"INFINITY", "REFL", "REFR"}:
                glass = token
                break
        surfaces.append(
            ListingSurface(
                label=label,
                radius_text=radius_text,
                thickness_text=thickness_text,
                glass=glass,
                radius=_float(radius_text),
                thickness=_float(thickness_text),
            )
        )
    return surfaces


def _parse_specification(text: str) -> Specification:
    specification = Specification()
    block = _section(
        text,
        "SPECIFICATION DATA",
        ("REFRACTIVE INDICES", "SOLVES", "APERTURE DATA", "Command End"),
    )
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        key = tokens[0].upper()
        if key in APERTURE_KEYS and specification.aperture_kind is None:
            specification.aperture_kind = key.lower()
            specification.aperture_value = _float(stripped[len(tokens[0]) :])
        elif key == "DIM":
            specification.dimension = stripped[len(tokens[0]) :].strip()
        elif key == "WL":
            specification.wavelengths_nm = [
                value for value in (_float(token) for token in tokens[1:]) if value is not None
            ]
        elif key == "REF":
            value = _float(stripped[len(tokens[0]) :])
            specification.reference_wavelength = int(value) if value is not None else None
        elif key == "YAN":
            specification.field_angles_y = [
                value for value in (_float(token) for token in tokens[1:]) if value is not None
            ]
        elif key == "XAN":
            specification.field_angles_x = [
                value for value in (_float(token) for token in tokens[1:]) if value is not None
            ]
            specification.field_kind = "angle"
        elif key in {"VUX", "VLX", "VUY", "VLY"}:
            specification.vignetting[key.lower()] = [
                value for value in (_float(token) for token in tokens[1:]) if value is not None
            ]
        elif key in {"YIM", "XIM"}:
            specification.field_kind = specification.field_kind or "image_height"
        elif key in {"YOB", "XOB"}:
            specification.field_kind = specification.field_kind or "object_height"
        elif key in {"YRI", "XRI"}:
            specification.field_kind = specification.field_kind or "reference_radius"
    return specification


def _parse_first_order(text: str) -> FirstOrder:
    first_order = FirstOrder()
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("INFINITE CONJUGATES", "FINITE CONJUGATES")):
            first_order.conjugate = stripped.split()[0].lower()
            start = index + 1
            break
    if start is None:
        return first_order

    pupil = None
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ENTRANCE PUPIL"):
            pupil = "entrance"
            continue
        if stripped.startswith("EXIT PUPIL"):
            pupil = "exit"
            continue
        if stripped.startswith(("PARAXIAL", "Command End", "REFRACTIVE")):
            continue
        match = KEY_VALUE.match(line)
        if not match:
            if stripped.startswith(("APERTURE", "SOLVES")):
                break
            continue
        key = match.group(1).strip()
        value = float(match.group(2))
        if key == "EFL":
            first_order.effective_focal_length = value
        elif key == "BFL":
            first_order.back_focal_length = value
        elif key == "FFL":
            first_order.front_focal_length = value
        elif key == "FNO":
            first_order.f_number = value
        elif key == "IMG DIS":
            first_order.image_distance = value
        elif key == "OAL":
            first_order.overall_length = value
        elif key == "HT":
            first_order.paraxial_image_height = value
        elif key == "ANG":
            first_order.angle = value
        elif key == "DIA":
            if pupil == "entrance":
                first_order.entrance_pupil_diameter = value
            elif pupil == "exit":
                first_order.exit_pupil_diameter = value
        elif key == "THI":
            if pupil == "entrance":
                first_order.entrance_pupil_distance = value
            elif pupil == "exit":
                first_order.exit_pupil_distance = value
    return first_order
