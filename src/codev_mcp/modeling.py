"""Typed command construction and native surface-number planning for M3."""

from __future__ import annotations

from dataclasses import dataclass

from .checkpoints import LensSnapshot, numbers_equal
from .errors import ParameterError, UnsupportedError
from .models import CreateLensRequest, StructureOperation, StructureRequest, StructureStep, Units
from .safety import (
    INFINITE_RADIUS_THRESHOLD,
    INFINITE_THICKNESS_THRESHOLD,
    check_number,
    format_float,
    validate_glass_name,
)


def _radius(value: float | None) -> str:
    if value is None:
        return "0"
    number = check_number(value, field_name="radius")
    if abs(number) >= INFINITE_RADIUS_THRESHOLD:
        raise ParameterError("A spherical radius must be below the infinite-radius threshold.")
    return format_float(number)


def _thickness(value: float | None) -> str:
    number = check_number(value, field_name="thickness")
    if abs(number) >= INFINITE_THICKNESS_THRESHOLD:
        raise ParameterError("A thickness must be below the infinite-thickness threshold.")
    return format_float(number)


def _glass(value: str | None) -> str:
    if value is None:
        return ""
    name = validate_glass_name(value)
    material, separator, catalog = name.rpartition("_")
    if not separator or not material or not catalog:
        raise ParameterError(
            "glass must be a fully qualified CODE V name such as BK7_SCHOTT."
        )
    return " " + name


def create_commands(request: CreateLensRequest) -> list[str]:
    """Build fixed CODE V commands; incomplete LEN states need raw output handling."""
    aperture = check_number(request.aperture_value, field_name="aperture_value", minimum=1e-9)
    if request.aperture_kind in {"na", "nao"} and aperture >= 1:
        raise UnsupportedError("NA and NAO values of 1 or greater are not verified for writing.")
    wavelengths = [check_number(value, field_name="wavelength_nm", minimum=1e-9,
                                maximum=1e6) for value in request.wavelengths_nm]
    if any(left <= right for left, right in zip(wavelengths, wavelengths[1:])):
        raise ParameterError(
            "wavelengths_nm must be strictly descending in native CODE V order."
        )
    angles = [
        (
            check_number(item.x_angle, field_name="x_angle", minimum=-89, maximum=89),
            check_number(item.y_angle, field_name="y_angle", minimum=-89, maximum=89),
        )
        for item in request.fields
    ]
    unit = {Units.MM: "M", Units.CM: "C", Units.INCH: "I"}[request.units]
    commands = [
        "len",
        "rdm",
        f"dim {unit}",
        "wl " + " ".join(map(format_float, wavelengths)),
        f"{request.aperture_kind} {format_float(aperture)}",
        "xan " + " ".join(format_float(x) for x, _ in angles),
        "yan " + " ".join(format_float(y) for _, y in angles),
    ]
    for index, surface in enumerate(request.surfaces):
        radius = _radius(surface.radius)
        thickness = _thickness(surface.thickness)
        commands.append(f"ins si {radius} {thickness}{_glass(surface.glass)}")
    commands.append(f"sto s{request.stop_surface}")
    if request.image_solve == "pim":
        # Paraxial image solve on the thickness of the last ordinary surface;
        # the requested thickness is only the starting value.
        commands.append("pim yes")
    return commands


