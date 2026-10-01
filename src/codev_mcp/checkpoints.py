"""Checkpoints of committed lens state.

The service promises that a successful ``update_lens`` call has been written to
a checkpoint it can restore from, so an engine that dies later does not lose
uncommitted work and never has to guess which file is the newest lens. This
module owns the files behind that promise and nothing else: no COM object is
held here, so it can be tested without CODE V.

Layout below ``<working-directory>/checkpoints``::

    <backend-id>/<lens-id>/revision-000000.len
    <backend-id>/<lens-id>/revision-000000.json
    <backend-id>/<lens-id>/current.json
    <backend-id>/<lens-id>/transactions/<transaction-id>.json
    <backend-id>/<lens-id>/restore-points/<transaction-id>.len

``current.json`` is the only file that is replaced in place, and it is replaced
atomically, so it is either the previous committed revision or the new one. A
candidate revision file only becomes a commit when ``current.json`` points at
it; nothing here ever picks a lens by file name or modification time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import SessionInvalidError

#: Version of the on disk metadata. A checkpoint that carries another version
#: is refused instead of being interpreted as the current format.
CHECKPOINT_FORMAT_VERSION = 6

CURRENT_POINTER_NAME = "current.json"
TRANSACTION_DIRECTORY = "transactions"
RESTORE_POINT_DIRECTORY = "restore-points"

#: Number of digits kept for the continuous values of a verification snapshot.
#: CODE V returns about sixteen significant digits; rounding here keeps a
#: save/resume round trip from showing up as a difference.
SNAPSHOT_DIGITS = 12

#: Tolerances for comparing two snapshots, matching the read-back tolerances of
#: the edit path.
ABSOLUTE_TOLERANCE = 1e-9
RELATIVE_TOLERANCE = 1e-7


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CheckpointError(SessionInvalidError):
    """A checkpoint is missing, damaged or inconsistent with itself."""


class CheckpointPublishError(CheckpointError):
    """A new revision could not be published; the previous one still stands."""


class CheckpointVerificationError(CheckpointError):
    """The state that came back from CODE V does not match the checkpoint."""


class UnexpectedChangeError(Exception):
    """CODE V changed more than the requested edits, so the batch is refused."""


def hash_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of a file, read in chunks so a large lens stays cheap."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalise_number(value: Any) -> float | None:
    """Round a continuous value so a save/resume round trip compares equal."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number") from None
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{value!r} is not finite")
    return round(number, SNAPSHOT_DIGITS) + 0.0


def normalise_glass(name: str | None, catalog: str | None = None) -> str | None:
    """Normalise a glass for comparison, keeping the catalog.

    ``LensData.glass`` already carries ``name_CATALOG``; a bare name from CODE V
    is combined with the catalog read next to it. Comparing only the part before
    the underscore would call two different glasses equal.
    """
    text = (name or "").strip()
    category = (catalog or "").strip()
    if not text:
        return None
    base = text.split("_")[0]
    if category:
        return f"{base}_{category}".upper()
    if "_" in text:
        return text.upper()
    return base.upper()


