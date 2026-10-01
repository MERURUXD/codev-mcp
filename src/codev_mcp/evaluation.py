"""Explicit, condition-bound design requirements and their judgement.

The scan config (``validate_config``) and the design spec (``spec.py``) share
one requirement shape. Values are read from recorded CODE V results or the lens
readback; quantities the service derives itself (edge thickness, Airy diameter)
are marked ``service_calculated``. Every item is pass, fail or unknown: missing
data, a simulated source and printed precision that straddles a bound are all
unknown, never a pass.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .listing import ordinary_surface_rows, parse_fie_distortion

# Metrics the image-quality scan accepts; the design spec adds the rest.
METRICS = {
    "effective_focal_length", "f_number", "overall_length", "back_focal_length",
    "mtf", "spot_rms_radius", "spot_max_radius", "wavefront_rms_waves",
    "wavefront_strehl",
}
FIRST_ORDER_METRICS = {"effective_focal_length", "f_number", "overall_length", "back_focal_length"}
THICKNESS_METRICS = {"center_thickness_min", "center_thickness_max", "edge_thickness_min",
                     "air_center_thickness_min", "air_edge_thickness_min"}
SPEC_METRICS = METRICS | THICKNESS_METRICS | {"distortion_max_abs"}
LENGTH_METRICS = {"effective_focal_length", "overall_length", "back_focal_length",
                  "spot_rms_radius", "spot_max_radius"} | THICKNESS_METRICS
LENGTH_UNITS = {"mm", "cm", "inch"}
MICROMETERS_PER_UNIT = {"mm": 1000.0, "cm": 10000.0, "inch": 25400.0}
REQUIREMENT_KEYS = {"id", "metric", "unit", "field", "direction", "frequency",
                    "minimum", "maximum", "required"}
SPEC_OPTIONAL_KEYS = frozenset({"pending", "source", "note"})

# Marechal: RMS wavefront error <= lambda/14 (about 0.0714 waves) gives a Strehl
# ratio of about 0.8 (Born & Wolf, Principles of Optics, section 9.3). The preset
# uses the commonly quoted, slightly stricter 0.07 waves.
MARECHAL_RMS_WAVES = 0.07
MARECHAL_STREHL = 0.8
# Airy disk diameter to the first dark ring: 2.44 lambda F/# (Born & Wolf 8.5.2).
AIRY_DIAMETER_FACTOR = 2.44
SPOT_STATISTICS = {"rms": "rms_radius", "geometric_max": "max_radius"}

OAL_NOTE = ("CODE V OAL runs from surface 1 to the last surface before the image; "
            "the image distance is not included.")


def read_config(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    config = json.loads(raw)
    validate_config(config)
    return config, hashlib.sha256(raw).hexdigest()


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_requirement(item: dict, kinds, frequencies, *, metrics=METRICS,
                         optional_keys=frozenset(), units: str | None = None) -> None:
    """Check one requirement against the analyses and frequencies it will read."""
    if (not isinstance(item, dict) or not REQUIREMENT_KEYS <= set(item)
            or set(item) - REQUIREMENT_KEYS - set(optional_keys)):
        raise ValueError("Each requirement needs explicit ID, metric, unit, conditions, bounds and required")
    ident, metric = item["id"], item["metric"]
    if not isinstance(ident, str) or not ident or not isinstance(metric, str) or metric not in metrics:
        raise ValueError("Missing requirement ID or unsupported metric")
    if type(item["required"]) is not bool:
        raise ValueError("required must be boolean")
    pending = item.get("pending", False)
    if type(pending) is not bool:
        raise ValueError("pending must be boolean")
    for key in ("source", "note"):
        if key in item and not isinstance(item[key], str):
            raise ValueError(f"{key} must be text")
    lower, upper = item["minimum"], item["maximum"]
    if pending:
        if lower is not None or upper is not None:
            raise ValueError("A pending requirement leaves both bounds null")
    else:
        if lower is None and upper is None:
            raise ValueError("Requirement needs a bound")
        if any(v is not None and not _finite(v) for v in (lower, upper)):
            raise ValueError("Requirement bounds must be finite")
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("minimum exceeds maximum")
    field_number, direction, frequency = item["field"], item["direction"], item["frequency"]
    unconditioned = field_number is None and direction is None and frequency is None
    if metric == "mtf":
        if ("mtf" not in kinds or type(field_number) is not int or field_number < 1
                or direction not in {"tangential", "sagittal"}
                or frequency not in frequencies or item["unit"] != "ratio"):
            raise ValueError("MTF requirement needs requested field/direction/frequency and ratio unit")
    elif metric.startswith(("spot_", "wavefront_")):
        kind = "spot_diagram" if metric.startswith("spot_") else "wavefront"
        if (kind not in kinds or type(field_number) is not int or field_number < 1
                or direction is not None or frequency is not None):
            raise ValueError("Field requirement does not match analysis")
    elif metric in FIRST_ORDER_METRICS:
        if "first_order" not in kinds or not unconditioned:
            raise ValueError("First-order requirement has incompatible conditions")
    elif metric == "distortion_max_abs":
        if "native_plot" not in kinds or not unconditioned:
            raise ValueError("Distortion requirement needs native_plot (FIE) and no field conditions")
    elif not unconditioned:
        raise ValueError("Thickness requirement takes no field, direction or frequency")
    if metric in LENGTH_METRICS:
        if item["unit"] not in LENGTH_UNITS:
            raise ValueError("Length requirement must specify lens units")
        if units is not None and item["unit"] != units:
            raise ValueError("Length requirement unit differs from the declared units")
    elif item["unit"] != {"wavefront_rms_waves": "waves", "distortion_max_abs": "percent"}.get(metric, "ratio"):
        raise ValueError("Requirement unit is incorrect")


def validate_config(config: dict) -> None:
    if not isinstance(config, dict) or set(config) != {"schema_version", "analysis", "requirements", "export_indices"}:
        raise ValueError("Config needs schema_version, analysis, requirements and export_indices")
    if config["schema_version"] != 1:
        raise ValueError("Unsupported scan config version")
    analysis = config["analysis"]
    if not isinstance(analysis, dict) or set(analysis) != {"kinds", "fields", "frequencies", "spot_grid"}:
        raise ValueError("Invalid analysis config")
    kinds = analysis["kinds"]
    allowed = {"first_order", "spot_diagram", "mtf", "wavefront"}
    if (not isinstance(kinds, list) or not kinds or any(not isinstance(k, str) for k in kinds)
            or len(set(kinds)) != len(kinds) or set(kinds) - allowed):
        raise ValueError("Analysis kinds must be unique and supported")
    fields = analysis["fields"]
    if (fields is not None and (not isinstance(fields, list) or not fields
            or any(type(n) is not int or n < 1 for n in fields) or len(set(fields)) != len(fields))):
        raise ValueError("fields must be unique positive numbers or null")
    if "wavefront" in kinds and fields is not None:
        raise ValueError("WAV requires all fields; fields must be null")
    frequencies = analysis["frequencies"]
    if (not isinstance(frequencies, list) or not frequencies or len(frequencies) > 101
            or any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in frequencies)
            or frequencies != sorted(set(frequencies))):
        raise ValueError("frequencies must be unique, ascending, finite and nonnegative")
    grid = analysis["spot_grid"]
    if type(grid) is not int or not 2 <= grid <= 101:
        raise ValueError("spot_grid must be 2 through 101")
    exports = config["export_indices"]
    if not isinstance(exports, list) or any(type(n) is not int or n < 1 for n in exports) or len(set(exports)) != len(exports):
        raise ValueError("export_indices must contain unique positive sample indices")
    requirements = config["requirements"]
    if not isinstance(requirements, list):
        raise ValueError("requirements must be a list")
    ids = set()
    for item in requirements:
        validate_requirement(item, kinds, frequencies)
        if item["id"] in ids:
            raise ValueError("Duplicate requirement ID")
        ids.add(item["id"])
    if fields is not None and any(r["field"] is not None and r["field"] not in fields for r in requirements):
        raise ValueError("Requirement field is absent from analysis fields")


#: A value read back with EvaluateExpression carries about sixteen significant
#: digits, and the arithmetic inside CODE V (a thickness pushed onto a variable
#: bound of 0.1 reads back as 0.09999999999999896) differs from the exact value
#: by a few units of 1e-15 of the operands. A bound met within this relative
#: tolerance (against a scale of at least one lens unit) counts as met. It is
#: not the printed precision, which stays a separate, symmetric uncertainty.
MACHINE_RELATIVE_TOLERANCE = 1e-14


def _roundoff(value) -> float:
    """Roundoff tolerance for a quantity read back at machine precision."""
    return MACHINE_RELATIVE_TOLERANCE * max(abs(value), 1.0) if _finite(value) else 0.0


@dataclass
class Measurement:
    value: float | None
    unit: str | None
    precision: str
    uncertainty: float = 0.0
    tolerance: float = 0.0
    value_source: str = "codev"
    reason: str | None = None
    details: object = None


def _half_ulp(value: float) -> float:
    """Half a unit in the last printed place; trailing zeroes lost by float parsing widen it."""
    return float(Decimal(5).scaleb(Decimal(str(value)).as_tuple().exponent - 1))


def _spot_uncertainty(radius: float) -> float:
    # SPO prints diameters; halving is exact in binary, so 2r recovers the printed digits.
    return _half_ulp(2 * radius) / 2


def _missing(reason: str, unit: str | None = None) -> Measurement:
    return Measurement(None, unit, "", reason=reason)


def _sag(surface: dict, height: float) -> float | None:
    radius = surface.get("radius")
    if surface.get("radius_is_infinite") or radius is None:
        return 0.0
    if not _finite(radius) or radius == 0 or height > abs(radius):
        return None
    return height * height / (radius * (1 + math.sqrt(1 - (height / radius) ** 2)))


def thickness_segments(lens: dict) -> tuple[list[dict] | None, str | None]:
    """Glass elements and air gaps between real surfaces, with service-calculated edges.

    Glass segments run from each surface followed by glass to the next surface
    (a cemented doublet is two segments). Air gaps run between consecutive
    surfaces that touch glass, so dummy air-air surfaces such as a separate stop
    plane are skipped. The edge height is the larger of the two bounding
    surfaces' GetMaxAperture semi-apertures; edge values are None when that
    height is missing or exceeds a sphere's radius.
    """
    if lens.get("zoom_positions", 1) != 1:
        return None, "thickness checks support single-zoom lenses only"
    surfaces = sorted(lens.get("surfaces") or [], key=lambda s: s["number"])
    if any(str(s.get("glass") or "").upper() == "REFL" for s in surfaces):
        return None, "reflective surfaces are not supported by thickness checks"
    ordinary = [s for s in surfaces if s["role"] == "surface"]
    if len(ordinary) < 2:
        return None, "the lens has fewer than two ordinary surfaces"
    before = {s["number"]: (surfaces[i - 1].get("glass") if i else None) for i, s in enumerate(surfaces)}
    physical = [s for s in ordinary if before[s["number"]] or s.get("glass")]
    pairs = [(a, b, "glass") for a, b in zip(ordinary, ordinary[1:]) if a.get("glass")]
    pairs += [(a, b, "air") for a, b in zip(physical, physical[1:]) if not a.get("glass")]
    segments = []
    for a, b, medium in sorted(pairs, key=lambda item: item[0]["number"]):
        chain = [s for s in ordinary if a["number"] <= s["number"] < b["number"]]
        if any(s.get("thickness_is_infinite") or not _finite(s.get("thickness")) for s in chain):
            return None, f"surfaces {a['number']}-{b['number']} have no finite thickness"
        center = sum(s["thickness"] for s in chain)
        heights = [a.get("semi_aperture"), b.get("semi_aperture")]
        height = max(heights) if all(_finite(h) and h >= 0 for h in heights) else None
        sags = [_sag(a, height), _sag(b, height)] if height is not None else [None, None]
        edge = center + sags[1] - sags[0] if None not in sags else None
        segment = {"medium": medium, "glass": a.get("glass"), "surfaces": [a["number"], b["number"]],
                   "center": center, "edge": edge, "edge_height": height}
        if height is None:
            segment["edge_reason"] = f"surfaces {a['number']}-{b['number']}: semi-aperture unavailable"
        elif edge is None:
            steep = a if sags[0] is None else b
            segment["edge_reason"] = (f"surfaces {a['number']}-{b['number']}: edge height {height:.6g} exceeds "
                                      f"|R| {abs(steep['radius']):.6g} of surface {steep['number']}")
        segments.append(segment)
    return segments, None


def _thickness(metric: str, lens: dict) -> Measurement:
    unit = lens.get("units")
    segments, reason = thickness_segments(lens)
    if segments is None:
        return _missing(reason, unit)
    medium = "air" if metric.startswith("air_") else "glass"
    edge = "edge" in metric
    if edge:
        try:
            ordinary_surface_rows(lens.get("raw_listing") or "", len(lens["surfaces"]))
        except ValueError as exc:
            return _missing(f"plain spherical surfaces cannot be confirmed: {exc}", unit)
    chosen = [s for s in segments if s["medium"] == medium]
    if not chosen:
        return _missing(f"the lens has no {medium} segments", unit)
    values = [s["edge" if edge else "center"] for s in chosen]
    if any(v is None for v in values):
        found = _missing("; ".join(s["edge_reason"] for s in chosen if s["edge"] is None), unit)
        found.details = chosen
        return found
    value = max(values) if metric.endswith("_max") else min(values)
    if edge:
        precision = ("service calculated from lens-unit thickness, sphere radii and the larger "
                     "GetMaxAperture semi-aperture of the two surfaces; CODE V AUT MNE/MAE use "
                     "their own ray-based edge and may differ")
        source = "service_calculated"
    else:
        precision = "center thickness read back from the lens (EvaluateExpression)"
        source = "codev"
    return Measurement(value, unit, precision, value_source=source, details=chosen,
                       tolerance=_roundoff(value))


def _distortion(results: dict, lens: dict) -> Measurement:
    raw = (results.get("native-field_aberration") or {}).get("raw_output")
    if not raw:
        return _missing("FIE field_aberration native output is missing", "percent")
    try:
        table = parse_fie_distortion(raw)
    except ValueError as exc:
        return _missing(f"FIE distortion table not parsed: {exc}", "percent")
    reference = [w for w in lens.get("wavelengths", []) if w.get("is_reference")]
    if len(reference) != 1 or not _finite(reference[0].get("micrometers")):
        return _missing("lens reference wavelength is unavailable", "percent")
    if abs(reference[0]["micrometers"] * 1000 - table["wavelength_nm"]) > 0.05 + 1e-9:
        return _missing("FIE wavelength differs from the lens reference wavelength", "percent")
    fields = lens.get("fields") or []
    if not fields or any((f.get("x_angle") or 0) != 0 or not _finite(f.get("y_angle")) for f in fields):
        return _missing("FIE distortion is checked only for Y-only angle fields", "percent")
    full = max(abs(f["y_angle"]) for f in fields)
    if abs(table["rows"][-1]["angle_deg"] - full) > 0.005 + 1e-9:
        return _missing("FIE full-field angle differs from the largest lens field", "percent")
    row = max(table["rows"], key=lambda r: abs(r["distortion_percent"]))
    uncertainty = float(Decimal(5).scaleb(Decimal(row["distortion_text"]).as_tuple().exponent - 1))
    return Measurement(abs(row["distortion_percent"]), "percent",
                       "largest |distortion| of the 11 FIE samples (relative field 0.0-1.0 in 0.1 steps) "
                       "at the reference wavelength, printed per cent; CODE V references it to the "
                       "paraxial (ideal) image height",
                       uncertainty=uncertainty,
                       details={"wavelength_nm": table["wavelength_nm"], "rows": table["rows"],
                                "at_relative_field": row["relative_field"],
                                "full_field_percent": table["rows"][-1]["distortion_percent"]})


def measure(results: dict, requirement: dict, lens: dict) -> Measurement:
    metric = requirement["metric"]
    number = requirement["field"]
    if metric in FIRST_ORDER_METRICS:
        result = results.get("first_order")
        if not result:
            return _missing("first-order result is missing")
        value = result.get(metric)
        # Only BFL comes from the printed listing; OAL is the (OAL Zn) database item.
        printed = metric == "back_focal_length"
        precision = result.get("precision_note", "")
        if metric == "overall_length":
            precision = (precision + " " + OAL_NOTE).strip()
        return Measurement(value, "ratio" if metric == "f_number" else result.get("units"), precision,
                           uncertainty=_half_ulp(value) if printed and _finite(value) else 0.0,
                           tolerance=0.0 if printed else _roundoff(value))
    if metric == "mtf":
        result = results.get("mtf") or {}
        frequencies = result.get("frequencies") or []
        curves = [c for c in result.get("curves", []) if c.get("field_number") == number]
        if len(curves) == 1 and requirement["frequency"] in frequencies:
            value = curves[0].get(requirement["direction"], [])[frequencies.index(requirement["frequency"])]
            return Measurement(value, "ratio", "numeric MTF result; inspect raw output and backend precision")
        return _missing("missing field/frequency")
    if metric.startswith("spot_"):
        result = results.get(f"spot-{number}")
        if not result:
            return _missing(f"spot result for field {number} is missing")
        value = result.get(metric.removeprefix("spot_"))
        return Measurement(value, result.get("units"),
                           "native SPO statistics printed precision (printed as diameter, reported as radius)",
                           uncertainty=_spot_uncertainty(value) if _finite(value) else 0.0)
    if metric.startswith("wavefront_"):
        result = results.get("wavefront") or {}
        fields = [r for r in result.get("fields", []) if r.get("field_number") == number]
        if len(fields) != 1:
            return _missing("missing WAV field")
        value = fields[0].get(metric.removeprefix("wavefront_"))
        return Measurement(value, "waves" if metric == "wavefront_rms_waves" else "ratio",
                           result.get("precision_note", ""),
                           uncertainty=_half_ulp(value) if _finite(value) else 0.0)
    if metric == "distortion_max_abs":
        return _distortion(results, lens)
    return _thickness(metric, lens)


def judge(value, uncertainty: float, minimum, maximum, tolerance: float = 0.0) -> tuple[str, str | None]:
    """Compare a value with its bounds; the bounds are relaxed by the roundoff tolerance."""
    lower = None if minimum is None else minimum - tolerance
    upper = None if maximum is None else maximum + tolerance
    if (lower is not None and value + uncertainty < lower) or (upper is not None and value - uncertainty > upper):
        return "fail", None
    if (lower is None or value - uncertainty >= lower) and (upper is None or value + uncertainty <= upper):
        if ((minimum is not None and value - uncertainty < minimum)
                or (maximum is not None and value + uncertainty > maximum)):
            return "pass", f"at the bound within the machine tolerance {tolerance:.3g}"
        return "pass", None
    return "unknown", "printed precision crosses threshold"


def _item(req: dict, found: Measurement, source: str, *, threshold_source="spec") -> dict:
    status, reason = "unknown", None
    if source != "codev":
        reason = "simulated source has no optical conclusion"
    elif req.get("pending"):
        reason = "threshold pending (待补)"
    elif found.reason:
        reason = found.reason
    elif found.unit != req["unit"]:
        reason = "unit mismatch or missing data"
    elif not _finite(found.value):
        reason = "missing or non-finite value"
    else:
        status, reason = judge(found.value, found.uncertainty, req["minimum"], req["maximum"],
                               found.tolerance)
    item = {"id": req["id"], "status": status, "value": found.value, "unit": found.unit,
            "precision": found.precision, "reason": reason, "required": req["required"],
            "metric": req["metric"], "minimum": req["minimum"], "maximum": req["maximum"],
            "uncertainty": found.uncertainty, "tolerance": found.tolerance,
            "value_source": found.value_source,
            "threshold_source": threshold_source}
    for key in ("field", "direction", "frequency", "source", "note", "pending"):
        if req.get(key) is not None:
            item[key] = req[key]
    if found.details is not None:
        item["details"] = found.details
    return item


def overall(items: list[dict], source: str) -> str:
    required = [r for r in items if r["required"]]
    return ("fail" if any(r["status"] == "fail" for r in required)
            else "pass" if required and source == "codev" and all(r["status"] == "pass" for r in required)
            else "unknown")


def evaluate(requirements: list[dict], results: dict, lens: dict, source: str) -> dict:
    items = [_item(req, measure(results, req, lens), source) for req in requirements]
    return {"status": overall(items, source), "requirements": items}


def _preset_marechal(preset: dict, results: dict, lens: dict, source: str) -> list[dict]:
    items = []
    for lens_field in lens.get("fields") or []:
        number = lens_field["number"]
        for metric, bounds in (("wavefront_rms_waves", (None, MARECHAL_RMS_WAVES)),
                               ("wavefront_strehl", (MARECHAL_STREHL, None))):
            req = {"id": f"preset.marechal.{metric.removeprefix('wavefront_')}.f{number}",
                   "metric": metric, "unit": "waves" if metric.endswith("waves") else "ratio",
                   "field": number, "direction": None, "frequency": None,
                   "minimum": bounds[0], "maximum": bounds[1], "required": preset["required"],
                   "source": preset.get("source")}
            item = _item(req, measure(results, req, lens), source, threshold_source="preset:marechal")
            item["precision"] += (" WAV nominal focus, all wavelengths; RMS in waves of the WAV "
                                  "listing; CODE V Strehl is an approximation.")
            items.append(item)
    return items


def _airy_diameter(results: dict, lens: dict) -> tuple[float | None, str | None]:
    units = lens.get("units")
    object_surface = next((s for s in lens.get("surfaces", []) if s["role"] == "object"), None)
    if object_surface is None or not object_surface.get("thickness_is_infinite"):
        return None, "Airy diameter uses the infinite-conjugate F/#; finite object distance unsupported"
    reference = [w for w in lens.get("wavelengths", []) if w.get("is_reference")]
    f_number = (results.get("first_order") or {}).get("f_number")
    if len(reference) != 1 or not _finite(reference[0].get("micrometers")) or units not in MICROMETERS_PER_UNIT:
        return None, "reference wavelength or lens units unavailable"
    if not _finite(f_number) or f_number <= 0:
        return None, "first-order F/# unavailable"
    return AIRY_DIAMETER_FACTOR * reference[0]["micrometers"] / MICROMETERS_PER_UNIT[units] * f_number, None


def _preset_airy(preset: dict, results: dict, lens: dict, source: str) -> list[dict]:
    statistic = preset["statistic"]
    airy, airy_reason = _airy_diameter(results, lens)
    items = []
    for lens_field in lens.get("fields") or []:
        number = lens_field["number"]
        radius_req = {"metric": f"spot_{SPOT_STATISTICS[statistic]}", "field": number}
        found = measure(results, radius_req, lens)
        if _finite(found.value):
            found = Measurement(2 * found.value, found.unit,
                                f"spot {statistic} diameter = 2 x native SPO radius (native SPO prints "
                                "diameters); compared with the service-calculated Airy diameter "
                                "2.44 x reference wavelength x first-order F/#",
                                uncertainty=2 * found.uncertainty)
        if airy_reason and not found.reason:
            found.reason = airy_reason
        req = {"id": f"preset.airy_spot.{statistic}.f{number}",
               "metric": f"spot_{statistic}_diameter", "unit": lens.get("units"),
               "field": number, "direction": None, "frequency": None,
               "minimum": None, "maximum": airy, "required": preset["required"],
               "source": preset.get("source")}
        item = _item(req, found, source, threshold_source="service_calculated:airy_diameter")
        item["airy_diameter"] = airy
        items.append(item)
    return items


PRESETS = {"marechal": _preset_marechal, "airy_spot": _preset_airy}


def _close(a, b, *, abs_tol: float, rel_tol: float = 1e-9) -> bool:
    return _finite(a) and _finite(b) and math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


def _condition(ident: str, entry: dict, expected, actual, source: str, check) -> dict:
    status, reason = "unknown", None
    if source != "codev":
        reason = "simulated source has no optical conclusion"
    elif entry.get("pending"):
        reason = "condition pending (待补)"
    else:
        status, reason = check()
    item = {"id": ident, "status": status, "expected": expected, "actual": actual,
            "reason": reason, "required": True}
    for key in ("source", "pending"):
        if entry.get(key) is not None:
            item[key] = entry[key]
    return item


def evaluate_conditions(system: dict, lens: dict, source: str) -> list[dict]:
    """Compare the spec's system definition with the lens that was evaluated."""
    items = []
    aperture = system.get("aperture")
    if aperture is not None:
        actual = {"kind": (lens.get("aperture") or {}).get("kind"),
                  "value": (lens.get("aperture") or {}).get("value")}

        def check_aperture():
            if actual["kind"] != aperture["kind"]:
                return "unknown", ("lens aperture is defined by another type; state F/# or EPD "
                                   "as a requirement with a tolerance instead")
            ok = _close(actual["value"], aperture["value"], abs_tol=1e-9)
            return ("pass", None) if ok else ("fail", "aperture value differs")
        items.append(_condition("system.aperture", aperture,
                                {"kind": aperture.get("kind"), "value": aperture.get("value")},
                                actual, source, check_aperture))
    fields = system.get("fields")
    if fields is not None:
        actual = [{"x_angle": f.get("x_angle"), "y_angle": f.get("y_angle"), "weight": f.get("weight")}
                  for f in lens.get("fields") or []]

        def check_fields():
            expected = fields["values"]
            if len(expected) != len(actual):
                return "fail", "field count differs"
            for want, have in zip(expected, actual):
                if not (_close(want.get("x_angle", 0.0), have["x_angle"] or 0.0, abs_tol=1e-6)
                        and _close(want["y_angle"], have["y_angle"], abs_tol=1e-6)):
                    return "fail", "field angle differs (tolerance 1e-6 degree)"
                if want.get("weight") is not None and not _close(want["weight"], have["weight"], abs_tol=1e-9):
                    return "fail", "field weight differs"
            return "pass", None
        items.append(_condition("system.fields", fields, fields.get("values"), actual, source, check_fields))
    waves = system.get("wavelengths")
    if waves is not None:
        lens_waves = lens.get("wavelengths") or []
        actual = {"values": [{"nm": w["micrometers"] * 1000 if _finite(w.get("micrometers")) else None,
                              "weight": w.get("weight")} for w in lens_waves],
                  "reference": next((w["number"] for w in lens_waves if w.get("is_reference")), None)}

        def check_waves():
            expected = waves["values"]
            if len(expected) != len(actual["values"]):
                return "fail", "wavelength count differs"
            for want, have in zip(expected, actual["values"]):
                if not _close(want["nm"], have["nm"], abs_tol=1e-6):
                    return "fail", "wavelength differs (tolerance 1e-6 nm)"
                if want.get("weight") is not None and not _close(want["weight"], have["weight"], abs_tol=1e-9):
                    return "fail", "wavelength weight differs"
            if waves.get("reference") is not None and waves["reference"] != actual["reference"]:
                return "fail", "reference wavelength differs"
            return "pass", None
        items.append(_condition("system.wavelengths", waves,
                                {"values": waves.get("values"), "reference": waves.get("reference")},
                                actual, source, check_waves))
    catalogs = system.get("glass_catalogs")
    if catalogs is not None:
        glasses = sorted({s["glass"] for s in lens.get("surfaces", []) if s.get("glass")})

        def check_catalogs():
            unknown = [g for g in glasses if "_" not in g]
            outside = [g for g in glasses if "_" in g and g.rsplit("_", 1)[1].upper()
                       not in {c.upper() for c in catalogs["allowed"]}]
            if outside:
                return "fail", "glasses outside the allowed catalogs: " + ", ".join(outside)
            if unknown:
                return "unknown", "catalog not recorded for: " + ", ".join(unknown)
            return "pass", None
        items.append(_condition("system.glass_catalogs", catalogs, catalogs.get("allowed"),
                                glasses, source, check_catalogs))
    return items


def evaluate_spec(spec: dict, results: dict, lens: dict, source: str) -> dict:
    """Judge one evaluated lens against a validated design spec.

    Conditions compare the lens with the spec's system definition, requirements
    use the shared format and criteria expand explicitly selected presets per
    lens field. The overall status covers every required item of all three.
    """
    conditions = evaluate_conditions(spec.get("system") or {}, lens, source)
    requirements = evaluate(spec["requirements"], results, lens, source)["requirements"]
    criteria = []
    for preset in spec.get("criteria", []):
        criteria.extend(PRESETS[preset["preset"]](preset, results, lens, source))
    everything = conditions + requirements + criteria
    return {"status": overall(everything, source), "source": source,
            "demonstration": spec.get("demonstration", False),
            "conditions": conditions, "requirements": requirements, "criteria": criteria,
            "notes": [OAL_NOTE,
                      "Edge thickness and the Airy diameter are service calculated from CODE V data.",
                      "Missing data, simulated results and pending thresholds are unknown, never pass."]}