def created_lens_differences(request: CreateLensRequest, snapshot: LensSnapshot) -> list[str]:
    """Compare a complete readback with the requested simple prescription."""
    differences: list[str] = []
    if snapshot.zoom_positions != 1 or snapshot.surface_count != len(request.surfaces) + 2:
        differences.append("surface or zoom count")
    if snapshot.units != request.units.value or snapshot.stop_surface != request.stop_surface:
        differences.append("units or stop surface")
    if snapshot.aperture_kind != request.aperture_kind or not numbers_equal(
        snapshot.aperture_value, request.aperture_value
    ):
        differences.append("system aperture")
    expected_solves = ["PIM"] if request.image_solve == "pim" else []
    if (not snapshot.relation_data_complete or snapshot.pickups
            or [item.upper() for item in snapshot.solves] != expected_solves):
        differences.append("unexpected solve or pickup relationship")
    last = len(request.surfaces)
    if request.image_solve == "pim":
        controls = snapshot.variable_controls.get(str(last)) or {}
        if "PIM" not in {str(value).upper() for value in controls.values()}:
            differences.append(f"surface {last} is not controlled by the PIM solve")
    if snapshot.aperture_commands or snapshot.zoom_aperture_commands:
        differences.append("unexpected explicit surface aperture")
    zoom = snapshot.zooms[0] if len(snapshot.zooms) == 1 else None
    if zoom is None:
        return differences + ["missing zoom position"]
    for number, requested in enumerate(request.surfaces, 1):
        actual = next((item for item in zoom.surfaces if item.number == number), None)
        if actual is None:
            differences.append(f"missing surface {number}")
            continue
        if requested.radius is None or requested.radius == 0:
            radius_ok = actual.radius_infinite
        else:
            radius_ok = not actual.radius_infinite and numbers_equal(actual.radius, requested.radius)
        # The PIM solve re-derives the last thickness; its requested value is a start.
        thickness_ok = (request.image_solve == "pim" and number == len(request.surfaces)
                        and actual.thickness is not None and not actual.thickness_infinite) or numbers_equal(
            actual.thickness, requested.thickness)
        if not radius_ok or not thickness_ok:
            differences.append(f"surface {number} radius or thickness")
        if (actual.glass or "").upper() != (requested.glass or "").upper():
            differences.append(f"surface {number} glass")
    if len(zoom.fields) != len(request.fields):
        differences.append("field count")
    else:
        for number, (actual, requested) in enumerate(zip(zoom.fields, request.fields), 1):
            if not numbers_equal(actual.x_angle, requested.x_angle) or not numbers_equal(
                actual.y_angle, requested.y_angle
            ):
                differences.append(f"field {number} angles")
    if len(snapshot.wavelengths) != len(request.wavelengths_nm):
        differences.append("wavelength count")
    else:
        for number, (actual, requested) in enumerate(
            zip(snapshot.wavelengths, request.wavelengths_nm), 1
        ):
            if not numbers_equal(actual.micrometers, requested / 1000):
                differences.append(f"wavelength {number}")
    return differences


@dataclass
class PlannedStructureStep:
    operation: StructureOperation
    commands: list[str]
    result: StructureStep
    surface_count: int