def describe_value(value: Any) -> str:
    """Short, stable rendering of a snapshot value for a message."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def numbers_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        first = float(left)
        second = float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)
    if math.isnan(first) or math.isnan(second):
        return False
    return abs(first - second) <= max(ABSOLUTE_TOLERANCE, abs(second) * RELATIVE_TOLERANCE)


# --------------------------------------------------------------- snapshots


@dataclass
class SurfaceApertureState:
    kind: str
    shape: str
    label: str | None = None
    radius: float | None = None
    x_semi_aperture: float | None = None
    y_semi_aperture: float | None = None
    x_decenter: float = 0.0
    y_decenter: float = 0.0
    rotation_degrees: float = 0.0
    or_with_previous: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "shape": self.shape,
            "label": self.label,
            "radius": self.radius,
            "x_semi_aperture": self.x_semi_aperture,
            "y_semi_aperture": self.y_semi_aperture,
            "x_decenter": self.x_decenter,
            "y_decenter": self.y_decenter,
            "rotation_degrees": self.rotation_degrees,
            "or_with_previous": self.or_with_previous,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SurfaceApertureState":
        return cls(
            kind=str(payload.get("kind") or "unknown"),
            shape=str(payload.get("shape") or "unknown"),
            label=payload.get("label"),
            radius=payload.get("radius"),
            x_semi_aperture=payload.get("x_semi_aperture"),
            y_semi_aperture=payload.get("y_semi_aperture"),
            x_decenter=float(payload.get("x_decenter") or 0.0),
            y_decenter=float(payload.get("y_decenter") or 0.0),
            rotation_degrees=float(payload.get("rotation_degrees") or 0.0),
            or_with_previous=bool(payload.get("or_with_previous")),
        )


@dataclass
class SurfaceState:
    number: int
    role: str | None = None
    is_stop: bool = False
    radius: float | None = None
    radius_infinite: bool = False
    thickness: float | None = None
    thickness_infinite: bool = False
    glass: str | None = None
    catalog: str | None = None
    apertures: list[SurfaceApertureState] = field(default_factory=list)
    aperture_data_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "role": self.role,
            "is_stop": self.is_stop,
            "radius": self.radius,
            "radius_infinite": self.radius_infinite,
            "thickness": self.thickness,
            "thickness_infinite": self.thickness_infinite,
            "glass": self.glass,
            "catalog": self.catalog,
            "apertures": [item.to_dict() for item in self.apertures],
            "aperture_data_complete": self.aperture_data_complete,
        }


#: Field vignetting factors kept in a snapshot since format 6.
VIGNETTING_NAMES = ("vux", "vlx", "vuy", "vly")


@dataclass
class FieldState:
    number: int
    x_angle: float | None = None
    y_angle: float | None = None
    weight: float | None = None
    vux: float | None = None
    vlx: float | None = None
    vuy: float | None = None
    vly: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "x_angle": self.x_angle,
            "y_angle": self.y_angle,
            "weight": self.weight,
            **{name: getattr(self, name) for name in VIGNETTING_NAMES},
        }


@dataclass
class WavelengthState:
    number: int
    micrometers: float | None = None
    weight: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "micrometers": self.micrometers,
            "weight": self.weight,
        }


@dataclass
class ZoomState:
    position: int
    surfaces: list[SurfaceState] = field(default_factory=list)
    fields: list[FieldState] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "surfaces": [surface.to_dict() for surface in self.surfaces],
            "fields": [item.to_dict() for item in self.fields],
        }


@dataclass
class LensSnapshot:
    """Readable lens state used to verify a checkpoint or a rollback.

    Deliberately narrower than ``LensData``: paths, titles, images and raw
    listings are left out so two reads of the same lens compare equal, while
    everything the service can reliably read about the optical prescription is
    kept and compared element by element.
    """

    units: str | None = None
    dimension_code: int | None = None
    stop_surface: int | None = None
    surface_count: int | None = None
    zoom_positions: int | None = None
    reference_wavelength: int | None = None
    aperture_kind: str | None = None
    aperture_value: float | None = None
    aperture_values: dict[str, float | None] = field(default_factory=dict)
    aperture_usage: str = "unknown"
    aperture_data_complete: bool = True
    aperture_commands: list[str] = field(default_factory=list)
    zoom_aperture_commands: list[str] = field(default_factory=list)
    solves: list[str] = field(default_factory=list)
    pickups: list[str] = field(default_factory=list)
    relation_data_complete: bool = False
    variable_controls: dict[str, dict[str, str]] = field(default_factory=dict)
    glass_catalogs: dict[str, str] = field(default_factory=dict)
    zooms: list[ZoomState] = field(default_factory=list)
    wavelengths: list[WavelengthState] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": self.units,
            "dimension_code": self.dimension_code,
            "stop_surface": self.stop_surface,
            "surface_count": self.surface_count,
            "zoom_positions": self.zoom_positions,
            "reference_wavelength": self.reference_wavelength,
            "aperture_kind": self.aperture_kind,
            "aperture_value": self.aperture_value,
            "aperture_values": dict(sorted(self.aperture_values.items())),
            "aperture_usage": self.aperture_usage,
            "aperture_data_complete": self.aperture_data_complete,
            "aperture_commands": list(self.aperture_commands),
            "zoom_aperture_commands": list(self.zoom_aperture_commands),
            "solves": list(self.solves),
            "pickups": list(self.pickups),
            "relation_data_complete": self.relation_data_complete,
            "variable_controls": self.variable_controls,
            "glass_catalogs": dict(sorted(self.glass_catalogs.items())),
            "zooms": [zoom.to_dict() for zoom in self.zooms],
            "wavelengths": [item.to_dict() for item in self.wavelengths],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LensSnapshot":
        zooms = []
        for zoom in payload.get("zooms") or []:
            zooms.append(
                ZoomState(
                    position=int(zoom.get("position") or 1),
                    surfaces=[
                        SurfaceState(
                            number=int(row.get("number") or 0),
                            role=row.get("role"),
                            is_stop=bool(row.get("is_stop")),
                            radius=row.get("radius"),
                            radius_infinite=bool(row.get("radius_infinite")),
                            thickness=row.get("thickness"),
                            thickness_infinite=bool(row.get("thickness_infinite")),
                            glass=row.get("glass"),
                            catalog=row.get("catalog"),
                            apertures=[
                                SurfaceApertureState.from_dict(item)
                                for item in row.get("apertures") or []
                            ],
                            aperture_data_complete=bool(
                                row.get("aperture_data_complete", True)
                            ),
                        )
                        for row in zoom.get("surfaces") or []
                    ],
                    fields=[
                        FieldState(
                            number=int(row.get("number") or 0),
                            x_angle=row.get("x_angle"),
                            y_angle=row.get("y_angle"),
                            weight=row.get("weight"),
                            **{name: row.get(name) for name in VIGNETTING_NAMES},
                        )
                        for row in zoom.get("fields") or []
                    ],
                )
            )
        return cls(
            units=payload.get("units"),
            dimension_code=payload.get("dimension_code"),
            stop_surface=payload.get("stop_surface"),
            surface_count=payload.get("surface_count"),
            zoom_positions=payload.get("zoom_positions"),
            reference_wavelength=payload.get("reference_wavelength"),
            aperture_kind=payload.get("aperture_kind"),
            aperture_value=payload.get("aperture_value"),
            aperture_values=dict(payload.get("aperture_values") or {}),
            aperture_usage=str(payload.get("aperture_usage") or "unknown"),
            aperture_data_complete=bool(payload.get("aperture_data_complete", True)),
            aperture_commands=list(payload.get("aperture_commands") or []),
            zoom_aperture_commands=list(payload.get("zoom_aperture_commands") or []),
            solves=list(payload.get("solves") or []),
            pickups=list(payload.get("pickups") or []),
            relation_data_complete=bool(payload.get("relation_data_complete", False)),
            variable_controls={
                str(number): {str(key): str(value) for key, value in row.items()}
                for number, row in (payload.get("variable_controls") or {}).items()
            },
            glass_catalogs=dict(payload.get("glass_catalogs") or {}),
            zooms=zooms,
            wavelengths=[
                WavelengthState(
                    number=int(row.get("number") or 0),
                    micrometers=row.get("micrometers"),
                    weight=row.get("weight"),
                )
                for row in payload.get("wavelengths") or []
            ],
        )


def _surface_values(surface: Any, catalog: Any = None) -> tuple[float | None, bool, float | None, bool, str | None]:
    """Normalised radius/thickness/glass of one surface read."""
    radius_infinite = bool(getattr(surface, "radius_is_infinite", False))
    radius = None if radius_infinite else normalise_number(getattr(surface, "radius", None))
    thickness_infinite = bool(getattr(surface, "thickness_is_infinite", False))
    thickness = None if thickness_infinite else normalise_number(getattr(surface, "thickness", None))
    glass = normalise_glass(getattr(surface, "glass", None), catalog)
    return radius, radius_infinite, thickness, thickness_infinite, glass


def parse_variable_controls(text: str, surface_count: int) -> dict[str, dict[str, str]]:
    """Read native CCY/THC/GLC columns without treating absent columns as frozen."""
    lines = text.splitlines()
    for index, header in enumerate(lines):
        if all(token in header for token in ("RDY", "THI", "CCY", "THC", "GLC")):
            starts = [header.index(name) for name in ("CCY", "THC", "GLC")]
            if not starts[0] < starts[1] < starts[2]:
                return {}
            rows: dict[str, dict[str, str]] = {}
            for line in lines[index + 1 :]:
                if len(rows) == surface_count:
                    break
                if not re.match(r"^\s*>?\s*(?:OBJ|STO|IMG|\d+)\s*:", line):
                    break
                values = (line[starts[0]:starts[1]].strip(),
                          line[starts[1]:starts[2]].strip(),
                          line[starts[2]:].strip())
                rows[str(len(rows))] = dict(zip(("CCY", "THC", "GLC"), values))
            return rows if len(rows) == surface_count else {}
    return {}


def read_snapshot(session: Any, lens: Any, listing: Any = None) -> LensSnapshot:
    """Read the verifiable state of the lens open in a live session.

    Every zoom position is read: a checkpoint that only covered the current zoom
    position could restore a lens whose other positions had changed. A read that
    fails or a truncated listing raises, because an incomplete read must never
    make two different lenses compare equal.
    """
    listing_text = getattr(session, "command", lambda _text: "")("lis")
    if hasattr(session, "output_is_truncated") and session.output_is_truncated(listing_text):
        raise CheckpointError(
            "The lens listing filled the CODE V text buffer, so the state could not be "
            "verified completely.",
            hint="Reduce the lens size or raise the text buffer before retrying.",
        )
    if listing_text:
        from .listing import parse_listing

        listing = parse_listing(listing_text)

    surface_count = int(session.get_surface_count())
    zoom_positions = int(session.get_zoom_count())
    if zoom_positions > 1 and (listing is None or listing.zoom_data_positions != zoom_positions):
        raise CheckpointError(
            "The native ZOOM DATA does not cover every zoom position, so surface apertures cannot be checkpointed safely.",
            hint="Reopen a lens whose complete native listing can be read.",
        )
    if listing is None or not listing.aperture_data_complete:
        raise CheckpointError(
            "The native surface aperture definition is incomplete, so the lens state cannot be checkpointed safely.",
            details={"unknown_lines": (listing.aperture_unknown_lines[:5] if listing else [])},
        )
    if not listing.relation_data_complete:
        raise CheckpointError(
            "The native solve or pickup section is incomplete, so the lens state cannot be checkpointed safely."
        )
    field_count = int(session.get_field_count())
    wavelength_count = int(session.get_wavelength_count())
    dimension = int(session.get_dimension())
    stop_surface = int(session.get_stop_surface())

    try:
        reference = int(float(session.evaluate("(REF)")))
    except Exception as exc:  # noqa: BLE001 - an unreadable item cannot be verified
        raise CheckpointError(
            "The reference wavelength could not be read while verifying the lens state.",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc

    catalogs: dict[str, str] = {}
    #: Which database items really carry a zoom qualifier. CODE V accepts a zoom
    #: qualified read for a parameter that was never zoomed, ignores the
    #: qualifier and retries the read for it to be sure.
    zoomed: dict[str, bool] = {}

    def is_zoomed(item: str) -> bool:
        if item not in zoomed:
            if zoom_positions <= 1:
                zoomed[item] = False
            else:
                values = [session.evaluate(f"({item} Z{number})") for number in range(1, zoom_positions + 1)]
                zoomed[item] = len(set(values)) > 1
        return zoomed[item]

    def qualified(item: str, position: int) -> str:
        return f"({item} Z{position})" if is_zoomed(item) else f"({item})"

    zooms: list[ZoomState] = []
    for zoom in range(1, zoom_positions + 1):
        parsed_apertures: dict[int, list[SurfaceApertureState]] = {}
        if listing is not None:
            for entry in listing.apertures_at(zoom):
                parsed_apertures.setdefault(entry.surface, []).append(
                    SurfaceApertureState(
                        kind=entry.kind,
                        shape=entry.shape,
                        label=entry.label,
                        radius=normalise_number(entry.radius),
                        x_semi_aperture=normalise_number(entry.x_semi_aperture),
                        y_semi_aperture=normalise_number(entry.y_semi_aperture),
                        x_decenter=normalise_number(entry.x_decenter) or 0.0,
                        y_decenter=normalise_number(entry.y_decenter) or 0.0,
                        rotation_degrees=normalise_number(entry.rotation_degrees) or 0.0,
                        or_with_previous=entry.or_with_previous,
                    )
                )
        surfaces = []
        for number in range(surface_count):
            radius = normalise_number(session.evaluate(qualified(f"RDY S{number}", zoom)))
            thickness = normalise_number(session.evaluate(qualified(f"THI S{number}", zoom)))
            radius_infinite = abs(radius) >= 1.0e17
            thickness_infinite = abs(thickness) >= 1.0e10
            glass_name = ""
            catalog = ""
            # A glass that cannot be read would compare as equal on both sides
            # of a verification, so a failed read raises and fails the check
            # instead of quietly turning the surface into air.
            glass_name = str(session.evaluate(f"(GLA S{number})")).strip()
            if glass_name:
                catalog_value = session.evaluate(f"(GLA S{number} CAT)")
                catalog = str(catalog_value).strip() if catalog_value is not None else ""
            if catalog:
                catalogs[str(number)] = catalog
            surfaces.append(
                SurfaceState(
                    number=number,
                    role=(lens.surfaces[number].role.value if number < len(lens.surfaces) else None),
                    is_stop=number == stop_surface,
                    radius=None if radius_infinite else radius,
                    radius_infinite=radius_infinite,
                    thickness=None if thickness_infinite else thickness,
                    thickness_infinite=thickness_infinite,
                    glass=normalise_glass(glass_name, catalog or catalogs.get(str(number))),
                    catalog=(catalog or catalogs.get(str(number)) or None),
                    apertures=list(parsed_apertures.get(number, [])),
                    aperture_data_complete=(
                        bool(listing.aperture_data_complete) if listing is not None else False
                    ),
                )
            )
        fields = []
        for number in range(1, field_count + 1):
            fields.append(
                FieldState(
                    number=number,
                    x_angle=normalise_number(session.evaluate(qualified(f"XAN F{number}", zoom))),
                    y_angle=normalise_number(session.evaluate(qualified(f"YAN F{number}", zoom))),
                    weight=normalise_number(session.evaluate(qualified(f"WTF F{number}", zoom))),
                    **{
                        name: normalise_number(
                            session.evaluate(qualified(f"{name.upper()} F{number}", zoom))
                        )
                        for name in VIGNETTING_NAMES
                    },
                )
            )
        zooms.append(ZoomState(position=zoom, surfaces=surfaces, fields=fields))

    # An unknown item echoes the previous result, so the factors are only trusted
    # when zoom position 1 agrees with the printed SPECIFICATION DATA rows.
    from .listing import vignetting_mismatches

    problems = vignetting_mismatches(listing.specification, [item.to_dict() for item in zooms[0].fields])
    if problems:
        raise CheckpointError(
            "The vignetting factors read from CODE V disagree with the native listing, "
            "so the lens state cannot be checkpointed safely.",
            details={"problems": problems[:5]},
        )

    wavelengths = []
    for number in range(1, wavelength_count + 1):
        micrometers = normalise_number(session.evaluate(f"(WL W{number})"))
        weight = normalise_number(session.evaluate(f"(WTW W{number})"))
        wavelengths.append(
            WavelengthState(
                number=number,
                micrometers=None if micrometers is None else micrometers / 1000.0,
                weight=weight,
            )
        )

    aperture_kind = None
    aperture_value = None
    aperture_values: dict[str, float | None] = {}
    if listing is not None:
        specification = listing.specification
        aperture_kind = specification.aperture_kind
    if aperture_kind not in {"epd", "fno", "na", "nao"}:
        raise CheckpointError(
            "The defining pupil specification was not present in the native lens listing, "
            "so the lens state cannot be checkpointed safely.",
            details={"aperture_kind": aperture_kind},
        )
    try:
        for zoom in range(1, zoom_positions + 1):
            aperture_values[str(zoom)] = normalise_number(
                session.evaluate(f"({aperture_kind.upper()} Z{zoom})")
            )
    except Exception as exc:  # noqa: BLE001 - a partial pupil read is not a snapshot
        raise CheckpointError(
            "The defining pupil value could not be read for every zoom position.",
            details={
                "aperture_kind": aperture_kind,
                "error": f"{type(exc).__name__}: {exc}",
            },
        ) from exc
    aperture_value = aperture_values.get("1")

    variable_controls = parse_variable_controls(listing_text, surface_count)
    if len(variable_controls) != surface_count or any(
        not row.get("CCY") or not row.get("THC")
        for row in variable_controls.values()
    ):
        raise CheckpointError(
            "The native CCY/THC/GLC variable-control columns could not be read completely.",
            details={"surface_count": surface_count, "parsed_rows": len(variable_controls)},
        )

    return LensSnapshot(
        units=DIMENSION_NAMES.get(dimension, f"code{dimension}"),
        dimension_code=dimension,
        stop_surface=stop_surface,
        surface_count=surface_count,
        zoom_positions=zoom_positions,
        reference_wavelength=reference,
        aperture_kind=aperture_kind,
        aperture_value=aperture_value,
        aperture_values=aperture_values,
        aperture_usage=(listing.aperture_usage if listing is not None else "unknown"),
        aperture_data_complete=(
            bool(listing.aperture_data_complete) if listing is not None else False
        ),
        aperture_commands=(list(listing.aperture_commands) if listing is not None else []),
        zoom_aperture_commands=(list(listing.zoom_aperture_commands) if listing is not None else []),
        solves=list(listing.solves),
        pickups=list(listing.pickups),
        relation_data_complete=True,
        variable_controls=variable_controls,
        glass_catalogs=catalogs,
        zooms=zooms,
        wavelengths=wavelengths,
    )


#: CODE V GetDimension values, kept as plain text so this module does not
#: depend on the public models.
DIMENSION_NAMES = {0: "inch", 1: "cm", 2: "mm"}


def _value_at(zoom: ZoomState, surface_number: int, name: str) -> Any:
    for surface in zoom.surfaces:
        if surface.number == surface_number:
            if name == "clear_aperture_radius":
                item = _editable_clear_aperture(surface)
                return None if item is None else item.radius
            return getattr(surface, name)
    return None


def _field_at(zoom: ZoomState, number: int, name: str) -> Any:
    for item in zoom.fields:
        if item.number == number:
            return getattr(item, name)
    return None


def _zoom_at(snapshot: LensSnapshot, position: int) -> ZoomState | None:
    for zoom in snapshot.zooms:
        if zoom.position == position:
            return zoom
    return None


def _editable_clear_aperture(surface: SurfaceState) -> SurfaceApertureState | None:
    """The one surface aperture M2 may edit, or None for every complex case."""
    if not surface.aperture_data_complete or len(surface.apertures) != 1:
        return None
    item = surface.apertures[0]
    if (
        item.kind != "clear"
        or item.shape != "circular"
        or item.radius is None
        or not numbers_equal(item.radius, item.x_semi_aperture)
        or not numbers_equal(item.radius, item.y_semi_aperture)
        or not numbers_equal(item.x_decenter, 0.0)
        or not numbers_equal(item.y_decenter, 0.0)
        or not numbers_equal(item.rotation_degrees, 0.0)
        or item.or_with_previous
    ):
        return None
    return item


_NATIVE_CLEAR_CIR = re.compile(
    r"^CIR\s+S(\d+)(?:\s+CLR)?(?:\s+L\s*'[^']{1,3}')?\s+"
    r"(-?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?)$",
    re.IGNORECASE,
)


def _mask_native_clear_radius(commands: list[str], permitted: set[int]) -> list[str]:
    """Ignore only the radius token of a uniquely identified requested CIR."""
    matches = [(_NATIVE_CLEAR_CIR.fullmatch(line.strip()), line) for line in commands]
    counts: dict[int, int] = {}
    for match, _line in matches:
        if match is not None:
            surface = int(match.group(1))
            counts[surface] = counts.get(surface, 0) + 1
    return [
        line[:match.start(2)] + "<edited radius>" + line[match.end(2):]
        if match is not None and int(match.group(1)) in permitted
        and counts[int(match.group(1))] == 1 else line
        for match, line in matches
    ]


def compare_snapshots(
    reference: LensSnapshot,
    actual: LensSnapshot,
    *,
    allowed_changes: list[tuple[str, int, int, str]] | None = None,
    expected_values: dict[tuple[str, int, int, str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Differences between two snapshots, element by element.

    A difference is only accepted when the caller asked for that exact
    parameter at that exact zoom position. ``expected_values`` carries the value
    the verified final state shows for such a parameter, so a change that went
    to the wrong value is still a difference. Everything else is reported, so an
    edit that silently moved a second parameter cannot pass as a success.
    """
    allowed = {tuple(entry) for entry in (allowed_changes or [])}
    values = expected_values or {}

    problems: list[dict[str, Any]] = []

    def report(where: str, expected_value: Any, actual_value: Any) -> None:
        problems.append(
            {
                "where": where,
                "detail": "changed",
                "expected": describe_value(expected_value),
                "actual": describe_value(actual_value),
            }
        )

    def requested(target: str, selector: int, zoom: int, parameter: str) -> bool:
        if (target, selector, zoom, parameter) not in allowed:
            return False
        expected_value = values.get((target, selector, zoom, parameter))
        return expected_value is None or numbers_equal(expected_value, _actual_value(
            actual, target, selector, zoom, parameter
        ))

    for name, label in (
        ("units", "units"),
        ("dimension_code", "dimension code"),
        ("stop_surface", "stop surface"),
        ("surface_count", "surface count"),
        ("zoom_positions", "zoom position count"),
        ("aperture_kind", "aperture type"),
        ("aperture_usage", "surface aperture usage mode"),
        ("aperture_data_complete", "surface aperture read completeness"),
    ):
        left, right = getattr(reference, name), getattr(actual, name)
        if left != right and not requested("lens", 0, 0, name):
            report(label, left, right)
    if reference.reference_wavelength != actual.reference_wavelength:
        # The reference wavelength is set through a wavelength edit, so the key
        # carries the wavelength number that was made the reference.
        selector = actual.reference_wavelength or reference.reference_wavelength or 0
        if not requested("wavelength", selector, 0, "is_reference"):
            report(
                "reference wavelength",
                reference.reference_wavelength,
                actual.reference_wavelength,
            )
    positions = sorted(set(reference.aperture_values) | set(actual.aperture_values), key=int)
    if not positions:
        positions = ["1"]
    for position_text in positions:
        position = int(position_text)
        left = reference.aperture_values.get(position_text, reference.aperture_value)
        right = actual.aperture_values.get(position_text, actual.aperture_value)
        if not numbers_equal(left, right) and not requested("aperture", 0, position, "value"):
            report(f"aperture value zoom {position}", left, right)
    permitted_cir: set[int] = set()
    if (reference.zoom_positions == actual.zoom_positions == 1
            and reference.aperture_usage in {"user_and_default", "user_only"}
            and actual.aperture_usage in {"user_and_default", "user_only"}):
        first = _zoom_at(reference, 1)
        second = _zoom_at(actual, 1)
        if first is not None and second is not None:
            for target, selector, zoom_position, parameter in allowed:
                if (target == "surface" and zoom_position == 1
                        and parameter == "clear_aperture_radius"
                        and _editable_clear_aperture(_surface(first, selector)) is not None
                        and _editable_clear_aperture(_surface(second, selector)) is not None):
                    permitted_cir.add(selector)
    if _mask_native_clear_radius(reference.aperture_commands, permitted_cir) != _mask_native_clear_radius(actual.aperture_commands, permitted_cir):
        report("native surface aperture commands", reference.aperture_commands, actual.aperture_commands)
    if reference.zoom_aperture_commands != actual.zoom_aperture_commands:
        report("native zoom aperture commands", reference.zoom_aperture_commands, actual.zoom_aperture_commands)
    if reference.relation_data_complete != actual.relation_data_complete:
        report("solve and pickup read completeness", reference.relation_data_complete,
               actual.relation_data_complete)
    if reference.solves != actual.solves:
        report("native solves", reference.solves, actual.solves)
    if reference.pickups != actual.pickups:
        report("native pickups", reference.pickups, actual.pickups)
    if reference.variable_controls != actual.variable_controls:
        report("native variable controls", reference.variable_controls,
               actual.variable_controls)

    for zoom in reference.zooms:
        other = _zoom_at(actual, zoom.position)
        if other is None:
            problems.append(
                {
                    "where": f"zoom {zoom.position}",
                    "detail": "missing",
                    "expected": describe_value(zoom.position),
                    "actual": "missing",
                }
            )
            continue
        for surface in zoom.surfaces:
            where = f"surface {surface.number} zoom {zoom.position}"
            counterpart = _surface(other, surface.number)
            for name, label in (("radius", "radius"), ("thickness", "thickness")):
                left, right = getattr(surface, name), getattr(counterpart, name)
                if requested("surface", surface.number, zoom.position, name):
                    continue
                if left is None or right is None:
                    if left != right:
                        report(f"{where} {label}", left, right)
                    continue
                if not numbers_equal(left, right):
                    report(f"{where} {label}", left, right)
            for name, label in (
                ("radius_infinite", "radius infinity flag"),
                ("thickness_infinite", "thickness infinity flag"),
                ("role", "role"),
                ("is_stop", "stop flag"),
                ("glass", "glass"),
            ):
                left, right = getattr(surface, name), getattr(counterpart, name)
                if left == right:
                    continue
                if requested("surface", surface.number, zoom.position, name):
                    continue
                report(f"{where} {label}", left, right)
            editable_change = requested(
                "surface", surface.number, zoom.position, "clear_aperture_radius"
            )
            left_apertures = [item.to_dict() for item in surface.apertures]
            right_apertures = [item.to_dict() for item in counterpart.apertures]
            if editable_change:
                left_editable = _editable_clear_aperture(surface)
                right_editable = _editable_clear_aperture(counterpart)
                if left_editable is not None and right_editable is not None:
                    for payload in (left_apertures[0], right_apertures[0]):
                        payload["radius"] = "editable"
                        payload["x_semi_aperture"] = "editable"
                        payload["y_semi_aperture"] = "editable"
            if left_apertures != right_apertures:
                report(f"{where} explicit apertures", left_apertures, right_apertures)
        for item in zoom.fields:
            where = f"field {item.number} zoom {zoom.position}"
            for name, label in (("x_angle", "x angle"), ("y_angle", "y angle"), ("weight", "weight"),
                                ("vux", "VUX vignetting"), ("vlx", "VLX vignetting"),
                                ("vuy", "VUY vignetting"), ("vly", "VLY vignetting")):
                left, right = getattr(item, name), _field_at(other, item.number, name)
                if numbers_equal(left, right):
                    continue
                if requested("field", item.number, zoom.position, name):
                    continue
                report(f"{where} {label}", left, right)

    for item in reference.wavelengths:
        counterpart = next(
            (row for row in actual.wavelengths if row.number == item.number), None
        )
        where = f"wavelength {item.number}"
        if counterpart is None:
            problems.append(
                {
                    "where": where,
                    "detail": "missing",
                    "expected": describe_value(item.number),
                    "actual": "missing",
                }
            )
            continue
        for name, label in (("micrometers", "value"), ("weight", "weight")):
            left, right = getattr(item, name), getattr(counterpart, name)
            if numbers_equal(left, right):
                continue
            if requested("wavelength", item.number, 0, name):
                continue
            report(f"{where} {label}", left, right)

    return problems

