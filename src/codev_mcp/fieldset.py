"""Field set replacement planning and the image-height normalisation of field angles (E3).

``resolve_field_set`` and ``field_set_commands`` are shared by the real and the
simulated backend, so both apply the same rules. CODE V keeps weights and
vignetting factors by field number when the field count changes (probe
``local validation records``): a shorter set drops the
tail, a longer set starts its new fields at weight 1 and factors 0.

The calculation half is a small command line tool that turns "maximum field
angle and relative fields" into angles. It sends nothing to CODE V:

    python -m codev_mcp.fieldset --max-angle 23.5 --relative 0 0.5 0.7 0.85 1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from .checkpoints import numbers_equal
from .errors import ParameterError
from .models import FieldSetReplacement, LensField
from .safety import check_number, format_float

MAX_FIELDS = 10
ANGLE_LIMIT = 89.0
VIGNETTING_LIMIT = 0.99
WEIGHT_LIMIT = 1e6
VIGNETTING_NAMES = ("vux", "vlx", "vuy", "vly")


def _kept(current: list[LensField], number: int, name: str, default: float) -> float:
    for item in current:
        if item.number == number:
            value = getattr(item, name)
            return default if value is None else float(value)
    return default


def resolve_field_set(spec: FieldSetReplacement, current: list[LensField]) -> list[LensField]:
    """The complete field set a replacement asks for, with the omitted items resolved."""
    if not 1 <= len(spec.fields) <= MAX_FIELDS:
        raise ParameterError(f"A field set needs 1 to {MAX_FIELDS} fields.")
    target: list[LensField] = []
    for number, item in enumerate(spec.fields, 1):
        weight = (_kept(current, number, "weight", 1.0) if item.weight is None
                  else check_number(item.weight, field_name=f"field {number} weight",
                                    minimum=0.0, maximum=WEIGHT_LIMIT))
        factors = {}
        for name in VIGNETTING_NAMES:
            given = getattr(item, name)
            factors[name] = (_kept(current, number, name, 0.0) if given is None
                             else check_number(given, field_name=f"field {number} {name}",
                                               minimum=-VIGNETTING_LIMIT, maximum=VIGNETTING_LIMIT))
        target.append(LensField(
            number=number,
            x_angle=check_number(item.x_angle, field_name=f"field {number} x_angle",
                                 minimum=-ANGLE_LIMIT, maximum=ANGLE_LIMIT),
            y_angle=check_number(item.y_angle, field_name=f"field {number} y_angle",
                                 minimum=-ANGLE_LIMIT, maximum=ANGLE_LIMIT),
            weight=weight, **factors,
        ))
    return target


def _predicted(current: list[LensField], number: int, name: str) -> float:
    """What CODE V holds for a weight or factor right after the count changed."""
    return _kept(current, number, name, 1.0 if name == "weight" else 0.0)


def field_set_commands(target: list[LensField], current: list[LensField]) -> list[str]:
    """Fixed commands for a replacement: the angle vectors, then only what differs.

    XAN and YAN with as many values as the target has fields set the count and
    the angles; weights and vignetting factors that CODE V does not already hold
    by field number are written per field, like any single-field edit.
    """
    commands = ["XAN " + " ".join(format_float(item.x_angle) for item in target),
                "YAN " + " ".join(format_float(item.y_angle) for item in target)]
    for item in target:
        for name, keyword in (("weight", "WTF"), ("vux", "VUX"), ("vlx", "VLX"),
                              ("vuy", "VUY"), ("vly", "VLY")):
            wanted = getattr(item, name)
            if not numbers_equal(_predicted(current, item.number, name), wanted):
                commands.append(f"{keyword} F{item.number} {format_float(wanted)}")
    return commands


def field_set_differences(expected: list[LensField], actual: list[LensField]) -> list[str]:
    """Differences between the requested field set and what was read back."""
    if len(expected) != len(actual):
        return [f"field count: expected {len(expected)}, read {len(actual)}"]
    problems = []
    for want, got in zip(expected, actual):
        for name in ("x_angle", "y_angle", "weight", *VIGNETTING_NAMES):
            if not numbers_equal(getattr(want, name), getattr(got, name)):
                problems.append(f"field {want.number} {name}: expected {getattr(want, name)!r}, "
                                f"read {getattr(got, name)!r}")
    return problems


# ------------------------------------------------------------- normalisation


CONVENTIONS = {
    "height": (
        "Relative field by image height: the field angle is atan(relative * tan(maximum angle)). "
        "This is CODE V's relative field for a distortion-free image, where the paraxial image "
        "height grows with tan(angle); it is not linear in the angle."
    ),
    "angle": "Relative field by angle: the field angle is relative * maximum angle (linear in the angle).",
}


def relative_field_angles(max_angle: float, relative: list[float], mode: str = "height") -> list[float]:
    """Field angles in degrees for relative fields of a maximum angle."""
    maximum = check_number(max_angle, field_name="max_angle", minimum=1e-6, maximum=ANGLE_LIMIT)
    if mode not in CONVENTIONS:
        raise ParameterError(f"mode must be one of {sorted(CONVENTIONS)}.")
    if not relative or len(relative) > MAX_FIELDS:
        raise ParameterError(f"1 to {MAX_FIELDS} relative fields are needed.")
    angles = []
    for value in relative:
        fraction = check_number(value, field_name="relative field", minimum=0.0, maximum=1.0)
        if mode == "height":
            angles.append(math.degrees(math.atan(fraction * math.tan(math.radians(maximum)))))
        else:
            angles.append(fraction * maximum)
    return angles


def relative_heights(max_angle: float, angles: list[float]) -> list[float]:
    """Relative image heights (tan ratio) of field angles, the reverse of ``height`` mode."""
    scale = math.tan(math.radians(max_angle))
    return [math.tan(math.radians(angle)) / scale for angle in angles]


def normalisation_report(max_angle: float, relative: list[float], mode: str = "height",
                         weights: list[float] | None = None) -> dict:
    angles = relative_field_angles(max_angle, relative, mode)
    if weights is not None and len(weights) != len(relative):
        raise ParameterError("weights needs one value per relative field.")
    fields = []
    for index, (fraction, angle) in enumerate(zip(relative, angles)):
        entry = {"y_angle": float(format_float(angle)), "relative": fraction}
        if weights is not None:
            entry["weight"] = weights[index]
        fields.append(entry)
    return {
        "mode": mode,
        "convention": CONVENTIONS[mode],
        "max_angle": max_angle,
        "linear_angle_for_comparison": [float(format_float(item * max_angle)) for item in relative],
        "relative_image_height_of_result": [float(format_float(item))
                                            for item in relative_heights(max_angle, angles)],
        "field_set": {"fields": [{key: value for key, value in entry.items() if key != "relative"}
                                 for entry in fields]},
        "note": "The angles are y field angles with x = 0; CODE V's field type is not changed.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="由最大视场角与相对视场计算视场角（不连接 CODE V）")
    parser.add_argument("--max-angle", type=float, required=True, help="最大视场角，度")
    parser.add_argument("--relative", type=float, nargs="+", required=True, help="相对视场 0..1")
    parser.add_argument("--mode", choices=sorted(CONVENTIONS), default="height",
                        help="height：按像高（正切）归一化，默认；angle：按角度线性")
    parser.add_argument("--weights", type=float, nargs="+", help="每个视场的权重（可选）")
    parser.add_argument("--output", type=Path, help="把 update_lens 的 field_set 请求写入新文件（拒绝覆盖）")
    args = parser.parse_args(argv)
    try:
        report = normalisation_report(args.max_angle, args.relative, args.mode, args.weights)
        FieldSetReplacement.model_validate(report["field_set"])
    except (ParameterError, ValueError) as exc:
        print(getattr(exc, "message", str(exc)), file=sys.stderr)
        return 1
    if args.output:
        if args.output.exists():
            print(f"输出文件已存在：{args.output}", file=sys.stderr)
            return 1
        args.output.write_text(json.dumps({"field_set": report["field_set"]}, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