def structure_differences(
    before: LensSnapshot, after: LensSnapshot, step: PlannedStructureStep,
) -> list[str]:
    """Check one native operation against its exact surface mapping."""
    problems: list[str] = []
    if after.surface_count != step.surface_count or after.stop_surface != step.result.stop_surface:
        problems.append("surface count or stop")
    left = before.to_dict()
    right = after.to_dict()
    for payload in (left, right):
        for key in ("surface_count", "stop_surface", "zooms", "glass_catalogs",
                    "variable_controls"):
            payload.pop(key, None)
    if left != right:
        problems.append("non-surface lens state")
    if (len(before.variable_controls) != (before.surface_count or 0)
            or len(after.variable_controls) != (after.surface_count or 0)):
        problems.append("native variable-control rows incomplete")
    for old_number, new_number in step.result.old_to_new.items():
        if before.variable_controls.get(str(old_number)) != after.variable_controls.get(str(new_number)):
            problems.append(f"surface {old_number} variable controls")
    if step.operation.kind == "insert_sphere":
        inserted_controls = after.variable_controls.get(str(step.operation.before_surface))
        if (inserted_controls is None or inserted_controls.get("CCY") != "100"
                or inserted_controls.get("THC") != "100"
                or inserted_controls.get("GLC") not in {"", "100"}):
            problems.append("inserted surface variable controls")
    if len(before.zooms) != 1 or len(after.zooms) != 1:
        return problems + ["zoom position count"]
    if before.zooms[0].fields != after.zooms[0].fields:
        problems.append("fields")
    old_surfaces = {row.number: row for row in before.zooms[0].surfaces}
    new_surfaces = {row.number: row for row in after.zooms[0].surfaces}
    for old_number, new_number in step.result.old_to_new.items():
        old = old_surfaces.get(old_number)
        new = new_surfaces.get(new_number)
        if old is None or new is None:
            problems.append(f"mapped surface {old_number}")
            continue
        old_payload, new_payload = old.to_dict(), new.to_dict()
        for payload in (old_payload, new_payload):
            for key in ("number", "role", "is_stop"):
                payload.pop(key, None)
        if old_payload != new_payload:
            problems.append(f"surface {old_number} changed outside the operation")
    if step.operation.kind == "insert_sphere":
        inserted = step.operation.before_surface
        row = new_surfaces.get(inserted)
        if row is None:
            problems.append("inserted surface missing")
        else:
            requested = step.operation
            radius_ok = row.radius_infinite if requested.radius is None or requested.radius == 0 else (
                not row.radius_infinite and numbers_equal(row.radius, requested.radius)
            )
            if not radius_ok or not numbers_equal(row.thickness, requested.thickness):
                problems.append("inserted surface dimensions")
            if (row.glass or "").upper() != (requested.glass or "").upper():
                problems.append("inserted surface glass")
            if row.apertures or row.role != "surface":
                problems.append("inserted surface is not ordinary")
    return problems


def plan_structure(
    request: StructureRequest, *, surface_count: int, stop_surface: int,
) -> list[PlannedStructureStep]:
    """Plan each operation against the numbering *at that step*."""
    if surface_count < 3 or not 1 <= stop_surface < surface_count - 1:
        raise UnsupportedError("The current lens has no verifiable ordinary stop surface.")
    steps: list[PlannedStructureStep] = []
    for operation in request.operations:
        last = surface_count - 1
        if operation.kind == "insert_sphere":
            before = operation.before_surface
            if before is None or not 1 <= before <= last:
                raise ParameterError(f"before_surface must be in 1..{last}.")
            command = f"ins s{before} {_radius(operation.radius)} {_thickness(operation.thickness)}{_glass(operation.glass)}"
            mapping = {old: old if old < before else old + 1 for old in range(surface_count)}
            stop_surface = mapping[stop_surface]
            surface_count += 1
            commands = [command]
        elif operation.kind == "delete_surface":
            number = operation.surface
            if number is None or not 1 <= number < last:
                raise ParameterError(f"surface must be in 1..{last - 1}.")
            if surface_count <= 3:
                raise UnsupportedError("The final ordinary surface cannot be deleted.")
            mapping = {old: old if old < number else old - 1
                       for old in range(surface_count) if old != number}
            if number == stop_surface:
                new_stop = operation.new_stop_surface
                if new_stop is None or not 1 <= new_stop < surface_count - 2:
                    raise ParameterError("Deleting the stop requires a valid new_stop_surface after deletion.")
                stop_surface = new_stop
                commands = [f"del s{number}", f"sto s{new_stop}"]
            else:
                if operation.new_stop_surface is not None:
                    raise ParameterError("new_stop_surface is only accepted when deleting the stop.")
                stop_surface = mapping[stop_surface]
                commands = [f"del s{number}"]
            surface_count -= 1
        else:
            number = operation.surface
            if number is None or not 1 <= number < last:
                raise ParameterError(f"surface must be in 1..{last - 1}.")
            mapping = {old: old for old in range(surface_count)}
            stop_surface = number
            commands = [f"sto s{number}"]
        steps.append(PlannedStructureStep(
            operation=operation,
            commands=commands,
            result=StructureStep(kind=operation.kind, old_to_new=mapping,
                                 stop_surface=stop_surface),
            surface_count=surface_count,
        ))
    return steps