def _actual_value(
    snapshot: LensSnapshot, target: str, selector: int, zoom: int, parameter: str
) -> Any:
    """The value a parameter has in a snapshot, for the change check."""
    if target == "lens":
        return getattr(snapshot, parameter, None)
    return _final_value(snapshot, target, selector, zoom, parameter)

def _surface(zoom: ZoomState, number: int) -> SurfaceState:
    for surface in zoom.surfaces:
        if surface.number == number:
            return surface
    return SurfaceState(number=number)


def _final_value(
    snapshot: LensSnapshot, target: str, selector: int, zoom: int, parameter: str
) -> Any:
    """The value a parameter really has in a snapshot, for the change check."""
    zoom_state = _zoom_at(snapshot, zoom)
    if target == "surface" and zoom_state is not None:
        surface = _surface(zoom_state, selector)
        if parameter == "clear_aperture_radius":
            item = _editable_clear_aperture(surface)
            return None if item is None else item.radius
        return getattr(surface, parameter, None)
    if target == "field" and zoom_state is not None:
        return _field_at(zoom_state, selector, parameter)
    if target == "wavelength":
        if parameter == "is_reference":
            return snapshot.reference_wavelength
        item = next((row for row in snapshot.wavelengths if row.number == selector), None)
        return None if item is None else getattr(item, parameter, None)
    if target == "aperture":
        return snapshot.aperture_values.get(str(zoom), snapshot.aperture_value)
    return None


