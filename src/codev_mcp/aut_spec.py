"""Typed, multi-stage AUT specification with constraints (D6/D7).

Everything the candidate process sends to CODE V is built here from validated
fields; no user text becomes a command. The layout follows the D5 probe and the
checkpoint C2 decisions (local validation records):

* variables are opened one by one after ``FRZ S0..I``; a zero-cycle run must
  list exactly the requested parameters (composite bending rows allowed);
* specific constraints (EFL, EFY, OAL, IMD, DIY, CT, ET) are read back from the
  final Active/Inactive tables at their printed precision;
* general thickness constraints (MXT, MNT, MNE, MNA, MAE) are only set; AUT
  prints no values for them, so the service checks the candidate separately;
* AUT output is redirected with ``OUT`` to a service file and parsed from it;
* the original CCY/THC control codes are restored after each stage;
* a stage may first change field data (weights, angles, vignetting factors) with
  typed edits, and a ``field_ramp`` expands into one such stage per step, so a
  large field is approached in steps that each start from the previous
  candidate (E5).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .safety import format_float

SCHEMA_VERSION = 1
MAX_STAGES = 20
MAX_RAMP_STEPS = 12
MAX_CYCLES = 500
MAX_WALL_SECONDS = 1800
DEFAULT_WALL_SECONDS = 600
SPECIFIC_OPERANDS = {"EFL": None, "EFY": None, "OAL": "surfaces", "IMD": None,
                     "DIY": "field", "CT": "surface", "ET": "surface"}
GENERAL_CONSTRAINTS = ("MXT", "MNT", "MNE", "MNA", "MAE")
#: General constraint -> the D2 thickness metric that checks it on the candidate.
GENERAL_CHECKS = {"MXT": ("center_thickness_max", "maximum"), "MNT": ("center_thickness_min", "minimum"),
                  "MNE": ("edge_thickness_min", "minimum"), "MNA": ("air_center_thickness_min", "minimum"),
                  "MAE": ("air_edge_thickness_min", "minimum")}
ERROR_FUNCTION_KEYS = {"MXC", "MNC", "TAR", "IMP", "DEL", "WTA"}
#: Field data a stage may edit before AUT runs, with CODE V's item and the accepted range.
FIELD_CHANGE_ITEMS = {"weight": ("wtf", 0.0, 1e6), "y_angle": ("yan", -89.0, 89.0), "x_angle": ("xan", -89.0, 89.0),
                      "vux": ("vux", -0.99, 0.99), "vlx": ("vlx", -0.99, 0.99),
                      "vuy": ("vuy", -0.99, 0.99), "vly": ("vly", -0.99, 0.99)}
NATIVE_PARAMETER = {"radius": "CUY", "thickness": "THI"}
CONTROL_CODE = {"radius": "CCY", "thickness": "THC"}
BOUND_ITEM = {"radius": "RDY", "thickness": "THI"}
#: Service convention when a constraint gives no tolerance: relative 1e-6 of the
#: target (at least 1e-6 absolute), for =, < and > alike.
DEFAULT_RELATIVE_TOLERANCE = 1e-6

COMPLETION = re.compile(r"Normal AUTO Completion\s*-\s*([^\r\n]+)")
CYCLE_ERROR = re.compile(r"CYCLE NUMBER\s+(\d+):\s*(?:\r?\n\s*)+ERR\. F\.\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?)")
NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?"
SPECIFIC_ROW = re.compile(
    r"^\s*(EFL|EFY|OAL S\d+\.\.\d+|IMD|DIY F\d+|CT S\d+|ET S\d+)\s+([=<>])\s+"
    rf"({NUMBER})\s+({NUMBER})\s+({NUMBER})(?:\s+(?:{NUMBER}|\*\*))?\s*$")
GENERAL_ROW = re.compile(rf"^\s*((?:Mn|Mx) (?:CT|ET|AC|AE|AI) S\d+|GL [AB] S\d+)\s+({NUMBER})\s*$")
VARIABLE_TOKEN = re.compile(r"\b([A-Z]{2,3}) S(\d+)\b")


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def read_aut_spec(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"AUT spec is not UTF-8 JSON: {exc}") from None
    validate_aut_spec(expand_field_ramp(spec))
    return spec, hashlib.sha256(raw).hexdigest()


def legacy_spec(surface: int, parameter: str, lower: float, upper: float, target: float,
                cycles: int, wall_seconds: int) -> dict:
    """The original one-variable command line, expressed as a one-stage spec."""
    return {"schema_version": SCHEMA_VERSION, "kind": "aut_spec", "name": "single-variable",
            "wall_seconds": wall_seconds,
            "stages": [{"name": "single", "variables": [
                {"surface": surface, "parameter": parameter, "lower": lower, "upper": upper}],
                "error_function": {"MXC": cycles, "MNC": 1, "TAR": target}}]}


def _stage_keys(stage: dict) -> None:
    allowed = {"name", "variables", "constraints", "general_constraints", "error_function",
               "lens_changes", "set_vignetting", "ramp_step"}
    if not isinstance(stage, dict) or set(stage) - allowed or not {"name", "variables", "error_function"} <= set(stage):
        raise ValueError(f"Each stage needs name, variables and error_function; allowed keys {sorted(allowed)}")


def validate_stage(stage: dict) -> None:
    _stage_keys(stage)
    if not isinstance(stage["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", stage["name"]):
        raise ValueError("Stage names are 1-32 letters, digits, dot, underscore or minus")
    variables = stage["variables"]
    if not isinstance(variables, list) or not 1 <= len(variables) <= 60:
        raise ValueError("A stage needs 1 to 60 variables")
    seen = set()
    for item in variables:
        if not isinstance(item, dict) or set(item) - {"surface", "parameter", "lower", "upper"}:
            raise ValueError("A variable takes surface, parameter and optional lower/upper")
        if type(item.get("surface")) is not int or item["surface"] < 1 or item.get("parameter") not in NATIVE_PARAMETER:
            raise ValueError("A variable is an existing ordinary surface radius or thickness")
        key = (item["surface"], item["parameter"])
        if key in seen:
            raise ValueError(f"Duplicate variable {key}")
        seen.add(key)
        lower, upper = item.get("lower"), item.get("upper")
        if any(v is not None and not _finite(v) for v in (lower, upper)):
            raise ValueError("Variable bounds must be finite numbers or null")
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("A variable lower bound must be below its upper bound")
    constraints = stage.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 60:
        raise ValueError("constraints must be a list of at most 60 entries")
    relations: dict[tuple, set] = {}
    for item in constraints:
        if not isinstance(item, dict) or item.get("operand") not in SPECIFIC_OPERANDS:
            raise ValueError("Constraint operand must be one of " + ", ".join(SPECIFIC_OPERANDS))
        qualifier = SPECIFIC_OPERANDS[item["operand"]]
        allowed = {"operand", "relation", "value", "tolerance", "source"} | ({qualifier} if qualifier else set())
        if set(item) - allowed or item.get("relation") not in {"=", "<", ">"} or not _finite(item.get("value")):
            raise ValueError(f"Constraint {item['operand']} takes {sorted(allowed)} with relation =, < or >")
        if qualifier == "field" and (type(item.get("field")) is not int or item["field"] < 1):
            raise ValueError("DIY needs a field number")
        if qualifier == "surface" and (type(item.get("surface")) is not int or item["surface"] < 1):
            raise ValueError(f"{item['operand']} needs a surface number")
        if qualifier == "surfaces" and "surfaces" in item:
            pair = item["surfaces"]
            if (not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair)
                    or not 1 <= pair[0] < pair[1]):
                raise ValueError("OAL surfaces must be [first, last] with 1 <= first < last")
        if "tolerance" in item and (not _finite(item["tolerance"]) or item["tolerance"] <= 0):
            raise ValueError("tolerance must be a positive number")
        if "source" in item and not isinstance(item["source"], str):
            raise ValueError("constraint source must be text")
        group = relations.setdefault(_target_key(item), set())
        if item["relation"] in group or ("=" in (group | {item["relation"]}) and group):
            raise ValueError(f"Conflicting or duplicate relations for {item['operand']}")
        group.add(item["relation"])
    general = stage.get("general_constraints", {})
    if not isinstance(general, dict) or set(general) - set(GENERAL_CONSTRAINTS):
        raise ValueError("general_constraints may set " + ", ".join(GENERAL_CONSTRAINTS))
    if any(not _finite(v) or v < 0 for v in general.values()):
        raise ValueError("general constraint values must be finite and nonnegative")
    function = stage["error_function"]
    if not isinstance(function, dict) or set(function) - ERROR_FUNCTION_KEYS or "MXC" not in function:
        raise ValueError("error_function needs MXC and may set " + ", ".join(sorted(ERROR_FUNCTION_KEYS - {"MXC"})))
    if type(function["MXC"]) is not int or not 1 <= function["MXC"] <= MAX_CYCLES:
        raise ValueError(f"MXC must be an integer 1..{MAX_CYCLES}")
    if "MNC" in function and (type(function["MNC"]) is not int or not 1 <= function["MNC"] <= function["MXC"]):
        raise ValueError("MNC must be an integer 1..MXC")
    for key, minimum in (("TAR", 0.0), ("IMP", 0.0)):
        if key in function and (not _finite(function[key]) or function[key] < minimum):
            raise ValueError(f"{key} must be finite and nonnegative")
    for key in ("DEL", "WTA"):
        if key in function and (not _finite(function[key]) or function[key] <= 0):
            raise ValueError(f"{key} must be finite and positive")
    changes = stage.get("lens_changes", [])
    if not isinstance(changes, list) or len(changes) > 30:
        raise ValueError("lens_changes must be a list of at most 30 typed weight edits")
    for item in changes:
        if not isinstance(item, dict):
            raise ValueError("lens_changes entries are typed objects")
        if item.get("target") == "field":
            if item.get("parameter") not in FIELD_CHANGE_ITEMS:
                raise ValueError("A field change edits " + ", ".join(FIELD_CHANGE_ITEMS))
            _, low, high = FIELD_CHANGE_ITEMS[item["parameter"]]
            if set(item) != {"target", "field", "parameter", "value"} or type(item["field"]) is not int \
                    or item["field"] < 1 or not _finite(item["value"]) or not low <= item["value"] <= high:
                raise ValueError(f"A field {item['parameter']} change needs field >= 1 and a value in {low}..{high}")
        elif item.get("target") == "wavelength":
            if item.get("parameter") != "weight":
                raise ValueError("lens_changes support field data and wavelength weights only")
            if set(item) != {"target", "wavelength", "parameter", "value"} or type(item["wavelength"]) is not int \
                    or item["wavelength"] < 1 or type(item["value"]) is not int or not 0 <= item["value"] <= 1000:
                raise ValueError("A wavelength weight change needs wavelength >= 1 and an integer weight 0..1000")
        else:
            raise ValueError("lens_changes target must be field or wavelength")
    if "ramp_step" in stage and (type(stage["ramp_step"]) is not int or stage["ramp_step"] < 1):
        raise ValueError("ramp_step must be a positive integer")
    if type(stage.get("set_vignetting", False)) is not bool:
        raise ValueError("set_vignetting must be boolean")


def validate_aut_spec(spec) -> None:
    if not isinstance(spec, dict) or set(spec) - {"schema_version", "kind", "name", "description",
                                                   "wall_seconds", "stages"}:
        raise ValueError("AUT spec keys: schema_version, kind, name, description, wall_seconds, stages "
                         "(a field_ramp is expanded into stages before validation)")
    if spec.get("schema_version") != SCHEMA_VERSION or spec.get("kind") != "aut_spec":
        raise ValueError("Unsupported AUT spec version or kind")
    if not isinstance(spec.get("name"), str) or not spec["name"].strip():
        raise ValueError("AUT spec needs a name")
    wall = spec.get("wall_seconds", DEFAULT_WALL_SECONDS)
    if type(wall) is not int or not 5 <= wall <= MAX_WALL_SECONDS:
        raise ValueError(f"wall_seconds must be an integer 5..{MAX_WALL_SECONDS}")
    stages = spec.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError(f"An AUT spec needs 1 to {MAX_STAGES} stages")
    names = set()
    for stage in stages:
        validate_stage(stage)
        if stage["name"] in names:
            raise ValueError(f"Duplicate stage name {stage['name']}")
        names.add(stage["name"])


RAMP_KEYS = {"name", "steps", "stage", "then"}
RAMP_STEP_ITEMS = ("y_angle", "x_angle", "weight", "vux", "vlx", "vuy", "vly")


def _ramp_changes(step: dict) -> list[dict]:
    """The typed field edits of one ramp step, in field order."""
    changes = []
    for entry in step["fields"]:
        for parameter in RAMP_STEP_ITEMS:
            if parameter in entry:
                changes.append({"target": "field", "field": entry["field"], "parameter": parameter,
                                "value": entry[parameter]})
    return changes


def expand_field_ramp(spec) -> dict:
    """Turn ``field_ramp`` into ordinary stages; a spec without one is returned unchanged.

    Each step becomes one stage built from the template ``stage``: the step's
    field edits come first (then the template's own ``lens_changes``), and
    ``ramp_step`` records the step number. ``then`` (optional) lists ordinary
    stages that run after the last step, for example a polish with more variables.
    Steps run in order, every one from the previous stage's saved candidate;
    nothing is published until the final candidate is accepted. The result is an ordinary spec that ``validate_aut_spec``
    accepts (the ramp itself is kept by the caller as the input spec).
    """
    if not isinstance(spec, dict) or "field_ramp" not in spec:
        return spec
    ramp = spec["field_ramp"]
    if not isinstance(ramp, dict) or set(ramp) - RAMP_KEYS or not {"steps", "stage"} <= set(ramp):
        raise ValueError("field_ramp needs steps and stage, and may set name")
    name = ramp.get("name", "ramp")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,24}", name):
        raise ValueError("field_ramp name is 1-24 letters, digits, dot, underscore or minus")
    steps = ramp["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_RAMP_STEPS:
        raise ValueError(f"A field_ramp needs 1 to {MAX_RAMP_STEPS} steps")
    template = ramp["stage"]
    if not isinstance(template, dict) or {"name", "lens_changes", "ramp_step"} & set(template) - {"lens_changes"}:
        raise ValueError("The ramp stage template is a stage without name or ramp_step")
    expanded = list(spec.get("stages", []))
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or set(step) - {"fields"} or not isinstance(step.get("fields"), list) \
                or not 1 <= len(step["fields"]) <= 10:
            raise ValueError(f"Ramp step {number} needs 1 to 10 fields entries")
        seen = set()
        for entry in step["fields"]:
            if not isinstance(entry, dict) or set(entry) - ({"field"} | set(RAMP_STEP_ITEMS)) \
                    or type(entry.get("field")) is not int or entry["field"] < 1 or len(entry) < 2:
                raise ValueError(f"Ramp step {number}: an entry is a field number with at least one of "
                                 + ", ".join(RAMP_STEP_ITEMS))
            if entry["field"] in seen:
                raise ValueError(f"Ramp step {number} edits field {entry['field']} twice")
            seen.add(entry["field"])
        stage = json.loads(json.dumps(template))
        stage["name"] = f"{name}-{number}"
        stage["ramp_step"] = number
        stage["lens_changes"] = _ramp_changes(step) + list(template.get("lens_changes", []))
        expanded.append(stage)
    then = ramp.get("then", [])
    if not isinstance(then, list):
        raise ValueError("field_ramp then is a list of ordinary stages")
    expanded += json.loads(json.dumps(then))
    result = {key: value for key, value in spec.items() if key not in {"field_ramp", "stages"}}
    result["stages"] = expanded
    return result


def _target_key(item: dict) -> tuple:
    qualifier = SPECIFIC_OPERANDS[item["operand"]]
    value = item.get(qualifier) if qualifier else None
    return item["operand"], tuple(value) if isinstance(value, list) else value


def constraint_label(item: dict, surface_count: int) -> str:
    """The row name CODE V prints for a constraint (OAL always gets its range)."""
    operand = item["operand"]
    if operand == "OAL":
        first, last = item.get("surfaces") or [1, surface_count - 2]
        return f"OAL S{first}..{last}"
    if operand == "DIY":
        return f"DIY F{item['field']}"
    if operand in {"CT", "ET"}:
        return f"{operand} S{item['surface']}"
    return operand


def constraint_commands(constraints: list[dict], surface_count: int) -> list[str]:
    """One command per constrained quantity; a two-sided bound stays one command."""
    groups: dict[str, dict[str, float]] = {}
    for item in constraints:
        groups.setdefault(constraint_label(item, surface_count), {})[item["relation"]] = item["value"]
    commands = []
    for label, relations in groups.items():
        parts = [label.lower()]
        for relation in ("=", ">", "<"):
            if relation in relations:
                parts.append(f"{relation} {format_float(relations[relation])}")
        commands.append(" ".join(parts))
    return commands


def open_commands(stage: dict) -> list[str]:
    """Freeze everything, then open exactly the requested parameters."""
    return ["frz s0..i"] + [f"{CONTROL_CODE[v['parameter']].lower()} s{v['surface']} 0"
                             for v in stage["variables"]]


def lens_change_commands(stage: dict) -> list[str]:
    commands = []
    for item in stage.get("lens_changes", []):
        if item["target"] == "field":
            commands.append(f"{FIELD_CHANGE_ITEMS[item['parameter']][0]} f{item['field']} {format_float(item['value'])}")
        else:
            commands.append(f"wtw w{item['wavelength']} {int(item['value'])}")
    return commands


def aut_commands(stage: dict, surface_count: int, wall_seconds: int) -> list[str]:
    function = stage["error_function"]
    commands = ["aut", "err cdv", f"mxc {function['MXC']}", f"mnc {function.get('MNC', 1)}",
                f"tim {max(1, math.ceil(wall_seconds / 60))}", f"tar {format_float(function.get('TAR', 0.0))}"]
    for key in ("IMP", "DEL", "WTA"):
        if key in function:
            commands.append(f"{key.lower()} {format_float(function[key])}")
    commands.append("vli y")
    for item in stage["variables"]:
        bounds = [f"> {format_float(item['lower'])}" if item.get("lower") is not None else "",
                  f"< {format_float(item['upper'])}" if item.get("upper") is not None else ""]
        if any(bounds):
            commands.append(" ".join([f"{BOUND_ITEM[item['parameter']].lower()} s{item['surface']}"]
                                     + [b for b in bounds if b]))
    commands += constraint_commands(stage.get("constraints", []), surface_count)
    commands += [f"{key.lower()} {format_float(value)}"
                 for key, value in stage.get("general_constraints", {}).items()]
    return commands


def restore_commands(controls: dict[str, dict[str, str]], stage: dict) -> list[str]:
    """Undo FRZ and the opened variables where they changed a numeric CCY/THC code.

    After ``FRZ S0..I`` every numeric code is 100 and the stage's variables are
    0; solve codes such as PIM are never touched. Only codes that now differ
    from the original are set back.
    """
    opened = {(v["surface"], CONTROL_CODE[v["parameter"]]) for v in stage["variables"]}
    commands = []
    for number in sorted(controls, key=int):
        for code in ("CCY", "THC"):
            original = controls[number].get(code, "")
            current = "0" if (int(number), code) in opened else "100"
            if original in {"0", "100"} and original != current:
                commands.append(f"{code.lower()} s{number} {original}")
    return commands


def requested_parameters(stage: dict) -> set[str]:
    return {f"{NATIVE_PARAMETER[v['parameter']]} S{v['surface']}" for v in stage["variables"]}


def parse_variable_table(output: str) -> set[str]:
    """Every parameter named in VARIABLE LIST, composite rows included."""
    if "VARIABLE LIST" not in output:
        raise ValueError("AUT did not list its variables")
    section = output.split("VARIABLE LIST", 1)[1]
    section = re.split(r"\n\s*\d+\s+VARIABLES\b|\* Multiple entries", section, maxsplit=1)[0]
    return {f"{kind} S{number}" for kind, number in VARIABLE_TOKEN.findall(section)}


def _final_block(text: str) -> str:
    """The last cycle block that carries constraint tables, before the completion line."""
    segments = re.split(r"CYCLE NUMBER\s+\d+:", text)
    for segment in reversed(segments[1:]):
        if any(header in segment for header in ("Active Constraints", "Inactive Constraints",
                                                "Specific Constraints:")):
            return segment
    return ""


def parse_aut_listing(text: str) -> dict:
    """Terminal state of one AUT run; missing pieces raise instead of being guessed."""
    if not text or re.search(r"(?m)^\s*Error:", text):
        raise ValueError("AUT reported an error or produced no output")
    completion = list(COMPLETION.finditer(text))
    cycles = [(int(n), float(v)) for n, v in CYCLE_ERROR.findall(text)]
    if not completion or not cycles:
        raise ValueError("AUT output lacks a normal completion or cycle error functions")
    body = text[:completion[-1].start()]
    block = _final_block(body)
    rows, general, frozen = [], [], []
    in_frozen = False
    for line in block.splitlines():
        match = SPECIFIC_ROW.match(line)
        if match:
            name, relation, target, value, diff = match.groups()
            rows.append({"label": name, "relation": relation, "target": float(target),
                         "value": float(value), "diff": float(diff),
                         "printed": {"target": target, "value": value, "diff": diff}})
            in_frozen = False
            continue
        general_match = GENERAL_ROW.match(line)
        if general_match and not in_frozen:
            general.append(general_match.group(1))
        if "Frozen Thickness Violations" in line:
            in_frozen = True
            continue
        if in_frozen:
            names = re.findall(r"(?:Mn|Mx) (?:CT|ET|AC|AE|AI) S\d+", line)
            if names:
                frozen += names
            elif line.strip():
                in_frozen = False
    values = [value for _, value in cycles]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("AUT printed a non-finite error function")
    return {"completion": completion[-1].group(1).strip(), "initial_error": cycles[0][1],
            "final_cycle": cycles[-1][0], "final_error": cycles[-1][1], "constraint_rows": rows,
            "active_general": general, "frozen_violations": sorted(set(frozen)),
            "has_constraint_tables": bool(block)}


def _printed_half_step(text: str) -> float:
    mantissa, _, exponent = text.upper().partition("E")
    decimals = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    return 0.5 * 10 ** (int(exponent or 0) - decimals)


def judge_constraints(stage: dict, parsed: dict, surface_count: int) -> list[dict]:
    """Each requested constraint against the final printed row (diff = value - target)."""
    rows = {(row["label"], row["relation"]): row for row in parsed["constraint_rows"]}
    items = []
    for item in stage.get("constraints", []):
        label = constraint_label(item, surface_count)
        row = rows.get((label, item["relation"]))
        record = {"operand": item["operand"], "label": label, "relation": item["relation"],
                  "target": item["value"], "source": item.get("source")}
        if row is None:
            items.append({**record, "status": "unknown", "reason": "not printed in the final constraint table"})
            continue
        slack = _printed_half_step(row["printed"]["diff"])
        tolerance = item.get("tolerance", DEFAULT_RELATIVE_TOLERANCE * max(1.0, abs(item["value"])))
        record["tolerance"] = tolerance
        if item["relation"] == "=":
            excess = abs(row["diff"]) - tolerance
        elif item["relation"] == "<":
            excess = row["diff"] - tolerance
        else:
            excess = -row["diff"] - tolerance
        ok, unsure = excess <= 0, 0 < excess <= slack
        items.append({**record, "value": row["value"], "diff": row["diff"], "printed": row["printed"],
                      "status": "satisfied" if ok else "unknown" if unsure else "violated",
                      "precision": "final AUT table, value 6 and diff 4 significant digits"})
    return items
