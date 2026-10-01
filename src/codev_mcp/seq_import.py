"""Import a lens data sequence (.seq) through the typed public tools (E4).

The file is never run. Every line is parsed here against a fixed whitelist of
lens data commands and turned into typed requests (``create_lens`` with an
optional PIM solve, a field set replacement, wavelength weight and reference
edits); the commands CODE V receives are built by the service from those
typed values, exactly as for any other request. ``IN``, macros, apertures,
solves other than a PIM image solve, zoom data, aspheres and every other
command are refused, and one refused line rejects the whole file with all the
offending lines listed. Metadata that does not describe the optics (``TITLE``,
``INI``, ``UID``, ``DOR``, ``DER``) and the variable markers (``CCY``, ``THC``,
``GLC``) are skipped and listed as not imported.

After the import the lens is read back and compared with the sequence, and
its native listing (LIS) is compared as well; the result is saved with
``save_lens_as``, reopened in a separate session and checked again. An optional
reference ``.len`` (for example the file the sequence was exported from with
WRL) is compared item by item.

    python -m codev_mcp.seq_import --seq lens.seq --output lens.len [--reference-lens ref.len]
"""
from __future__ import annotations

import argparse
import copy
import math
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .compare import ROOT, digest, write_json
from .errors import ParameterError
from .fieldset import field_set_commands, resolve_field_set
from .listing import parse_listing
from .models import (
    CreateLensRequest,
    FieldAngleSpec,
    FieldSetReplacement,
    FieldSpec,
    LensField,
    SphericalSurfaceSpec,
    Units,
)
from .modeling import create_commands
from .record import execution_step, write_record
from .safety import format_float
from .scale import _now, _session
from .stdio_client import StdioClient

MAX_BYTES = 1 << 20
MAX_LINES = 5000
#: Commands that describe nothing the import needs: skipped and listed.
METADATA_COMMANDS = {"TITLE", "INI", "UID", "DOR", "DER"}
#: Variable markers of the surface lines: skipped and listed (AUT variables are set
#: by their own typed specification, never by a marker in an imported file).
MARKER_COMMANDS = {"CCY", "THC", "GLC"}
UNITS = {"M": Units.MM, "C": Units.CM, "I": Units.INCH}
APERTURE_COMMANDS = {"EPD": "epd", "FNO": "fno", "NA": "na", "NAO": "nao"}
FIELD_COMMANDS = ("XAN", "YAN", "WTF", "VUX", "VLX", "VUY", "VLY")
NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][+-]?\d+)?$")
#: A thickness at or beyond this is CODE V's infinite object distance.
INFINITE_THICKNESS = 1.0e10
#: Factors below this are zero for the import; the sent values carry twelve digits.
NEGLIGIBLE = 1e-9
#: Read back values must equal the sequence to this relative tolerance (twelve digits are sent).
RELATIVE_TOLERANCE = 1e-9
ABSOLUTE_TOLERANCE = 1e-9
#: The re-derived PIM thickness may differ from the sequence's start value by this much
#: before the difference is reported as a warning (CODE V would re-derive it as well).
PIM_START_TOLERANCE = 1e-6


class SeqRejected(ValueError):
    """The whole file is refused; ``problems`` lists every offending line."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        shown = "; ".join(f"line {item['line']}: {item['reason']}" for item in problems[:8])
        more = f" (+{len(problems) - 8} more)" if len(problems) > 8 else ""
        super().__init__(f"The sequence is refused: {shown}{more}")


@dataclass
class SeqSurface:
    radius: float | None
    thickness: float
    glass: str | None
    line: int
    stop: bool = False
    pim: bool = False


@dataclass
class ParsedSeq:
    units: str | None = None
    aperture_kind: str | None = None
    aperture_value: float | None = None
    wavelengths_nm: list[float] = field(default_factory=list)
    reference: int | None = None
    wavelength_weights: list[float] | None = None
    fields: dict[str, list[float]] = field(default_factory=dict)
    surfaces: list[SeqSurface] = field(default_factory=list)
    object_thickness: float | None = None
    image: tuple[float | None, float] | None = None
    not_imported: list[dict] = field(default_factory=list)
    lines_read: int = 0
    commands_read: int = 0

    @property
    def field_count(self) -> int:
        return len(self.fields.get("YAN", []))

    @property
    def stop_surface(self) -> int | None:
        return next((number for number, item in enumerate(self.surfaces, 1) if item.stop), None)

    @property
    def image_solve(self) -> str | None:
        return "pim" if any(item.pim for item in self.surfaces) else None


# ------------------------------------------------------------------ parsing


def _split(text: str, separator: str | None) -> list[str]:
    """Split at ``separator`` (or whitespace when None) outside quoted strings."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    for character in text:
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
        elif character in "'\"":
            quote = character
            current.append(character)
        elif (separator is not None and character == separator) or (
                separator is None and character.isspace()):
            if current or separator is not None:
                parts.append("".join(current))
            current = []
        else:
            current.append(character)
    if quote:
        raise ValueError("unterminated quoted string")
    if current or separator is not None:
        parts.append("".join(current))
    return [part for part in (item.strip() for item in parts) if part] if separator is None else parts