def solve_coupled_changes(
    reference: LensSnapshot,
    actual: LensSnapshot,
    candidates: list[tuple[str, int, int, str]],
) -> list[tuple[str, int, int, str]]:
    """Requested changes that a solve on that parameter re-derived.

    CODE V keeps a solved parameter consistent with the rest of the system, so
    editing one value can move a solved one. Those moves are allowed, but only
    when the parameter really changed and the caller confirmed that a solve
    controls it; everything else stays a difference the caller has to explain.
    """
    coupled: list[tuple[str, int, int, str]] = []
    for target, selector, zoom, parameter in candidates:
        before = _final_value(reference, target, selector, zoom, parameter)
        after = _final_value(actual, target, selector, zoom, parameter)
        if before is None or after is None:
            if before != after:
                coupled.append((target, selector, zoom, parameter))
            continue
        if parameter in {"radius", "thickness", "x_angle", "y_angle", "weight", "micrometers",
                         *VIGNETTING_NAMES}:
            if not numbers_equal(before, after):
                coupled.append((target, selector, zoom, parameter))
        elif before != after:
            coupled.append((target, selector, zoom, parameter))
    return coupled


def describe_changes(
    reference: LensSnapshot, actual: LensSnapshot, changes: list[tuple[str, int, int, str]]
) -> str:
    """One line naming the values a list of changes moved."""
    parts = []
    for target, selector, zoom, parameter in changes:
        before = _final_value(reference, target, selector, zoom, parameter)
        after = _final_value(actual, target, selector, zoom, parameter)
        if target == "surface":
            where = f"surface {selector}"
        elif target == "field":
            where = f"field {selector}"
        else:
            where = f"wavelength {selector}"
        parts.append(f"{where} {parameter} {describe_value(before)} -> {describe_value(after)}")
    return "; ".join(parts)

