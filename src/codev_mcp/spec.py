"""Versioned design spec: system definition, evaluation profile and requirements.

A spec is plain JSON supplied by the user. Nothing here has course or project
defaults: thresholds, catalogs and frequencies all come from the file, and the
only built-in criteria are the explicitly selected presets in ``evaluation``.
Every entry may carry a free-text ``source`` (task text, page, "待补"), and an
entry whose value is not yet known is written as ``"pending": true`` so it is
judged unknown instead of being guessed.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .evaluation import (LENGTH_UNITS, PRESETS, SPEC_METRICS, SPEC_OPTIONAL_KEYS,
                         SPOT_STATISTICS, validate_requirement)

# Same analysis names as the comparison workflow (kept here to avoid an import cycle).
ANALYSES = ("first_order", "spot_diagram", "mtf", "wavefront", "native_plot")
TOP_KEYS = {"schema_version", "kind", "name", "units", "system", "evaluation", "criteria", "requirements"}
OPTIONAL_TOP_KEYS = {"description", "demonstration"}
SYSTEM_KEYS = {"aperture", "fields", "wavelengths", "glass_catalogs"}
PRESET_ANALYSES = {"marechal": {"wavefront"}, "airy_spot": {"spot_diagram", "first_order"}}


def read_spec(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Design spec is not UTF-8 JSON: {exc}") from None
    validate_spec(spec)
    return spec, hashlib.sha256(raw).hexdigest()


def _number(value, name: str, *, positive=False, nonnegative=False) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must not be negative")


def _entry(system: dict, key: str, required: set[str]) -> dict | None:
    """Return a system entry, allowing only its annotations when it is pending."""
    entry = system.get(key)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ValueError(f"system.{key} must be an object or null")
    pending = entry.get("pending", False)
    if type(pending) is not bool:
        raise ValueError(f"system.{key}.pending must be boolean")
    if "source" in entry and not isinstance(entry["source"], str):
        raise ValueError(f"system.{key}.source must be text")
    extra = set(entry) - required - {"pending", "source"}
    if extra:
        raise ValueError(f"system.{key} has unknown keys: {sorted(extra)}")
    if pending:
        if set(entry) & required:
            raise ValueError(f"pending system.{key} must not state values")
        return None
    if missing := required - set(entry):
        raise ValueError(f"system.{key} needs {sorted(missing)}")
    return entry


def _weight(item: dict, name: str) -> None:
    if item.get("weight") is not None:
        _number(item["weight"], name + ".weight", nonnegative=True)


def validate_system(system) -> None:
    if not isinstance(system, dict) or set(system) - SYSTEM_KEYS:
        raise ValueError("system may contain only aperture, fields, wavelengths and glass_catalogs")
    aperture = _entry(system, "aperture", {"kind", "value"})
    if aperture is not None:
        if aperture["kind"] not in {"epd", "fno", "na", "nao"}:
            raise ValueError("system.aperture.kind must be epd, fno, na or nao")
        _number(aperture["value"], "system.aperture.value", positive=True)
    fields = _entry(system, "fields", {"values"})
    if fields is not None:
        values = fields["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= 25:
            raise ValueError("system.fields.values needs 1 to 25 fields")
        for i, item in enumerate(values, 1):
            if not isinstance(item, dict) or set(item) - {"x_angle", "y_angle", "weight"} or "y_angle" not in item:
                raise ValueError(f"system.fields.values[{i}] needs y_angle and optional x_angle, weight (degrees)")
            _number(item["y_angle"], f"field {i} y_angle")
            _number(item.get("x_angle", 0.0), f"field {i} x_angle")
            _weight(item, f"field {i}")
    waves = _entry(system, "wavelengths", {"values", "reference"})
    if waves is not None:
        values = waves["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= 21:
            raise ValueError("system.wavelengths.values needs 1 to 21 wavelengths")
        for i, item in enumerate(values, 1):
            if not isinstance(item, dict) or set(item) - {"nm", "weight"} or "nm" not in item:
                raise ValueError(f"system.wavelengths.values[{i}] needs nm and optional weight")
            _number(item["nm"], f"wavelength {i} nm", positive=True)
            _weight(item, f"wavelength {i}")
        reference = waves["reference"]
        if reference is not None and (type(reference) is not int or not 1 <= reference <= len(values)):
            raise ValueError("system.wavelengths.reference must name one listed wavelength")
    catalogs = _entry(system, "glass_catalogs", {"allowed"})
    if catalogs is not None:
        allowed = catalogs["allowed"]
        if (not isinstance(allowed, list) or not allowed
                or any(not isinstance(c, str) or not c.strip() or "_" in c for c in allowed)):
            raise ValueError("system.glass_catalogs.allowed needs catalog names such as SCHOTT")


def validate_evaluation(evaluation) -> None:
    if not isinstance(evaluation, dict) or set(evaluation) != {"analyses", "mtf_frequencies", "spot_grid"}:
        raise ValueError("evaluation needs analyses, mtf_frequencies and spot_grid")
    analyses = evaluation["analyses"]
    if (not isinstance(analyses, list) or not analyses or len(set(analyses)) != len(analyses)
            or any(a not in ANALYSES for a in analyses)):
        raise ValueError("evaluation.analyses must be a unique subset of " + ", ".join(ANALYSES))
    frequencies = evaluation["mtf_frequencies"]
    if (not isinstance(frequencies, list) or not frequencies or len(frequencies) > 101
            or any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in frequencies)
            or frequencies != sorted(set(frequencies))):
        raise ValueError("mtf_frequencies must be unique, ascending, finite and nonnegative cycles/mm")
    grid = evaluation["spot_grid"]
    if type(grid) is not int or not 2 <= grid <= 101:
        raise ValueError("spot_grid must be 2 through 101")


def validate_criteria(criteria, analyses) -> None:
    if not isinstance(criteria, list):
        raise ValueError("criteria must be a list")
    seen = set()
    for item in criteria:
        if not isinstance(item, dict) or item.get("preset") not in PRESETS:
            raise ValueError("Each criterion names a preset: " + ", ".join(sorted(PRESETS)))
        allowed = {"preset", "required", "source"} | ({"statistic"} if item["preset"] == "airy_spot" else set())
        if set(item) - allowed or "required" not in item or type(item["required"]) is not bool:
            raise ValueError(f"criterion {item['preset']} takes {sorted(allowed)} with boolean required")
        if "source" in item and not isinstance(item["source"], str):
            raise ValueError("criterion source must be text")
        if item["preset"] == "airy_spot" and item.get("statistic") not in SPOT_STATISTICS:
            raise ValueError("airy_spot needs statistic rms or geometric_max")
        key = (item["preset"], item.get("statistic"))
        if key in seen:
            raise ValueError("Duplicate criterion")
        seen.add(key)
        if missing := PRESET_ANALYSES[item["preset"]] - set(analyses):
            raise ValueError(f"criterion {item['preset']} needs analyses {sorted(missing)}")


def validate_spec(spec) -> None:
    if not isinstance(spec, dict):
        raise ValueError("Design spec must be a JSON object")
    if missing := TOP_KEYS - set(spec):
        raise ValueError(f"Design spec is missing {sorted(missing)}")
    if extra := set(spec) - TOP_KEYS - OPTIONAL_TOP_KEYS:
        raise ValueError(f"Design spec has unknown keys {sorted(extra)}")
    if spec["schema_version"] != 1 or spec["kind"] != "design_spec":
        raise ValueError("Unsupported design spec version or kind")
    if not isinstance(spec["name"], str) or not spec["name"].strip():
        raise ValueError("Design spec needs a name")
    if "description" in spec and not isinstance(spec["description"], str):
        raise ValueError("description must be text")
    if type(spec.get("demonstration", False)) is not bool:
        raise ValueError("demonstration must be boolean")
    if spec["units"] not in LENGTH_UNITS:
        raise ValueError("units must be mm, cm or inch")
    validate_system(spec["system"])
    validate_evaluation(spec["evaluation"])
    analyses = spec["evaluation"]["analyses"]
    validate_criteria(spec["criteria"], analyses)
    requirements = spec["requirements"]
    if not isinstance(requirements, list):
        raise ValueError("requirements must be a list")
    ids = set()
    for item in requirements:
        validate_requirement(item, analyses, spec["evaluation"]["mtf_frequencies"],
                             metrics=SPEC_METRICS, optional_keys=SPEC_OPTIONAL_KEYS, units=spec["units"])
        if item["id"] in ids:
            raise ValueError(f"Duplicate requirement ID {item['id']}")
        if item["id"].startswith(("system.", "preset.")):
            raise ValueError("Requirement IDs starting with system. or preset. are reserved")
        ids.add(item["id"])


def check_lens(spec: dict, lens: dict) -> None:
    """Reject a lens the spec cannot be judged on before any analysis runs."""
    if lens["units"] != spec["units"]:
        raise ValueError(f"Design spec units {spec['units']} differ from lens units {lens['units']}")
    if lens.get("zoom_positions") != 1:
        raise ValueError("Design spec evaluation supports single-zoom lenses only")
    numbers = {f["number"] for f in lens.get("fields") or []}
    missing = sorted({r["field"] for r in spec["requirements"] if r["field"] is not None} - numbers)
    if missing:
        raise ValueError(f"Requirement fields {missing} do not exist in the lens")