def _number(token: str) -> float:
    if not NUMBER.match(token):
        raise ValueError(f"{token!r} is not a number")
    value = float(token.replace("d", "e").replace("D", "e"))
    if not math.isfinite(value):
        raise ValueError(f"{token!r} is not finite")
    return value


def _numbers(tokens: list[str], *, minimum: int = 1, maximum: int = 10) -> list[float]:
    if not minimum <= len(tokens) <= maximum:
        raise ValueError(f"expected {minimum} to {maximum} numbers, found {len(tokens)}")
    return [_number(token) for token in tokens]


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Physical lines with comments and blanks dropped and ``&`` continuations joined."""
    lines: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if pending is None and (not stripped or stripped.startswith("!")):
            continue
        if pending is not None:
            stripped = pending[1] + " " + stripped
            number = pending[0]
            pending = None
        if stripped.endswith("&"):
            pending = (number, stripped[:-1].rstrip())
            continue
        lines.append((number, stripped))
    if pending is not None:
        lines.append(pending)
    return lines


def parse_seq(text: str) -> ParsedSeq:
    """Parse a sequence against the whitelist; raise ``SeqRejected`` listing every refused line."""
    if "\x00" in text:
        raise SeqRejected([{"line": 0, "text": "", "reason": "the file contains a NUL character"}])
    if len(text.encode("utf-8")) > MAX_BYTES or text.count("\n") > MAX_LINES:
        raise SeqRejected([{"line": 0, "text": "", "reason": f"the file exceeds {MAX_BYTES} bytes or {MAX_LINES} lines"}])
    parsed = ParsedSeq()
    problems: list[dict] = []
    seen: set[str] = set()
    context: tuple[str, SeqSurface | None] | None = None  # ("SO" | "S" | "SI", surface)
    finished = False
    lines = _logical_lines(text)
    parsed.lines_read = len(lines)

    def refuse(number: int, source: str, reason: str) -> None:
        problems.append({"line": number, "text": source[:120], "reason": reason})

    def once(keyword: str, number: int, source: str) -> bool:
        if keyword in seen:
            refuse(number, source, f"{keyword} appears more than once")
            return False
        seen.add(keyword)
        return True

    for number, line in lines:
        try:
            commands = _split(line, ";")
        except ValueError as exc:
            refuse(number, line, str(exc))
            continue
        for command in commands:
            if not command:
                continue
            try:
                tokens = _split(command, None)
            except ValueError as exc:
                refuse(number, command, str(exc))
                continue
            if not tokens:
                continue
            keyword, args = tokens[0].upper(), tokens[1:]
            parsed.commands_read += 1
            if finished:
                refuse(number, command, "content after GO")
                continue
            try:
                context = _dispatch(parsed, keyword, args, command, number, context, once, refuse)
            except ValueError as exc:
                refuse(number, command, str(exc))
            if keyword == "GO":
                finished = True

    _check_structure(parsed, problems)
    if problems:
        raise SeqRejected(problems)
    return parsed


def _dispatch(parsed, keyword, args, command, number, context, once, refuse):
    """Handle one command; returns the surface context that follows it."""
    def skip(reason: str) -> None:
        parsed.not_imported.append({"line": number, "keyword": keyword, "text": command[:120], "reason": reason})

    if keyword in {"S", "SO", "SI"}:
        if keyword == "S":
            if len(args) not in (2, 3):
                raise ValueError("S needs a radius, a thickness and an optional glass")
            radius, thickness = _numbers(args[:2], minimum=2, maximum=2)
            glass = args[2] if len(args) == 3 else None
            surface = SeqSurface(None if radius == 0 else radius, thickness, glass, number)
            parsed.surfaces.append(surface)
            return ("S", surface)
        if len(args) != 2:
            raise ValueError(f"{keyword} needs a radius and a thickness")
        radius, thickness = _numbers(args, minimum=2, maximum=2)
        if keyword == "SO":
            if not once("SO", number, command):
                return None
            parsed.object_thickness = thickness  # recorded first: a refusal is not also "missing"
            if radius != 0 or thickness < INFINITE_THICKNESS:
                raise ValueError("only an object at infinity (plane, infinite thickness) is supported")
        else:
            if not once("SI", number, command):
                return None
            parsed.image = (None, thickness)
            if radius != 0 or thickness != 0:
                raise ValueError("the image surface must be a plane at zero thickness "
                                 "(a defocused image is not supported)")
        return (keyword, None)

    if keyword in MARKER_COMMANDS:
        if context is None:
            raise ValueError(f"{keyword} is only accepted after a surface line")
        _numbers(args, minimum=1, maximum=1)
        skip("variable marker")
        return context
    if keyword == "STO":
        if args or context is None or context[0] != "S":
            raise ValueError("STO is only accepted, without arguments, after an ordinary surface")
        context[1].stop = True
        return context
    if keyword == "PIM":
        if context is None or context[0] != "S" or (args and [item.upper() for item in args] != ["YES"]):
            raise ValueError("PIM is only accepted after an ordinary surface line")
        context[1].pim = True
        return context

    if keyword in METADATA_COMMANDS:
        skip("metadata" if keyword in {"TITLE", "INI", "UID"} else
             ("unknown system item" if keyword == "DOR" else "derivative increments left by AUT"))
        return None if keyword != "DER" else context
    if keyword == "RDM":
        if args:
            raise ValueError("RDM takes no arguments")
        return None
    if keyword == "LEN":
        if len(args) > 1 or (args and args[0][0] not in "'\""):
            raise ValueError("LEN accepts only a quoted version string")
        skip("version string")
        return None
    if keyword == "GO":
        if args:
            raise ValueError("GO takes no arguments")
        return None
    if keyword in APERTURE_COMMANDS:
        if parsed.aperture_kind is not None:
            raise ValueError("the system aperture is defined more than once")
        parsed.aperture_kind = APERTURE_COMMANDS[keyword]
        parsed.aperture_value = _numbers(args, minimum=1, maximum=1)[0]
        return None
    if keyword == "DIM":
        if not once("DIM", number, command):
            return None
        if len(args) != 1 or args[0].upper() not in UNITS:
            raise ValueError("DIM must be M, C or I")
        parsed.units = args[0].upper()
        return None
    if keyword == "WL":
        if not once("WL", number, command):
            return None
        parsed.wavelengths_nm = _numbers(args, minimum=1, maximum=10)
        return None
    if keyword == "REF":
        if not once("REF", number, command):
            return None
        value = _numbers(args, minimum=1, maximum=1)[0]
        if value != int(value) or value < 1:
            raise ValueError("REF must be a positive integer")
        parsed.reference = int(value)
        return None
    if keyword == "WTW":
        if not once("WTW", number, command):
            return None
        values = _numbers(args, minimum=1, maximum=10)
        if any(item != int(item) or item < 0 for item in values):
            raise ValueError("wavelength weights must be non-negative integers")
        parsed.wavelength_weights = values
        return None
    if keyword in FIELD_COMMANDS:
        if not once(keyword, number, command):
            return None
        parsed.fields[keyword] = _numbers(args, minimum=1, maximum=10)
        return None
    raise ValueError(f"{keyword} is not a lens data command this import accepts")


def _check_structure(parsed: ParsedSeq, problems: list[dict]) -> None:
    def refuse(reason: str, line: int = 0) -> None:
        problems.append({"line": line, "text": "", "reason": reason})

    if parsed.units is None:
        refuse("DIM is missing")
    if parsed.aperture_kind is None:
        refuse("the system aperture (EPD, FNO, NA or NAO) is missing")
    if not parsed.wavelengths_nm:
        refuse("WL is missing")
    elif any(a <= b for a, b in zip(parsed.wavelengths_nm, parsed.wavelengths_nm[1:])):
        refuse("wavelengths must be strictly descending, as CODE V lists them")
    if "YAN" not in parsed.fields or "XAN" not in parsed.fields:
        refuse("XAN and YAN are both required")
    else:
        count = parsed.field_count
        for name, values in parsed.fields.items():
            if len(values) != count:
                refuse(f"{name} has {len(values)} values but YAN has {count}")
    if parsed.wavelength_weights is not None and len(parsed.wavelength_weights) != len(parsed.wavelengths_nm):
        refuse("WTW needs one weight per wavelength")
    if parsed.reference is not None and parsed.reference > len(parsed.wavelengths_nm):
        refuse("REF names a wavelength that does not exist")
    if parsed.object_thickness is None:
        refuse("SO (the object surface) is missing")
    if parsed.image is None:
        refuse("SI (the image surface) is missing")
    if len(parsed.surfaces) < 2:
        refuse("at least two ordinary surfaces are needed")
    stops = [item for item in parsed.surfaces if item.stop]
    if len(stops) != 1:
        refuse(f"exactly one STO is needed, found {len(stops)}")
    pim = [number for number, item in enumerate(parsed.surfaces, 1) if item.pim]
    if len(pim) > 1 or (pim and pim[0] != len(parsed.surfaces)):
        refuse("a PIM solve is only supported on the last ordinary surface", parsed.surfaces[pim[0] - 1].line)
    for item in parsed.surfaces:
        if item.glass is not None and "_" not in item.glass:
            refuse(f"glass {item.glass!r} needs the form NAME_CATALOG", item.line)


# ------------------------------------------------------------------ planning


@dataclass
class ImportPlan:
    create: CreateLensRequest
    field_set: FieldSetReplacement | None
    reference: int | None
    wavelength_weights: list[float] | None
    commands: list[str]


def plan_import(parsed: ParsedSeq) -> ImportPlan:
    """The typed requests for a parsed sequence, validated before any CODE V call."""
    try:
        angles = [FieldAngleSpec(x_angle=x, y_angle=y)
                  for x, y in zip(parsed.fields["XAN"], parsed.fields["YAN"])]
        create = CreateLensRequest(
            units=UNITS[parsed.units], aperture_kind=parsed.aperture_kind,
            aperture_value=parsed.aperture_value, wavelengths_nm=parsed.wavelengths_nm,
            fields=angles,
            surfaces=[SphericalSurfaceSpec(radius=item.radius, thickness=item.thickness, glass=item.glass)
                      for item in parsed.surfaces],
            stop_surface=parsed.stop_surface, image_solve=parsed.image_solve)
        commands = create_commands(create)
        count = parsed.field_count
        weights = parsed.fields.get("WTF") or [1.0] * count
        factors = {name: parsed.fields.get(name.upper()) or [0.0] * count
                   for name in ("vux", "vlx", "vuy", "vly")}
        field_set = None
        if any(abs(item - 1) > NEGLIGIBLE for item in weights) or any(
                abs(item) > NEGLIGIBLE for values in factors.values() for item in values):
            field_set = FieldSetReplacement(fields=[
                FieldSpec(x_angle=parsed.fields["XAN"][index], y_angle=parsed.fields["YAN"][index],
                          weight=weights[index],
                          **{name: (0.0 if abs(values[index]) <= NEGLIGIBLE else values[index])
                             for name, values in factors.items()})
                for index in range(count)])
            created = [LensField(number=index + 1, x_angle=item.x_angle, y_angle=item.y_angle,
                                 weight=1.0, vux=0.0, vlx=0.0, vuy=0.0, vly=0.0)
                       for index, item in enumerate(angles)]
            commands += field_set_commands(resolve_field_set(field_set, created), created)
    except (ValidationError, ParameterError, ValueError) as exc:
        reason = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()) \
            if isinstance(exc, ValidationError) else getattr(exc, "message", str(exc))
        raise SeqRejected([{"line": 0, "text": "", "reason": f"the sequence cannot be built as a typed lens: {reason}"}]) from None
    return ImportPlan(create, field_set, parsed.reference, parsed.wavelength_weights, commands)


# --------------------------------------------------------------- verification


def _close(left, right, tolerance: float = RELATIVE_TOLERANCE) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=ABSOLUTE_TOLERANCE)


def _deviation(left, right) -> float:
    if left is None or right is None:
        return 0.0
    return abs(left - right) / max(abs(right), 1.0)


def verify_against_sequence(lens: dict, parsed: ParsedSeq) -> tuple[list[dict], float, list[str]]:
    """Compare a lens read back with the sequence; returns checks, the largest deviation, warnings."""
    checks: list[dict] = []
    warnings: list[str] = []
    worst = 0.0

    def check(name: str, passed: bool, **detail) -> None:
        checks.append({"name": name, "passed": bool(passed), **detail})

    def measure(left, right) -> bool:
        nonlocal worst
        worst = max(worst, _deviation(left, right))
        return _close(left, right)

    check("units", lens["units"] == UNITS[parsed.units].value, expected=UNITS[parsed.units].value, actual=lens["units"])
    aperture = lens["aperture"]
    check("system aperture", aperture["kind"] == parsed.aperture_kind and measure(aperture["value"], parsed.aperture_value),
          expected=[parsed.aperture_kind, parsed.aperture_value], actual=[aperture["kind"], aperture["value"]])
    waves = lens["wavelengths"]
    check("wavelengths", len(waves) == len(parsed.wavelengths_nm) and all(
        measure(item["micrometers"] * 1000, nm) for item, nm in zip(waves, parsed.wavelengths_nm)),
        expected=parsed.wavelengths_nm, actual=[item["micrometers"] * 1000 for item in waves])
    if parsed.wavelength_weights is not None:
        check("wavelength weights", [item["weight"] for item in waves] == parsed.wavelength_weights,
              expected=parsed.wavelength_weights, actual=[item["weight"] for item in waves])
    if parsed.reference is not None:
        actual = next((item["number"] for item in waves if item["is_reference"]), None)
        check("reference wavelength", actual == parsed.reference, expected=parsed.reference, actual=actual)
    fields = lens["fields"]
    count = parsed.field_count
    weights = parsed.fields.get("WTF") or [1.0] * count
    check("field count", len(fields) == count, expected=count, actual=len(fields))
    if len(fields) == count:
        for name, key in (("XAN", "x_angle"), ("YAN", "y_angle")):
            check(f"field {key}s", all(measure(item[key], value) for item, value in zip(fields, parsed.fields[name])),
                  expected=parsed.fields[name], actual=[item[key] for item in fields])
        check("field weights", all(measure(item["weight"], value) for item, value in zip(fields, weights)),
              expected=weights, actual=[item["weight"] for item in fields])
        for key in ("vux", "vlx", "vuy", "vly"):
            expected = parsed.fields.get(key.upper()) or [0.0] * count
            check(f"field {key}", all(measure(item[key], value) for item, value in zip(fields, expected)),
                  expected=expected, actual=[item[key] for item in fields])
    surfaces = lens["surfaces"]
    check("surface count", len(surfaces) == len(parsed.surfaces) + 2,
          expected=len(parsed.surfaces) + 2, actual=len(surfaces))
    if len(surfaces) == len(parsed.surfaces) + 2:
        check("stop surface", lens["stop_surface"] == parsed.stop_surface,
              expected=parsed.stop_surface, actual=lens["stop_surface"])
        for number, expected in enumerate(parsed.surfaces, 1):
            actual = surfaces[number]
            radius_ok = (actual["radius_is_infinite"] if expected.radius is None
                         else (not actual["radius_is_infinite"] and measure(actual["radius"], expected.radius)))
            glass_ok = (actual["glass"] or "").upper() == (expected.glass or "").upper()
            if expected.pim:
                thickness_ok = actual["thickness"] is not None and not actual["thickness_is_infinite"]
                gap = _deviation(actual["thickness"], expected.thickness)
                if gap > PIM_START_TOLERANCE:
                    warnings.append(
                        f"The PIM solve re-derived surface {number} thickness as {actual['thickness']!r}; "
                        f"the sequence started from {expected.thickness!r}.")
            else:
                thickness_ok = measure(actual["thickness"], expected.thickness)
            check(f"surface {number}", radius_ok and thickness_ok and glass_ok,
                  expected={"radius": expected.radius, "thickness": expected.thickness, "glass": expected.glass},
                  actual={"radius": None if actual["radius_is_infinite"] else actual["radius"],
                          "thickness": actual["thickness"], "glass": actual["glass"]})
    listing = parse_listing(lens.get("raw_listing") or "")
    expected_solves = ["PIM"] if parsed.image_solve else []
    check("native solves (LIS)", [item.upper() for item in listing.solves] == expected_solves and not listing.pickups
          and listing.relation_data_complete, expected=expected_solves, actual=listing.solves)
    spec = listing.specification
    lis_ok = (spec.aperture_kind == parsed.aperture_kind
              and math.isclose(spec.aperture_value or math.nan, parsed.aperture_value, rel_tol=1e-4, abs_tol=1e-4)
              and len(spec.wavelengths_nm) == len(parsed.wavelengths_nm)
              and all(math.isclose(a, b, rel_tol=1e-4, abs_tol=1e-2) for a, b in zip(spec.wavelengths_nm, parsed.wavelengths_nm))
              and len(spec.field_angles_y) == count
              and all(math.isclose(a, b, rel_tol=1e-4, abs_tol=1e-4) for a, b in zip(spec.field_angles_y, parsed.fields["YAN"])))
    check("native specification rows (LIS)", lis_ok,
          actual={"aperture": [spec.aperture_kind, spec.aperture_value], "wl": spec.wavelengths_nm,
                  "yan": spec.field_angles_y})
    return checks, worst, warnings


def compare_lenses(imported: dict, reference: dict) -> list[str]:
    """Item-by-item differences between two lens reads (empty when they agree)."""
    problems: list[str] = []

    def same(name: str, left, right) -> None:
        if not (_close(left, right) if isinstance(left, (int, float)) and not isinstance(left, bool)
                and isinstance(right, (int, float)) and not isinstance(right, bool) else left == right):
            problems.append(f"{name}: {left!r} != {right!r}")

    for key in ("units", "stop_surface", "zoom_positions"):
        same(key, imported[key], reference[key])
    same("aperture.kind", imported["aperture"]["kind"], reference["aperture"]["kind"])
    same("aperture.value", imported["aperture"]["value"], reference["aperture"]["value"])
    for name, left_list, right_list, keys in (
            ("wavelength", imported["wavelengths"], reference["wavelengths"], ("micrometers", "weight", "is_reference")),
            ("field", imported["fields"], reference["fields"],
             ("x_angle", "y_angle", "weight", "vux", "vlx", "vuy", "vly")),
            ("surface", imported["surfaces"], reference["surfaces"],
             ("role", "is_stop", "radius", "radius_is_infinite", "thickness", "thickness_is_infinite",
              "glass", "semi_aperture"))):
        if len(left_list) != len(right_list):
            problems.append(f"{name} count: {len(left_list)} != {len(right_list)}")
            continue
        for index, (left, right) in enumerate(zip(left_list, right_list)):
            for key in keys:
                same(f"{name} {index} {key}", left.get(key), right.get(key))
    left_relations = parse_listing(imported.get("raw_listing") or "")
    right_relations = parse_listing(reference.get("raw_listing") or "")
    same("solves", left_relations.solves, right_relations.solves)
    same("pickups", left_relations.pickups, right_relations.pickups)
    return problems


# ----------------------------------------------------------------- execution


def summarise_not_imported(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["keyword"]] = counts.get(item["keyword"], 0) + 1
    return counts


def run_import(seq: Path, output: Path, *, reference_lens: Path | None = None, backend: str = "com",
               timeout: float = 300.0, output_dir: Path | None = None,
               client_factory=StdioClient) -> tuple[Path, dict]:
    seq, output = seq.resolve(), output.resolve()
    if not seq.is_file() or seq.suffix.lower() != ".seq":
        raise ValueError(f"Expected an existing .seq file: {seq}")
    if output.suffix.lower() != ".len" or output.exists() or not output.parent.is_dir():
        raise ValueError(f"Output must be a new .len in an existing directory: {output}")
    if reference_lens is not None:
        reference_lens = reference_lens.resolve()
        if not reference_lens.is_file() or reference_lens.suffix.lower() != ".len":
            raise ValueError(f"Expected an existing reference .len: {reference_lens}")
    if backend not in {"com", "simulated"} or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("backend must be com or simulated and timeout must be positive")
    raw = seq.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("The sequence file is not UTF-8/ASCII text") from None
    source_hash = digest(seq)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    bundle = (output_dir or ROOT / ".codev-run" / "seq-imports").resolve() / run_id
    bundle.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(seq, bundle / "input.seq")
    manifest: dict = {"schema_version": 1, "kind": "seq_import", "run_id": run_id, "created_at": _now(),
                      "status": "running", "source": "codev" if backend == "com" else "simulated",
                      "input": {"path": str(seq), "sha256": source_hash}}
    started = time.monotonic()
    commands: list[str] = []
    try:
        parsed = parse_seq(text)
        manifest["parsed"] = {"lines_read": parsed.lines_read, "commands_read": parsed.commands_read,
                              "surfaces": len(parsed.surfaces), "fields": parsed.field_count,
                              "wavelengths": len(parsed.wavelengths_nm), "image_solve": parsed.image_solve}
        manifest["not_imported"] = {"counts": summarise_not_imported(parsed.not_imported),
                                    "items": parsed.not_imported}
        plan = plan_import(parsed)
        commands = list(plan.commands)
        write_json(bundle / "plan.json", {
            "create_lens": plan.create.model_dump(mode="json"),
            "field_set": plan.field_set.model_dump(mode="json") if plan.field_set else None,
            "reference": plan.reference, "wavelength_weights": plan.wavelength_weights})
        work_root = ROOT / ".codev-run" / ("q-" + uuid.uuid4().hex[:8])
        if not str(work_root).isascii():
            raise ValueError("Repository work directory must have an ASCII path for CODE V 10.2")
        (work_root / "import").mkdir(parents=True)
        saved_path = work_root / "import" / "imported.len"

        def build(client):
            created, _ = client.call("create_lens", {"request": plan.create.model_dump(mode="json")})
            steps: list[dict] = []
            if plan.field_set is not None:
                update, _ = client.call("update_lens", {"request": {"field_set": plan.field_set.model_dump(
                    mode="json", exclude_none=True)}})
                steps.append({"step": "field_set", "warnings": update.get("warnings")})
                if update.get("rolled_back") or not (update.get("field_set") or {}).get("applied"):
                    raise ValueError("The field set was refused or rolled back: " + "; ".join(update.get("warnings") or []))
            lens, _ = client.call("get_lens")
            edits = []
            if plan.reference is not None and not next(
                    (item["is_reference"] for item in lens["wavelengths"] if item["number"] == plan.reference), False):
                edits.append({"target": "wavelength", "wavelength": plan.reference,
                              "parameter": "is_reference", "value": plan.reference})
            if plan.wavelength_weights is not None:
                edits += [{"target": "wavelength", "wavelength": index, "parameter": "weight", "value": weight}
                          for index, (item, weight) in enumerate(zip(lens["wavelengths"], plan.wavelength_weights), 1)
                          if item["weight"] != weight]
            if edits:
                commands.extend(
                    f"REF {item['value']}" if item["parameter"] == "is_reference"
                    else f"WTW W{item['wavelength']} {int(item['value'])}" for item in edits)
                update, _ = client.call("update_lens", {"request": {"edits": edits}})
                steps.append({"step": "wavelength_edits", "edits": edits, "warnings": update.get("warnings")})
                if update.get("rolled_back") or not all(o.get("applied") for o in update.get("outcomes", [])):
                    raise ValueError("The wavelength edits were refused or rolled back: "
                                     + "; ".join(update.get("warnings") or []))
                lens, _ = client.call("get_lens")
            write_json(bundle / "lens-imported.json", lens)
            checks, worst, warnings = verify_against_sequence(lens, parsed)
            manifest["verification"] = {"checks": checks, "max_relative_deviation": worst, "warnings": warnings,
                                        "steps": steps, "created_warnings": created.get("warnings", [])}
            failed = [item["name"] for item in checks if not item["passed"]]
            if failed:
                raise ValueError("The imported lens does not match the sequence: " + ", ".join(failed))
            saved, _ = client.call("save_lens_as", {"path": str(saved_path)})
            if saved.get("overwritten") or not saved_path.is_file() or saved_path.stat().st_size <= 0:
                raise ValueError("Imported lens save was not confirmed")
            return lens

        imported = _session(work_root / "import", bundle / "import-session", backend, timeout, client_factory, build)
        shutil.copyfile(saved_path, bundle / "imported.len")
        imported_hash = digest(bundle / "imported.len")
        reopen_dir = work_root / "reopen"
        reopen_dir.mkdir()
        reopened_path = reopen_dir / "imported.len"
        shutil.copyfile(bundle / "imported.len", reopened_path)

        def reopen(client):
            lens, _ = client.call("open_lens", {"path": str(reopened_path)})
            write_json(bundle / "lens-reopened.json", lens)
            checks, worst, _ = verify_against_sequence(lens, parsed)
            failed = [item["name"] for item in checks if not item["passed"]]
            manifest["reopen"] = {"max_relative_deviation": worst, "failed": failed}
            if failed:
                raise ValueError("The reopened lens does not match the sequence: " + ", ".join(failed))
            return lens

        _session(reopen_dir, bundle / "reopen-session", backend, timeout, client_factory, reopen)
        if reference_lens is not None:
            reference_hash = digest(reference_lens)
            reference_dir = work_root / "reference"
            reference_dir.mkdir()
            reference_copy = reference_dir / "reference.len"
            shutil.copyfile(reference_lens, reference_copy)

            def read_reference(client):
                lens, _ = client.call("open_lens", {"path": str(reference_copy)})
                write_json(bundle / "lens-reference.json", lens)
                return lens

            reference = _session(reference_dir, bundle / "reference-session", backend, timeout,
                                 client_factory, read_reference)
            differences = compare_lenses(imported, reference)
            manifest["reference"] = {"path": str(reference_lens), "sha256": reference_hash,
                                     "unchanged": digest(reference_lens) == reference_hash,
                                     "differences": differences}
            if differences:
                raise ValueError("The imported lens differs from the reference lens: " + "; ".join(differences[:6]))
        with output.open("xb") as handle:
            handle.write((bundle / "imported.len").read_bytes())
        if digest(output) != imported_hash:
            raise ValueError("Output copy does not match the verified imported lens")
        manifest["output"] = {"path": str(output), "sha256": imported_hash, "bundle_copy": "imported.len"}
        manifest["status"] = "succeeded"
    except SeqRejected as exc:
        manifest["status"] = "rejected"
        manifest["error"] = str(exc)
        manifest["rejected"] = exc.problems
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        manifest["input"]["unchanged"] = digest(seq) == source_hash
        if not manifest["input"]["unchanged"]:
            manifest["status"] = "failed"
            manifest["error"] = "Input changed during the import"
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        manifest["finished_at"] = _now()
        write_json(bundle / "manifest.json", manifest)
        write_record(bundle / "execution-record.json", [execution_step(
            action="import_seq", tool="codev_mcp.seq_import", status=manifest["status"], source=manifest["source"],
            inputs=[{"role": "sequence", "path": str(seq), "sha256": source_hash}],
            parameters={"not_imported": manifest.get("not_imported", {}).get("counts", {}),
                        "image_solve": manifest.get("parsed", {}).get("image_solve")},
            native_commands=commands,
            command_note=("Built by the service from typed values (create_lens, update_lens); "
                          "the sequence text itself is never sent to CODE V"),
            results={"verification": manifest.get("verification"), "reference": manifest.get("reference")},
            outputs=[manifest["output"]] if manifest.get("output") else [],
            bundle=str(bundle), error=manifest.get("error"))])
    return bundle, manifest


def describe(parsed: ParsedSeq, plan: ImportPlan) -> str:
    counts = summarise_not_imported(parsed.not_imported)
    skipped = "、".join(f"{key}×{value}" for key, value in sorted(counts.items())) or "无"
    lines = [f"读取 {parsed.lines_read} 行、{parsed.commands_read} 条命令：{len(parsed.surfaces)} 个普通面、"
             f"{parsed.field_count} 个视场、{len(parsed.wavelengths_nm)} 个波长；像面求解：{parsed.image_solve or '无'}",
             f"未导入：{skipped}", "服务将发送的等效命令："]
    lines += [f"  {command}" for command in plan.commands]
    lines.append("  （REF 与 WTW 只在新建镜头的读回值与序列不同时才追加修改）")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把 .seq 镜头数据序列经类型化入口导入为新的 .len（不执行序列）")
    parser.add_argument("--seq", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="新的 .len 输出文件；--dry-run 时可省略")
    parser.add_argument("--reference-lens", type=Path, help="可选：与该 .len 逐项比对（例如序列由它导出）")
    parser.add_argument("--dry-run", action="store_true", help="只解析并列出将发送的命令，不连接 CODE V")
    parser.add_argument("--backend", choices=("com", "simulated"), default="com")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codev-run" / "seq-imports")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            text = args.seq.read_text(encoding="utf-8")
            parsed = parse_seq(text)
            print(describe(parsed, plan_import(parsed)))
            return 0
        if args.output is None:
            parser.error("--output is required unless --dry-run is given")
        bundle, manifest = run_import(args.seq, args.output, reference_lens=args.reference_lens,
                                      backend=args.backend, timeout=args.timeout, output_dir=args.output_dir)
    except SeqRejected as exc:
        print(str(exc), file=sys.stderr)
        for item in exc.problems:
            print(f"  第 {item['line']} 行 {item['text']!r}：{item['reason']}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['status']}: {bundle}")
    counts = manifest.get("not_imported", {}).get("counts")
    if counts:
        print("未导入：" + "、".join(f"{key}×{value}" for key, value in sorted(counts.items())))
    for problem in manifest.get("rejected", []):
        print(f"  第 {problem['line']} 行 {problem['text']!r}：{problem['reason']}", file=sys.stderr)
    if manifest.get("error"):
        print(manifest["error"], file=sys.stderr)
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