def change_expectations(
    final: LensSnapshot, requested: list[tuple[str, int, int, str]]
) -> dict[tuple[str, int, int, str], Any]:
    """What each requested change actually reads as in the final state.

    The caller names the parameters it asked CODE V to change; their real value
    comes from the verified final read, so the change check catches every other
    difference without having to guess how CODE V applied the request (a zoom
    qualified command on an unzoomed parameter still changes the shared value).
    """
    expected: dict[tuple[str, int, int, str], Any] = {}
    for target, selector, zoom, parameter in requested:
        key = (target, selector, zoom, parameter)
        expected[key] = _final_value(final, target, selector, zoom, parameter)
    return expected


def expand_allowed_changes(
    snapshot: LensSnapshot, requested: list[tuple[str, int, int, str]]
) -> list[tuple[str, int, int, str]]:
    """Cover every zoom position a requested change can really touch.

    CODE V applies a zoom qualified edit of an unzoomed parameter to the single
    shared value, so the other zoom positions change as well. The change check
    has to accept exactly that and nothing more.
    """
    expanded: list[tuple[str, int, int, str]] = []
    for entry in requested:
        target, selector, _zoom, parameter = entry
        if target in {"surface", "field"} and not parameter_varies_by_zoom(
            snapshot, target, selector, parameter
        ):
            for zoom in snapshot.zooms:
                expanded.append((target, selector, zoom.position, parameter))
        else:
            expanded.append(entry)
    return list(dict.fromkeys(expanded))


def with_radius_flags(
    allowed: list[tuple[str, int, int, str]]
) -> list[tuple[str, int, int, str]]:
    """Let a requested surface radius change its infinity flag (a plane becomes curved).

    The flag is a second view of the same value: the radius edit is still checked
    against the requested number, and a finite requested radius can never read back
    as infinite, so this only admits the plane -> curved change.
    """
    flags = [("surface", selector, zoom, "radius_infinite")
             for target, selector, zoom, parameter in allowed
             if target == "surface" and parameter == "radius"]
    return list(dict.fromkeys(allowed + flags))


def parameter_varies_by_zoom(
    snapshot: LensSnapshot, target: str, selector: int, parameter: str
) -> bool:
    """Whether a surface or field parameter really has a value per zoom position."""
    if snapshot is None:
        return True
    seen: list[Any] = []
    for zoom in snapshot.zooms:
        if target == "surface":
            seen.append(_value_at(zoom, selector, parameter))
        else:
            seen.append(_field_at(zoom, selector, parameter))
    if len(seen) <= 1:
        return False
    first = seen[0]
    return any(not numbers_equal(first, value) for value in seen[1:])


def summarise(problems: list[dict[str, Any]], limit: int = 5) -> str:
    """Compress comparison problems into one line for warnings and errors."""
    if not problems:
        return ""
    parts = [
        f"{problem['where']} {problem['detail']} (expected {problem['expected']}, read "
        f"{problem['actual']})"
        for problem in problems[:limit]
    ]
    if len(problems) > limit:
        parts.append(f"and {len(problems) - limit} more difference(s)")
    return "; ".join(parts)


def expected_value(edit: Any, zoom: Any, snapshot: LensSnapshot) -> Any:
    """Normalised snapshot value an edit is expected to leave behind."""
    position = getattr(edit, "zoom_position", None) or 1
    target = getattr(edit, "target", "surface")
    parameter = getattr(edit, "parameter", "")
    value = getattr(edit, "value", None)
    zoom_state = _zoom_at(snapshot, position)
    if target == "surface" and zoom_state is not None:
        if parameter == "glass":
            catalog = _surface(zoom_state, int(getattr(edit, "surface", 0) or 0)).catalog
            return normalise_glass(str(value), catalog)
        if parameter in {"radius", "thickness", "clear_aperture_radius"}:
            number = normalise_number(value)
            if parameter == "radius" and abs(number or 0.0) >= 1.0e17:
                return None
            if parameter == "thickness" and abs(number or 0.0) >= 1.0e10:
                return None
            return number
    if target == "aperture":
        return normalise_number(value)
    if target == "field" and zoom_state is not None:
        return normalise_number(value)
    if target == "wavelength":
        if parameter == "is_reference":
            return int(float(value))
        if parameter == "weight":
            return normalise_number(float(int(float(value))))
        if parameter == "micrometers":
            return normalise_number(float(value))
    return None


def collapse_expectations(
    expectations: list[tuple[Any, int]],
    snapshot: LensSnapshot | None = None,
) -> tuple[list[tuple[Any, int]], int]:
    """Keep the last edit of each parameter, the way CODE V applies them.

    A batch may edit the same parameter twice; only the last assignment decides
    the final value, so the earlier ones must not be required to hold at the same
    time. Merging uses the real parameter identity (see
    :func:`parameter_identity`), so a shared parameter edited at two zoom
    positions is one parameter while a genuinely zoomed one stays two. Returns
    the surviving expectations and how many were superseded.
    """
    last: dict[tuple[str, int, int, str], tuple[Any, int]] = {}
    order: list[tuple[str, int, int, str]] = []
    for edit, position in expectations:
        key = parameter_identity(edit, position, snapshot)
        if key not in last:
            order.append(key)
        last[key] = (edit, position)
    surviving = [last[key] for key in order]
    return surviving, len(expectations) - len(surviving)

def check_expected_values(
    snapshot: LensSnapshot, expectations: list[Any]
) -> list[dict[str, Any]]:
    """Compare the final state against the values the batch asked for.

    ``expectations`` is a list of ``(edit, zoom_position)`` pairs. A repeated edit
    of the same parameter is handled by the last entry winning, exactly as CODE V
    applies the commands in order.
    """
    problems: list[dict[str, Any]] = []
    for edit, position in expectations:
        target = getattr(edit, "target", "surface")
        parameter = getattr(edit, "parameter", "")
        where = describe_edit(edit)
        if target == "aperture":
            actual = snapshot.aperture_values.get(str(position), snapshot.aperture_value)
            expected = expected_value(edit, position, snapshot)
        elif target == "wavelength" and parameter == "is_reference":
            actual = snapshot.reference_wavelength
            expected = int(float(getattr(edit, "value", 0)))
        elif target == "wavelength":
            item = next(
                (row for row in snapshot.wavelengths if row.number == getattr(edit, "wavelength", None)),
                None,
            )
            actual = None if item is None else getattr(item, parameter, None)
            expected = expected_value(edit, position, snapshot)
        else:
            zoom_state = _zoom_at(snapshot, position)
            if zoom_state is None:
                problems.append(
                    {
                        "where": where,
                        "detail": "zoom position missing from the verification state",
                        "expected": describe_value(position),
                        "actual": "missing",
                    }
                )
                continue
            if target == "surface":
                actual = _value_at(zoom_state, getattr(edit, "surface", None), parameter)
            else:
                actual = _field_at(zoom_state, getattr(edit, "field", None), parameter)
            expected = expected_value(edit, position, snapshot)
        if parameter in {
            "radius",
            "thickness",
            "clear_aperture_radius",
            "value",
            "x_angle",
            "y_angle",
            "weight",
            "micrometers",
            *VIGNETTING_NAMES,
        }:
            if not numbers_equal(expected, actual):
                problems.append(
                    {
                        "where": where,
                        "detail": "did not reach the requested value",
                        "expected": describe_value(expected),
                        "actual": describe_value(actual),
                    }
                )
        elif expected != actual:
            problems.append(
                {
                    "where": where,
                    "detail": "did not reach the requested value",
                    "expected": describe_value(expected),
                    "actual": describe_value(actual),
                }
            )
    return problems


def describe_edit(edit: Any) -> str:
    target = getattr(edit, "target", "surface")
    if target == "surface":
        return f"surface {getattr(edit, 'surface', None)} {getattr(edit, 'parameter', '')}"
    if target == "field":
        return f"field {getattr(edit, 'field', None)} {getattr(edit, 'parameter', '')}"
    if target == "aperture":
        return f"system aperture {getattr(edit, 'parameter', '')}"
    return f"wavelength {getattr(edit, 'wavelength', None)} {getattr(edit, 'parameter', '')}"


def edit_key(edit: Any, zoom_position: int) -> tuple[str, int, int, str]:
    """The ``allowed_changes`` key of one edit at one zoom position."""
    target = getattr(edit, "target", "surface")
    parameter = getattr(edit, "parameter", "")
    if target == "surface":
        return (target, int(getattr(edit, "surface", 0) or 0), int(zoom_position), parameter)
    if target == "field":
        return (target, int(getattr(edit, "field", 0) or 0), int(zoom_position), parameter)
    if target == "aperture":
        return (target, 0, int(zoom_position), parameter)
    return (target, int(getattr(edit, "wavelength", 0) or 0), 0, parameter)


def parameter_identity(
    edit: Any, zoom_position: int, snapshot: LensSnapshot | None = None
) -> tuple[str, int, int, str]:
    """The one parameter an edit really changes, for merging repeated edits.

    A zoomed parameter needs one expectation per zoom position, while a shared
    parameter has a single value no matter which position an edit names, so both
    edits have to be required only once. The reference wavelength is a property
    of the whole lens, not of the wavelength that appears in the edit, so it
    collapses under one global key.
    """
    target = getattr(edit, "target", "surface")
    parameter = getattr(edit, "parameter", "")
    if target == "surface":
        selector = int(getattr(edit, "surface", 0) or 0)
        zoom = (
            int(zoom_position)
            if parameter_varies_by_zoom(snapshot, target, selector, parameter)
            else 0
        )
        return (target, selector, zoom, parameter)
    if target == "field":
        selector = int(getattr(edit, "field", 0) or 0)
        zoom = (
            int(zoom_position)
            if parameter_varies_by_zoom(snapshot, target, selector, parameter)
            else 0
        )
        return (target, selector, zoom, parameter)
    if target == "aperture":
        return (target, 0, int(zoom_position), parameter)
    if parameter == "is_reference":
        return (target, 0, 0, parameter)
    return (target, int(getattr(edit, "wavelength", 0) or 0), 0, parameter)


def expectation_keys(
    final: LensSnapshot, requested: list[tuple[str, int, int, str]]
) -> list[tuple[str, int, int, str]]:
    """Keys whose accepted value depends on the final state, such as glass.

    A glass edit names the glass only; CODE V keeps the catalog that is already
    on the surface, so the accepted value is ``NAME_CATALOG`` and the catalog
    can only come from the verified final read.
    """
    keys: list[tuple[str, int, int, str]] = []
    for target, selector, zoom, parameter in requested:
        if target != "surface" or parameter != "glass":
            continue
        zoom_state = _zoom_at(final, zoom) or (final.zooms[0] if final.zooms else None)
        if zoom_state is None:
            continue
        surface = _surface(zoom_state, selector)
        current = surface.glass or ""
        catalog = current.split("_", 1)[1] if "_" in current else ""
        if not catalog or catalog in surface.glass or True:
            keys.append((target, selector, zoom, parameter))
    return keys


# -------------------------------------------------------------- persistence


@dataclass
class Checkpoint:
    """A committed revision as recorded in ``current.json``."""

    lens_id: str
    revision: int
    lens_path: Path
    metadata_path: Path
    source_path: str | None = None
    lens_sha256: str = ""
    lens_size: int = 0
    created_at: str = ""
    snapshot: LensSnapshot = field(default_factory=LensSnapshot)
    backend_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": CHECKPOINT_FORMAT_VERSION,
            "backend_id": self.backend_id,
            "lens_id": self.lens_id,
            "revision": self.revision,
            "source_path": self.source_path,
            "lens_file": self.lens_path.name,
            "metadata_file": self.metadata_path.name,
            "lens_sha256": self.lens_sha256,
            "lens_size": self.lens_size,
            "created_at": self.created_at,
            "snapshot": self.snapshot.to_dict(),
        }


class LensCheckpointStore:
    """Files of one backend instance: one directory per opened lens."""

    def __init__(self, root: str | Path, backend_id: str | None = None) -> None:
        self.root = Path(root)
        self.backend_id = backend_id or uuid.uuid4().hex
        self.directory = self.root / self.backend_id
        #: Where the engine losses and recovery attempts of this instance are
        #: appended, so a later diagnosis does not depend on the worker log.
        self.recovery_log = self.directory / "recovery-log.jsonl"

    # ------------------------------------------------------------ discovery

    def create_lens(self) -> tuple[str, Path]:
        """Allocate a new lens identity and its own directory."""
        lens_id = uuid.uuid4().hex
        directory = self.directory / lens_id
        prepare_directory(directory)
        return lens_id, directory

    def ensure(self) -> None:
        prepare_directory(self.directory)

    # ------------------------------------------------------------- writing

    def publish(
        self,
        directory: Path,
        lens_id: str,
        revision: int,
        lens_path: Path,
        snapshot: LensSnapshot,
        *,
        source_path: str | None,
        expected_sha256: str | None = None,
    ) -> Checkpoint:
        """Write the revision metadata and point ``current.json`` at it.

        Called only after the candidate lens file has been restored and verified.
        The pointer is the last step, so a failure here leaves the previous
        revision committed.
        """
        if not lens_path.exists():
            raise CheckpointPublishError(
                "The candidate lens file for this revision does not exist.",
                details={"path": str(lens_path)},
            )
        size = lens_path.stat().st_size
        if size <= 0:
            raise CheckpointPublishError(
                "The candidate lens file for this revision is empty.",
                details={"path": str(lens_path)},
            )
        digest = hash_file(lens_path)
        if expected_sha256 is not None and digest != expected_sha256:
            raise CheckpointPublishError(
                "The candidate lens bytes changed before checkpoint publication.",
                details={"path": str(lens_path)},
            )
        checkpoint = Checkpoint(
            lens_id=lens_id,
            revision=revision,
            lens_path=lens_path,
            metadata_path=directory / f"revision-{revision:06d}.json",
            source_path=source_path,
            lens_sha256=digest,
            lens_size=size,
            created_at=utc_now(),
            snapshot=snapshot,
            backend_id=self.backend_id,
        )
        prepare_directory(directory)
        write_json_atomic(checkpoint.metadata_path, checkpoint.to_dict())
        write_json_atomic(directory / CURRENT_POINTER_NAME, checkpoint.to_dict())
        return checkpoint

    def load_current(self, directory: Path) -> Checkpoint:
        """Read and validate the committed revision of one lens directory."""
        pointer = Path(directory) / CURRENT_POINTER_NAME
        if not pointer.exists():
            raise CheckpointError(
                "No committed lens checkpoint was found for this lens directory.",
                details={"path": str(pointer)},
            )
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                "The committed checkpoint pointer is unreadable.",
                details={"path": str(pointer), "error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        return self._checkpoint_from_payload(Path(directory), payload)

    def load_revision(self, directory: Path, revision: int) -> Checkpoint:
        """Read one revision metadata file by number."""
        path = Path(directory) / f"revision-{revision:06d}.json"
        if not path.exists():
            raise CheckpointError(
                "The requested checkpoint revision does not exist.",
                details={"path": str(path), "revision": revision},
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                "The checkpoint revision metadata is unreadable.",
                details={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        return self._checkpoint_from_payload(Path(directory), payload)

    def _checkpoint_from_payload(self, directory: Path, payload: dict[str, Any]) -> Checkpoint:
        if not isinstance(payload, dict):
            raise CheckpointError(
                "The checkpoint metadata is not an object.", details={"directory": str(directory)}
            )
        version = payload.get("format_version")
        if version != CHECKPOINT_FORMAT_VERSION:
            hint = (
                "This checkpoint predates complete aperture state. Reopen the source lens "
                "to create a new checkpoint; the old files were left unchanged."
                if version in {1, 2}
                else "This checkpoint predates field vignetting factors. Reopen the source lens "
                "to create a new checkpoint; the old files were left unchanged."
                if version in {3, 4, 5}
                else "Reopen the source lens with this service version to create a compatible checkpoint."
            )
            raise CheckpointError(
                "The checkpoint metadata has an unsupported format version.",
                details={
                    "directory": str(directory),
                    "format_version": version,
                    "expected": CHECKPOINT_FORMAT_VERSION,
                },
                hint=hint,
            )
        try:
            revision = int(payload["revision"])
            lens_name = str(payload["lens_file"])
            metadata_name = str(payload["metadata_file"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(
                "The checkpoint metadata is incomplete.",
                details={"directory": str(directory), "error": str(exc)},
            ) from exc
        lens_path = directory / lens_name
        metadata_path = directory / metadata_name
        if lens_path.parent != Path(directory) or metadata_path.parent != Path(directory):
            raise CheckpointError(
                "The checkpoint metadata points outside its own directory.",
                details={"directory": str(directory), "lens_file": lens_name},
            )
        if not lens_path.exists():
            raise CheckpointError(
                "The lens file of the committed checkpoint is missing.",
                details={"path": str(lens_path), "revision": revision},
            )
        expected_digest = str(payload.get("lens_sha256") or "")
        expected_size = int(payload.get("lens_size") or 0)
        size = lens_path.stat().st_size
        if size <= 0:
            raise CheckpointError(
                "The lens file of the committed checkpoint is empty.",
                details={"path": str(lens_path), "revision": revision},
            )
        if expected_size and size != expected_size:
            raise CheckpointError(
                "The lens file of the committed checkpoint changed size.",
                details={"path": str(lens_path), "revision": revision, "expected": expected_size, "actual": size},
            )
        digest = hash_file(lens_path)
        if expected_digest and digest != expected_digest:
            raise CheckpointError(
                "The lens file of the committed checkpoint does not match its hash.",
                details={"path": str(lens_path), "revision": revision, "expected": expected_digest, "actual": digest},
            )
        return Checkpoint(
            lens_id=str(payload.get("lens_id") or directory.name),
            revision=revision,
            lens_path=lens_path,
            metadata_path=metadata_path,
            source_path=payload.get("source_path"),
            lens_sha256=digest,
            lens_size=size,
            created_at=str(payload.get("created_at") or ""),
            snapshot=LensSnapshot.from_dict(payload.get("snapshot") or {}),
            backend_id=str(payload.get("backend_id") or ""),
        )

    # ------------------------------------------------------- bookkeeping

    def transaction_path(self, directory: Path, transaction_id: str) -> Path:
        return Path(directory) / TRANSACTION_DIRECTORY / f"{transaction_id}.json"

    def write_transaction(self, directory: Path, transaction_id: str, payload: dict[str, Any]) -> Path:
        path = self.transaction_path(directory, transaction_id)
        prepare_directory(path.parent)
        record = dict(payload)
        record.setdefault("transaction_id", transaction_id)
        write_json_atomic(path, record)
        return path

    def restore_point_path(self, directory: Path, transaction_id: str, unique: str | None = None) -> Path:
        name = f"{transaction_id}.len" if unique is None else f"{transaction_id}-{unique}.len"
        return Path(directory) / RESTORE_POINT_DIRECTORY / name

    def prepare(self, directory: Path) -> None:
        prepare_directory(Path(directory))


def prepare_directory(path: Path) -> None:
    """Create a directory (and its parents) if it is missing."""
    path.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON file through a temporary file in the same directory."""
    path = Path(path)
    prepare_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            flush_to_disk(handle)
        os.replace(temporary, path)
        sync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CheckpointPublishError(
            "The checkpoint metadata could not be written.",
            details={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def flush_to_disk(handle: Any) -> None:
    """Ask the platform to put the bytes of a just written file on disk."""
    if hasattr(os, "fdatasync"):
        try:
            os.fdatasync(handle.fileno())
            return
        except (OSError, AttributeError):
            pass
    try:
        os.fsync(handle.fileno())
    except (OSError, AttributeError):
        pass


def sync_directory(path: Path) -> None:
    """Flush a directory entry, which only has an effect on POSIX hosts."""
    if sys.platform.startswith("win"):
        return
    try:  # pragma: no cover - not reached on Windows
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:  # pragma: no cover - not reached on Windows
        os.fsync(descriptor)
    except OSError:
        pass
    finally:  # pragma: no cover - not reached on Windows
        os.close(descriptor)


def call_hook(hook: Callable[[str], None] | None, name: str) -> None:
    """Run a test fault injection hook, if one is installed."""
    if hook is not None:
        hook(name)


def checkpoint_format_version() -> int:
    """Format version the metadata of a newly committed revision carries."""
    return CHECKPOINT_FORMAT_VERSION
