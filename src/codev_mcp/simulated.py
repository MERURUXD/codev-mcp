"""Simulated backend used for automated tests and skeleton development.

Everything this backend returns is derived from built-in sample data and
deterministic arithmetic; nothing here touches CODE V. Results are therefore
always marked with source=simulated, and the service must never present them as
machine verified evidence for real CODE V behaviour.

All built-in prescriptions are synthetic test data. The legacy dbgauss
filename selector is kept for compatibility; it selects an invented model,
not the vendor lens. First-order constants are test values, not a ray-traced
solution for that geometry. No vendor example file is read or distributed.
"""

from __future__ import annotations

import base64
import math
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .backend import Backend, check_native_plot_options, check_wavefront_options
from .fieldset import resolve_field_set
from .modeling import create_commands, plan_structure
from .errors import (
    ComputationError,
    InternalError,
    NotFoundError,
    NotReadyError,
    ParameterError,
    SessionInvalidError,
    UnsupportedError,
)
from .models import (
    AnalysisRequest,
    AnalysisSettings,
    AnalysisSnapshot,
    ApertureInfo,
    CapabilityInfo,
    CreateLensRequest,
    EditOutcome,
    FieldSetOutcome,
    FirstOrderResult,
    ImagePayload,
    LensData,
    LensField,
    LensWavelength,
    MtfCurve,
    MtfResult,
    MtfType,
    NativePlotResult,
    NativePlotType,
    WavefrontField,
    WavefrontResult,
    ParameterEdit,
    SaveResult,
    Source,
    SpotDiagramResult,
    StatusInfo,
    StructureRequest,
    StructureResult,
    SurfaceData,
    SurfaceAperture,
    SurfaceRole,
    TaskInfo,
    TaskState,
    Units,
    UpdateRequest,
    UpdateResult,
    VIGNETTING_FACTORS,
)
from . import plotting

GLASS_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")

DIMENSION_TO_UNITS = {0: Units.INCH, 1: Units.CM, 2: Units.MM}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SampleLens:
    """Built-in sample data: surfaces plus first order and setup values."""

    def __init__(
        self,
        name: str,
        title: str,
        rows: list[tuple[str, str | None, str | None, str | None, bool]],
        *,
        stop_surface: int,
        zoom_positions: int = 1,
        solves: dict[int, str] | None = None,
        pickups: dict[int, str] | None = None,
        first_order: dict[str, float] | None = None,
        fields: list[tuple[float, float, float]] | None = None,
        wavelengths: list[tuple[float, float]] | None = None,
        reference_wavelength: int = 1,
        aperture: tuple[str, float] = ("epd", 0.0),
        surface_apertures: dict[int, float] | None = None,
        vignetting: list[tuple[float, float, float, float]] | None = None,
    ) -> None:
        self.name = name
        self.title = title
        self.rows = rows
        self.stop_surface = stop_surface
        self.zoom_positions = zoom_positions
        self.solves = solves or {}
        self.pickups = pickups or {}
        self.first_order = first_order or {}
        self.fields = fields or [(0.0, 0.0, 1.0)]
        self.wavelengths = wavelengths or [(0.5876, 1.0)]
        self.reference_wavelength = reference_wavelength
        self.aperture = aperture
        self.surface_apertures = surface_apertures or {}
        #: (VUX, VLX, VUY, VLY) per field; zero when the sample has none.
        self.vignetting = vignetting or [(0.0, 0.0, 0.0, 0.0)] * len(self.fields)


def _infinity_or(value: str | None) -> tuple[float | None, bool]:
    if value is None:
        return None, False
    if value.strip().upper() in {"INFINITY", "INF", "PLANE"}:
        return None, True
    return float(value), False


DBGAUSS = SampleLens(
    "dbgauss",
    "Synthetic compound test model",
    [
        ("OBJ", "INFINITY", "INFINITY", None, False),
        ("1", "64.12345", "9.123456", "BSM24_OHARA", False),
        ("2", "170.76543", "0.456789", None, False),
        ("3", "48.12345", "11.234560", "SK1_SCHOTT", False),
        ("4", "INFINITY", "4.654321", "F15_SCHOTT", False),
        ("5", "31.87654", "13.765432", None, False),
        ("STO", "INFINITY", "10.345678", None, True),
        ("7", "-36.12345", "4.654321", "F15_SCHOTT", False),
        ("8", "INFINITY", "9.876543", "SK16_SCHOTT", False),
        ("9", "-47.56789", "0.456789", None, False),
        ("10", "510.12345", "7.345678", "SK16_SCHOTT", False),
        ("11", "-70.45678", "61.234567", None, False),
        ("IMG", "INFINITY", "0.000000", None, False),
    ],
    stop_surface=6,
    solves={11: "PIM"},
    first_order={
        "effective_focal_length": 100.000123456789,
        "back_focal_length": 61.2346,
        "front_focal_length": -29.1234,
        "f_number": 2.0,
        "image_distance": 61.2346,
        "overall_length": 81.9134,
        "paraxial_image_height": 23.4567,
        "entrance_pupil_diameter": 50.0,
        "entrance_pupil_distance": 53.1234,
        "exit_pupil_diameter": 56.1234,
        "exit_pupil_distance": -49.1234,
    },
    fields=[(0.0, 0.0, 1.0), (0.0, 10.0, 1.0), (0.0, 14.0, 1.0)],
    wavelengths=[(0.6563, 1.0), (0.5876, 1.0), (0.4861, 1.0)],
    reference_wavelength=2,
    aperture=("epd", 50.0),
    vignetting=[(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.2, 0.3), (0.0, 0.0, 0.4, 0.4)],
)

SINGLET = SampleLens(
    "singlet",
    "Singlet sample",
    [
        ("OBJ", "INFINITY", "INFINITY", None, False),
        ("1", "62.5", "4.0", "BK7_SCHOTT", False),
        ("2", "INFINITY", "96.0", None, False),
        ("IMG", "INFINITY", "0.000000", None, False),
    ],
    stop_surface=1,
    surface_apertures={1: 15.0},
    first_order={
        "effective_focal_length": 120.0,
        "back_focal_length": 116.0,
        "front_focal_length": -116.0,
        "f_number": 4.0,
        "image_distance": 96.0,
        "overall_length": 100.0,
        "paraxial_image_height": 12.0,
        "entrance_pupil_diameter": 30.0,
        "entrance_pupil_distance": 20.0,
        "exit_pupil_diameter": 31.0,
        "exit_pupil_distance": -80.0,
    },
    fields=[(0.0, 0.0, 1.0)],
    wavelengths=[(0.5876, 1.0)],
    aperture=("epd", 30.0),
)

ZOOM_TRIPLET = SampleLens(
    "zoomtriplet",
    "Simulated two position zoom triplet",
    [
        ("OBJ", "INFINITY", "INFINITY", None, False),
        ("1", "34.0", "6.0", "SK16_SCHOTT", False),
        ("2", "-120.0", "20.0", None, False),
        ("3", "-45.0", "3.0", "F15_SCHOTT", False),
        ("4", "78.0", "30.0", None, False),
        ("STO", "INFINITY", "4.0", None, True),
        ("6", "26.0", "5.0", "BK7_SCHOTT", False),
        ("7", "-70.0", "70.0", None, False),
        ("IMG", "INFINITY", "0.000000", None, False),
    ],
    stop_surface=5,
    zoom_positions=2,
    first_order={
        "effective_focal_length": 75.0,
        "back_focal_length": 68.0,
        "front_focal_length": -70.0,
        "f_number": 3.5,
        "image_distance": 70.0,
        "overall_length": 138.0,
        "paraxial_image_height": 18.0,
        "entrance_pupil_diameter": 21.4,
        "entrance_pupil_distance": 40.0,
        "exit_pupil_diameter": 22.0,
        "exit_pupil_distance": -60.0,
    },
    fields=[(0.0, 0.0, 1.0), (0.0, 12.0, 1.0)],
    wavelengths=[(0.6563, 1.0), (0.5876, 1.0), (0.4861, 1.0)],
    reference_wavelength=2,
    aperture=("epd", 21.4),
)

SAMPLES = [DBGAUSS, SINGLET, ZOOM_TRIPLET]

RESULT_DIR_NAME = "results"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sample_for(path: str) -> SampleLens:
    stem = Path(path).stem.lower()
    for sample in SAMPLES:
        if sample.name in stem:
            return sample
    return SINGLET


class SimulatedBackend(Backend):
    """Deterministic stand-in for CODE V."""

    name = "simulated"
    source = Source.SIMULATED

    def __init__(
        self,
        *,
        working_directory: str | Path = ".codev-run",
        require_existing_files: bool = True,
        **_: object,
    ) -> None:
        self.working_directory = Path(working_directory)
        self.working_directory.mkdir(parents=True, exist_ok=True)
        self.require_existing_files = require_existing_files
        self.session_open = True
        self.session_valid = True
        #: Lens state, revision and opening identity of the simulated lens.
        #: The names match the COM backend so a client sees the same shape, but
        #: a simulated checkpoint is an in memory snapshot, never a CODE V file.
        self.lens_state = "empty"
        self.lens_id: str | None = None
        self.committed_revision: int | None = None
        self.recovery_count = 0
        self.last_recovery: dict[str, object] | None = None
        self.last_checkpoint: dict[str, object] | None = None
        self._solves: dict[int, str] = {}
        self._pickups: dict[int, str] = {}
        #: Solves of the lens create_lens made (the sample tables know nothing of it).
        self._created_solves: dict[int, str] = {}
        self._modelled_lens_id: str | None = None
        self.lens: LensData | None = None
        self.lens_surface_state: list[dict[str, object]] = []
        self.task: TaskInfo | None = None
        self.result_payload: dict[str, object] = {}
        self.restore_points: dict[str, list[dict[str, object]]] = {}
        self._restore_counter = 0
        self._task_counter = 0

    # ------------------------------------------------------------------ status

    def capabilities(self) -> list[CapabilityInfo]:
        return [
            CapabilityInfo(name="read_lens", supported=True),
            CapabilityInfo(name="update_lens", supported=True, note="simulated data only"),
            CapabilityInfo(name="create_lens", supported=True, note="simulated construction only"),
            CapabilityInfo(name="edit_lens_structure", supported=True,
                           note="simulated simple-lens editing only"),
            CapabilityInfo(name="save_lens_as", supported=True, note="writes a text lens file"),
            CapabilityInfo(name="first_order", supported=True),
            CapabilityInfo(name="spot_diagram", supported=True),
            CapabilityInfo(name="mtf", supported=True, note="diffraction sine MTF, focal systems only"),
            CapabilityInfo(name="wavefront", supported=True, note="simulated nominal-focus WAV shape only"),
            CapabilityInfo(
                name="native_plot_export",
                supported=True,
                note=(
                    "simulated picture only; no CODE V option ran and no neutral "
                    "plot file exists"
                ),
            ),
            CapabilityInfo(
                name="afocal_mtf",
                supported=False,
                note="not supported in the first release",
            ),
            CapabilityInfo(
                name="async_cancel",
                supported=True,
                note="simulated task advances one state per get_analysis call",
            ),
            CapabilityInfo(
                name="aperture_edit",
                supported=True,
                note=(
                    "simulated single-zoom existing system pupil value and centered circular "
                    "clear-aperture radius only"
                ),
            ),
            CapabilityInfo(
                name="field_set_edit",
                supported=True,
                note="simulated field set replacement and a PIM solve marker; no paraxial image is calculated",
            ),
        ]

    def get_status(self) -> StatusInfo:
        return StatusInfo(
            backend=self.name,
            source=self.source,
            service_version=__version__,
            codev_version="simulated-10.2",
            ready=self.session_open,
            session_open=self.session_open,
            lens_open=self.lens is not None,
            current_lens=self.lens.title if self.lens else None,
            working_directory=str(self.working_directory),
            task=self.task,
            capabilities=self.capabilities(),
            warnings=[
                "Simulated backend: no CODE V calculation was performed.",
                "Results must not be reported as machine verified evidence.",
                "Simulated checkpoints are in memory snapshots of the sample data, not "
                "files, so they do not exercise CODE V recovery.",
            ],
            details={
                "session_valid": self.session_valid,
                "lens_state": self.lens_state,
                "lens_id": self.lens_id,
                "committed_revision": self.committed_revision,
                "checkpoint_path": None,
                "recovery_count": self.recovery_count,
                "last_recovery": self.last_recovery,
                "last_checkpoint": self.last_checkpoint,
                "checkpoint_format_version": None,
                "simulated": True,
            },
        )

    # -------------------------------------------------------------------- lens

    def _require_session(self) -> None:
        if not self.session_open:
            raise NotReadyError(
                "The CODE V session is not open.",
                hint="Call open_lens again, or restart the service.",
            )

    def _require_valid_session(self) -> None:
        self._require_session()
        if not self.session_valid:
            raise SessionInvalidError(
                "The session was marked invalid after a failed recovery.",
                hint="Writes are refused until the session is reopened.",
            )

    def _require_lens(self) -> LensData:
        self._require_session()
        if self.lens is None:
            raise NotReadyError(
                "No lens is open.",
                hint="Call open_lens with a lens file first.",
            )
        return self.lens

    def _require_lens_state(self) -> None:
        """Refuse lens work once the simulated lens is no longer trustworthy."""
        if self.lens_state != "ready":
            raise SessionInvalidError(
                f"The lens state is {self.lens_state}; the simulated session refuses "
                "further lens work until it is reopened.",
                details={
                    "lens_state": self.lens_state,
                    "session_valid": self.session_valid,
                    "lens_id": self.lens_id,
                    "committed_revision": self.committed_revision,
                },
                hint="Reopen the lens, or restart the service.",
            )

    def open_lens(self, path: str) -> LensData:
        self._require_session()
        candidate = Path(path)
        if self.require_existing_files and not candidate.exists():
            raise NotFoundError(f"Lens file not found: {candidate}", details={"path": str(candidate)})
        sample = _sample_for(path)
        self.lens = self._build_lens(sample, source_path=str(candidate))
        self.lens_surface_state = [dict(surface.model_dump()) for surface in self.lens.surfaces]
        self.task = None
        self.result_payload = {}
        self.lens_id = uuid.uuid4().hex
        self._modelled_lens_id = None
        self.committed_revision = 0
        self.lens_state = "ready"
        self.session_valid = True
        self.last_checkpoint = {
            "revision": 0,
            "path": None,
            "source_path": str(candidate),
            "created_at": _utc_now(),
            "simulated": True,
        }
        return self.lens

    def create_lens(self, request: CreateLensRequest) -> LensData:
        create_commands(request)  # keep validation identical to the COM path
        self._require_valid_session()
        if self.lens is not None or self.task is not None:
            raise UnsupportedError("A new lens requires an empty session.")
        surfaces = [SurfaceData(number=0, role=SurfaceRole.OBJECT,
                                radius_is_infinite=True, thickness_is_infinite=True)]
        for number, spec in enumerate(request.surfaces, 1):
            surfaces.append(SurfaceData(
                number=number, role=SurfaceRole.SURFACE,
                is_stop=number == request.stop_surface,
                radius=None if spec.radius in (None, 0) else spec.radius,
                radius_is_infinite=spec.radius in (None, 0),
                thickness=spec.thickness, glass=spec.glass,
                label="STO" if number == request.stop_surface else None,
            ))
        surfaces.append(SurfaceData(number=len(surfaces), role=SurfaceRole.IMAGE,
                                    radius_is_infinite=True))
        self.lens = LensData(
            source=self.source, units=request.units,
            dimension_code={Units.INCH: 0, Units.CM: 1, Units.MM: 2}[request.units],
            surfaces=surfaces, stop_surface=request.stop_surface,
            fields=[LensField(number=i, x_angle=item.x_angle, y_angle=item.y_angle,
                              vux=0.0, vlx=0.0, vuy=0.0, vly=0.0)
                    for i, item in enumerate(request.fields, 1)],
            wavelengths=[LensWavelength(number=i, micrometers=value / 1000,
                                        is_reference=i == 1)
                         for i, value in enumerate(request.wavelengths_nm, 1)],
            aperture=ApertureInfo(kind=request.aperture_kind, value=request.aperture_value,
                                  units=request.units if request.aperture_kind == "epd" else None,
                                  definition_source="listing"),
            aperture_usage="default_only",
            warnings=["Simulated construction; no CODE V command ran."],
        )
        self.lens_surface_state = [dict(surface.model_dump()) for surface in surfaces]
        self._created_solves = {len(request.surfaces): "PIM"} if request.image_solve == "pim" else {}
        if self._created_solves:
            self.lens.warnings.append(
                "Simulated PIM solve: the last thickness keeps its starting value; "
                "no paraxial image was calculated."
            )
        self.lens_id = uuid.uuid4().hex
        self._modelled_lens_id = self.lens_id
        self.committed_revision = 0
        self.lens_state = "ready"
        self.last_checkpoint = {"revision": 0, "path": None, "source_path": None,
                                "created_at": _utc_now(), "simulated": True}
        return self.lens.model_copy(deep=True)

    def edit_lens_structure(self, request: StructureRequest) -> StructureResult:
        self._require_valid_session()
        lens = self._require_lens()
        self._require_lens_state()
        if self.lens_id != self._modelled_lens_id:
            raise UnsupportedError("Structural editing requires a service-created simple lens.")
        if self.task is not None and self.task.state in {TaskState.RUNNING, TaskState.PENDING}:
            raise NotReadyError("An analysis is running.")
        if lens.zoom_positions != 1 or self._created_solves or self._solves or self._pickups or any(
            surface.apertures for surface in lens.surfaces
        ):
            raise UnsupportedError("This lens is outside the simple spherical model.")
        planned = plan_structure(request, surface_count=len(lens.surfaces),
                                 stop_surface=lens.stop_surface)
        candidate = lens.model_copy(deep=True)
        for step in planned:
            operation = step.operation
            if operation.kind == "insert_sphere":
                number = operation.before_surface
                candidate.surfaces.insert(number, SurfaceData(
                    number=number, role=SurfaceRole.SURFACE,
                    radius=None if operation.radius in (None, 0) else operation.radius,
                    radius_is_infinite=operation.radius in (None, 0),
                    thickness=operation.thickness, glass=operation.glass,
                ))
            elif operation.kind == "delete_surface":
                del candidate.surfaces[operation.surface]
            candidate.stop_surface = step.result.stop_surface
            for number, surface in enumerate(candidate.surfaces):
                surface.number = number
                surface.is_stop = number == candidate.stop_surface
                surface.label = "STO" if surface.is_stop else None
        self.lens = candidate
        self.lens_surface_state = [dict(surface.model_dump()) for surface in candidate.surfaces]
        self.committed_revision = (self.committed_revision or 0) + 1
        self.last_checkpoint = {"revision": self.committed_revision, "path": None,
                                "source_path": None, "created_at": _utc_now(),
                                "simulated": True}
        return StructureResult(source=self.source, steps=[step.result for step in planned],
                               lens=candidate.model_copy(deep=True), applied=True,
                               restore_point=f"simulated-rp-{self.committed_revision}")

    def get_lens(self, zoom_position: int | None = None) -> LensData:
        lens = self._require_lens()
        if zoom_position is not None:
            if not 1 <= zoom_position <= lens.zoom_positions:
                raise ParameterError(
                    f"zoom_position {zoom_position} is outside 1..{lens.zoom_positions}."
                )
            lens = lens.model_copy(update={"zoom_position": zoom_position}, deep=True)
        return lens.model_copy(deep=True)

    def _build_lens(self, sample: SampleLens, *, source_path: str | None) -> LensData:
        surfaces: list[SurfaceData] = []
        last_index = len(sample.rows) - 1
        for index, (label, radius_text, thickness_text, glass, is_stop) in enumerate(sample.rows):
            if index == 0:
                role = SurfaceRole.OBJECT
            elif index == last_index:
                role = SurfaceRole.IMAGE
            else:
                role = SurfaceRole.SURFACE
            radius, radius_infinite = _infinity_or(radius_text)
            thickness, thickness_infinite = _infinity_or(thickness_text)
            surfaces.append(
                SurfaceData(
                    number=index,
                    role=role,
                    is_stop=is_stop,
                    radius=radius,
                    radius_is_infinite=radius_infinite,
                    thickness=thickness,
                    thickness_is_infinite=thickness_infinite,
                    glass=glass,
                    semi_aperture=27.996150667349955 if index == 1 else None,
                    apertures=(
                        [
                            SurfaceAperture(
                                kind="clear",
                                shape="circular",
                                radius=sample.surface_apertures[index],
                                x_semi_aperture=sample.surface_apertures[index],
                                y_semi_aperture=sample.surface_apertures[index],
                            )
                        ]
                        if index in sample.surface_apertures
                        else []
                    ),
                    label=label,
                )
            )
        fields = [
            LensField(number=index + 1, x_angle=x, y_angle=y, weight=weight,
                      **dict(zip(VIGNETTING_FACTORS, sample.vignetting[index])))
            for index, (x, y, weight) in enumerate(sample.fields)
        ]
        wavelengths = [
            LensWavelength(
                number=index + 1,
                micrometers=value,
                weight=weight,
                is_reference=(index + 1 == sample.reference_wavelength),
            )
            for index, (value, weight) in enumerate(sample.wavelengths)
        ]
        return LensData(
            source=self.source,
            path=source_path,
            title=sample.title,
            units=Units.MM,
            dimension_code=2,
            surfaces=surfaces,
            stop_surface=sample.stop_surface,
            fields=fields,
            wavelengths=wavelengths,
            aperture=ApertureInfo(
                kind=sample.aperture[0],
                value=sample.aperture[1],
                units=Units.MM if sample.aperture[0] == "epd" else None,
                definition_source="listing",
                derived_epd=sample.aperture[1] if sample.aperture[0] == "epd" else None,
                derived_epd_units=Units.MM,
            ),
            aperture_usage=("user_and_default" if sample.surface_apertures else "default_only"),
            zoom_positions=sample.zoom_positions,
            zoom_position=1,
            raw_listing=None,
            warnings=["Synthetic lens data; no physical or vendor-lens equivalence is claimed."],
        )

    # ------------------------------------------------------------------ update

    def _surface_index(self, lens: LensData, number: int) -> int:
        for index, surface in enumerate(lens.surfaces):
            if surface.number == number:
                return index
        numbers = [surface.number for surface in lens.surfaces]
        raise ParameterError(
            f"Surface {number} does not exist in this lens; valid surfaces are "
            f"{min(numbers)}..{max(numbers)}.",
            details={"surface": number, "valid_surfaces": numbers},
        )

    def _validate_edit(self, lens: LensData, edit: ParameterEdit) -> str | None:
        """Return a rejection reason, or None when the edit is acceptable."""
        if edit.target == "aperture":
            index = 0
        else:
            try:
                index = self._selector_index(lens, edit)
            except ParameterError as exc:
                return exc.message

        if lens.zoom_positions > 1 and edit.zoom_position is None:
            return "This lens has multiple zoom positions; specify zoom_position for the edit."
        if edit.zoom_position is not None and not 1 <= edit.zoom_position <= lens.zoom_positions:
            return f"zoom_position {edit.zoom_position} is outside 1..{lens.zoom_positions}."

        if edit.target == "surface":
            surface = lens.surfaces[index]
            if edit.parameter in {"semi_aperture", "clear_aperture"}:
                return (
                    f"{edit.parameter} is an ambiguous legacy name and remains read only; "
                    "use clear_aperture_radius for a verified command-line radius."
                )
            if edit.parameter == "clear_aperture_radius":
                if lens.zoom_positions != 1:
                    return "Surface clear aperture editing currently supports single-zoom lenses only."
                if lens.aperture_usage not in {"user_and_default", "user_only"}:
                    return "Surface clear apertures cannot be edited while CA NO/default-only mode ignores user definitions."
                if surface.role is not SurfaceRole.SURFACE:
                    return "clear_aperture_radius can only be set on an optical surface."
                if not surface.aperture_data_complete or len(surface.apertures) != 1:
                    return "The surface must have exactly one complete explicit aperture definition."
                aperture = surface.apertures[0]
                if (
                    aperture.kind != "clear"
                    or aperture.shape != "circular"
                    or aperture.x_decenter != 0.0
                    or aperture.y_decenter != 0.0
                    or aperture.rotation_degrees != 0.0
                    or aperture.or_with_previous
                ):
                    return "Only an existing centered circular clear aperture can be edited."
            if edit.parameter in {"radius", "glass"} and surface.role is not SurfaceRole.SURFACE:
                return f"{edit.parameter} cannot be set on the {surface.role.value} surface."
            if edit.parameter == "thickness" and surface.role is SurfaceRole.IMAGE:
                return "thickness cannot be set on the image surface."

            solve = self._solves.get(edit.surface)
            if solve:
                return (
                    f"{edit.parameter} on surface {edit.surface} is controlled by the "
                    f"{solve} solve and cannot be edited."
                )
            pickup = self._pickups.get(edit.surface)
            if pickup:
                return (
                    f"{edit.parameter} on surface {edit.surface} is controlled by a pickup "
                    f"and cannot be edited."
                )

        if edit.target == "aperture":
            if lens.zoom_positions != 1:
                return "System aperture editing currently supports single-zoom lenses only."
            if lens.aperture.kind == "unknown" or lens.aperture.definition_source != "listing":
                return "The defining system aperture type is not readable from the native listing."

        if edit.parameter == "glass":
            if not isinstance(edit.value, str) or not GLASS_PATTERN.match(edit.value):
                return "glass name must be 1-32 characters of A-Z, a-z, 0-9, dot, plus, minus or underscore."
            return None

        if edit.parameter == "is_reference":
            try:
                number = int(float(edit.value))
            except (TypeError, ValueError):
                return "is_reference expects a wavelength number."
            if not 1 <= number <= len(lens.wavelengths):
                return f"is_reference must be one of 1..{len(lens.wavelengths)}."
            return None

        try:
            number = float(edit.value)
        except (TypeError, ValueError):
            return f"{edit.parameter} expects a number, received a non numeric string."
        if not math.isfinite(number):
            return f"{edit.parameter} must be a finite number."
        if edit.parameter == "radius" and abs(number) > 1e12:
            return "radius magnitude is outside the accepted range."
        if edit.parameter == "thickness" and abs(number) > 1e9:
            return "thickness magnitude is outside the accepted range."
        if edit.parameter in {"y_angle", "x_angle"} and abs(number) > 180.0:
            return f"{edit.parameter} must be within -180..180 degrees."
        if edit.parameter in VIGNETTING_FACTORS and abs(number) > 0.99:
            return f"{edit.parameter} must be within -0.99..0.99 of the pupil radius."
        if edit.parameter == "weight":
            if number < 0:
                return "weight cannot be negative."
            if edit.target == "wavelength" and number != int(number):
                return (
                    "CODE V requires an integer wavelength weight (WTW expects integer data)."
                )
        if edit.parameter == "micrometers" and not 0.01 <= number <= 1000.0:
            return "The wavelength must be between 0.01 and 1000 micrometers."
        if edit.parameter == "clear_aperture_radius" and not 1e-12 <= number <= 1e9:
            return "clear_aperture_radius must be positive and no greater than 1e9."
        if edit.target == "aperture":
            if not 1e-12 <= number <= 1e9:
                return "The system aperture value must be positive and no greater than 1e9."
            if lens.aperture.kind in {"na", "nao"} and number >= 1:
                return "NA and NAO values must be less than 1 in this release."
        return None

    def _selector_index(self, lens: LensData, edit: ParameterEdit) -> int:
        """Index of the surface, field or wavelength an edit targets."""
        if edit.target == "surface":
            return self._surface_index(lens, edit.surface)
        if edit.target == "field":
            for index, item in enumerate(lens.fields):
                if item.number == edit.field:
                    return index
            numbers = [item.number for item in lens.fields]
            raise ParameterError(
                f"Field {edit.field} does not exist; valid fields are {min(numbers)}..{max(numbers)}.",
                details={"field": edit.field, "valid_fields": numbers},
            )
        if edit.target == "aperture":
            return 0
        for index, item in enumerate(lens.wavelengths):
            if item.number == edit.wavelength:
                return index
        numbers = [item.number for item in lens.wavelengths]
        raise ParameterError(
            f"Wavelength {edit.wavelength} does not exist; valid wavelengths are "
            f"{min(numbers)}..{max(numbers)}.",
            details={"wavelength": edit.wavelength, "valid_wavelengths": numbers},
        )

    def update_lens(self, request: UpdateRequest) -> UpdateResult:
        lens = self._require_lens()
        self._require_valid_session()
        self._require_lens_state()

        session = self._sample(lens)
        self._solves = self._created_solves if self.lens_id == self._modelled_lens_id else session.solves
        self._pickups = session.pickups
        if request.field_set is not None:
            return self._replace_field_set(lens, request)

        defaults = request.zoom_position
        effective_edits = [
            edit.model_copy(update={"zoom_position": edit.zoom_position or defaults})
            for edit in request.edits
        ]
        reasons = [self._validate_edit(lens, edit) for edit in effective_edits]
        restore_point = self._make_restore_point(lens)

        if any(reasons):
            outcomes = [
                EditOutcome(
                    edit=edit,
                    applied=False,
                    previous_value=self._current_value(lens, edit),
                    new_value=None,
                    rejected_reason=reason
                    or "Batch rejected: another edit in the same batch was refused.",
                )
                for edit, reason in zip(effective_edits, reasons)
            ]
            return UpdateResult(
                source=self.source,
                outcomes=outcomes,
                restore_point=restore_point,
                rolled_back=True,
                session_valid=self.session_valid,
                raw_output=None,
                warnings=[
                    "No parameter was changed: the batch is rejected as a whole so that a "
                    "partially applied lens is never reported as success."
                ],
            )

        applied: list[EditOutcome] = []
        planned: list[tuple[int, ParameterEdit, float | str]] = []
        self.lens_state = "updating"
        for edit in effective_edits:
            index = self._selector_index(lens, edit)
            value = normalise_value(edit)
            previous = self._value_at(lens, edit, index)
            self._apply_at(lens, edit, index, value)
            planned.append((index, edit, value))
            applied.append(
                EditOutcome(
                    edit=edit,
                    applied=True,
                    previous_value=previous,
                    new_value=value,
                )
            )

        verified, mismatch = self._verify(lens, planned)
        if not verified:
            rolled_back, restore_ok = self._restore(restore_point)
            if restore_ok:
                warnings = [
                    f"Read back did not match the requested value for {mismatch}; "
                    "the batch was rolled back to the restore point."
                ]
            else:
                self.session_valid = False
                self.lens_state = "invalid"
                self.last_recovery = {
                    "at": _utc_now(),
                    "reason": "the simulated restore point could not be confirmed",
                    "revision": self.committed_revision,
                    "result": "failed",
                    "detail": str(mismatch),
                    "operation": "rollback",
                    "simulated": True,
                }
                warnings = [
                    f"Read back did not match the requested value for {mismatch}, and the "
                    "restore point could not be confirmed: the session is marked invalid."
                ]
            return UpdateResult(
                source=self.source,
                outcomes=applied,
                restore_point=restore_point,
                rolled_back=rolled_back,
                session_valid=self.session_valid,
                warnings=warnings,
            )

        self.lens_surface_state = [dict(surface.model_dump()) for surface in lens.surfaces]
        # A successful batch is only published once the read back confirmed it,
        # so the revision counter never moves for a batch that was rolled back.
        self.committed_revision = (self.committed_revision or 0) + 1
        self.lens_state = "ready"
        self.last_checkpoint = {
            "revision": self.committed_revision,
            "path": None,
            "source_path": self.lens.path if self.lens else None,
            "created_at": _utc_now(),
            "simulated": True,
        }
        return UpdateResult(
            source=self.source,
            outcomes=applied,
            restore_point=restore_point,
            rolled_back=False,
            session_valid=self.session_valid,
            raw_output=None,
            warnings=[
                "The simulated lens accepted the batch: the values were applied and "
                "read back from the in memory sample, not from CODE V."
            ],
        )

    def _replace_field_set(self, lens: LensData, request: UpdateRequest) -> UpdateResult:
        previous = [item.model_copy(deep=True) for item in lens.fields]
        reason = None
        target: list[LensField] = []
        if lens.zoom_positions != 1:
            reason = "Replacing the field set currently supports single-zoom lenses only."
        else:
            try:
                target = resolve_field_set(request.field_set, lens.fields)
            except ParameterError as exc:
                reason = exc.message
        if reason is not None:
            return UpdateResult(
                source=self.source, outcomes=[], rolled_back=True,
                session_valid=self.session_valid,
                field_set=FieldSetOutcome(applied=False, previous_fields=previous,
                                          rejected_reason=reason),
                warnings=["No field was changed: the replacement is refused as a whole."],
            )
        restore_point = self._make_restore_point(lens)
        lens.fields = target
        self.committed_revision = (self.committed_revision or 0) + 1
        self.last_checkpoint = {"revision": self.committed_revision, "path": None,
                                "source_path": self.lens.path if self.lens else None,
                                "created_at": _utc_now(), "simulated": True}
        return UpdateResult(
            source=self.source, outcomes=[], restore_point=restore_point,
            rolled_back=False, session_valid=self.session_valid,
            field_set=FieldSetOutcome(applied=True, previous_fields=previous,
                                      fields=[item.model_copy(deep=True) for item in target]),
            warnings=["The simulated lens accepted the field set: it was applied to the "
                      "in memory sample, not to CODE V."],
        )

    def _make_restore_point(self, lens: LensData) -> str:
        self._restore_counter += 1
        key = f"rp-{self._restore_counter}"
        self.restore_points[key] = lens.model_dump()
        return key

    def _restore(self, key: str) -> tuple[bool, bool]:
        """Return (rolled_back, restore_confirmed)."""
        snapshot = self.restore_points.get(key)
        if snapshot is None or self.lens is None:
            return False, False
        self.lens = LensData.model_validate(snapshot)
        return True, self.lens.model_dump() == snapshot

    def _verify(
        self, lens: LensData, planned: list[tuple[int, ParameterEdit, float | str]]
    ) -> tuple[bool, str]:
        for index, edit, value in planned:
            if edit.parameter == "is_reference":
                if not lens.wavelengths[index].is_reference:
                    return False, f"wavelength {edit.wavelength} is_reference"
                continue
            current = self._value_at(lens, edit, index)
            if isinstance(value, str):
                if current != value:
                    return False, self._describe_edit(edit)
            else:
                if current is None or abs(float(current) - value) > 1e-9:
                    return False, self._describe_edit(edit)
        return True, ""

    @staticmethod
    def _describe_edit(edit: ParameterEdit) -> str:
        if edit.target == "surface":
            return f"surface {edit.surface} {edit.parameter}"
        if edit.target == "field":
            return f"field {edit.field} {edit.parameter}"
        return f"wavelength {edit.wavelength} {edit.parameter}"

    @staticmethod
    def _value_at(lens: LensData, edit: ParameterEdit, index: int) -> float | str | None:
        if edit.target == "aperture":
            return lens.aperture.value
        if edit.target == "surface":
            surface = lens.surfaces[index]
            if edit.parameter == "radius":
                return surface.radius
            if edit.parameter == "thickness":
                return surface.thickness
            if edit.parameter == "glass":
                return surface.glass
            if edit.parameter == "clear_aperture_radius":
                return surface.apertures[0].radius if len(surface.apertures) == 1 else None
            return surface.semi_aperture
        if edit.target == "field":
            item = lens.fields[index]
            return {
                "y_angle": item.y_angle,
                "x_angle": item.x_angle,
                "weight": item.weight,
                **{name: getattr(item, name) for name in VIGNETTING_FACTORS},
            }.get(edit.parameter)
        wavelength = lens.wavelengths[index]
        if edit.parameter == "micrometers":
            return wavelength.micrometers
        if edit.parameter == "weight":
            return wavelength.weight
        return None

    @staticmethod
    def _apply_at(
        lens: LensData, edit: ParameterEdit, index: int, value: float | str
    ) -> None:
        if edit.target == "aperture":
            lens.aperture.value = float(value)
            if lens.aperture.kind == "epd":
                lens.aperture.derived_epd = float(value)
            return
        if edit.target == "surface":
            surface = lens.surfaces[index]
            if edit.parameter == "radius":
                surface.radius = float(value)
                surface.radius_is_infinite = False
            elif edit.parameter == "thickness":
                surface.thickness = float(value)
                surface.thickness_is_infinite = False
            elif edit.parameter == "glass":
                surface.glass = str(value)
            elif edit.parameter == "clear_aperture_radius":
                aperture = surface.apertures[0]
                aperture.radius = float(value)
                aperture.x_semi_aperture = float(value)
                aperture.y_semi_aperture = float(value)
                surface.semi_aperture = float(value)
            else:
                surface.semi_aperture = float(value)
            return
        if edit.target == "field":
            field = lens.fields[index]
            if edit.parameter == "y_angle":
                field.y_angle = float(value)
            elif edit.parameter == "x_angle":
                field.x_angle = float(value)
            elif edit.parameter in VIGNETTING_FACTORS:
                setattr(field, edit.parameter, float(value))
            else:
                field.weight = float(value)
            return
        wavelength = lens.wavelengths[index]
        if edit.parameter == "micrometers":
            wavelength.micrometers = float(value)
        elif edit.parameter == "weight":
            wavelength.weight = float(value)
        else:
            for item in lens.wavelengths:
                item.is_reference = item.number == int(value)

    def _current_value(self, lens: LensData, edit: ParameterEdit) -> float | str | None:
        try:
            return self._value_at(lens, edit, self._selector_index(lens, edit))
        except ParameterError:
            return None

    # ---------------------------------------------------------------- analysis

    def run_analysis(self, request: AnalysisRequest) -> TaskInfo:
        lens = self._require_lens()
        if self.task is not None and self.task.state in {TaskState.QUEUED, TaskState.RUNNING}:
            raise ParameterError(
                "Another analysis task is still running.",
                details={"task_id": self.task.task_id, "state": self.task.state.value},
                hint="Call cancel_analysis or wait for get_analysis to report a final state.",
            )
        if request.kind.value == "mtf" and not request.options.frequencies:
            raise ParameterError(
                "MTF analysis needs an explicit frequency grid.",
                hint="Pass options.frequencies, for example [10, 20, 40, 80] cycles/mm.",
            )
        if request.options.zoom_position is not None and not (
            1 <= request.options.zoom_position <= lens.zoom_positions
        ):
            raise ParameterError(
                f"zoom_position {request.options.zoom_position} is outside 1..{lens.zoom_positions}."
            )
        check_native_plot_options(lens, request.options, request.kind)
        check_wavefront_options(lens, request.options, request.kind)
        self._validate_sample_selection(lens, request)

        self._task_counter += 1
        settings = AnalysisSettings(
            zoom_position=request.options.zoom_position or lens.zoom_position,
            field_numbers=request.options.field_numbers
            or [field.number for field in lens.fields],
            wavelength_numbers=request.options.wavelength_numbers
            or [wavelength.number for wavelength in lens.wavelengths],
            ray_grid=request.options.ray_grid,
            frequencies=request.options.frequencies,
            frequency_unit="cycles/mm" if request.kind.value == "mtf" else None,
            azimuth=request.options.azimuth,
            mtf_type=request.options.mtf_type if request.kind.value == "mtf" else None,
            plot_type=(
                request.options.plot_type if request.kind.value == "native_plot" else None
            ),
            wavefront_nrd=20 if request.kind.value == "wavefront" else None,
            notes=["Simulated settings; no CODE V option was executed."],
        )
        self.task = TaskInfo(
            task_id=f"sim-{self._task_counter:04d}",
            kind=request.kind,
            state=TaskState.QUEUED,
            source=self.source,
            created_at=_utc_now(),
            settings=settings,
            warnings=["Simulated task."],
        )
        self.result_payload = {"request": request}
        return self.task.model_copy(deep=True)

    def _validate_sample_selection(self, lens: LensData, request: AnalysisRequest) -> None:
        valid_fields = {field.number for field in lens.fields}
        for number in request.options.field_numbers or []:
            if number not in valid_fields:
                raise ParameterError(
                    f"Field {number} does not exist.", details={"valid_fields": sorted(valid_fields)}
                )
        valid_wavelengths = {wavelength.number for wavelength in lens.wavelengths}
        for number in request.options.wavelength_numbers or []:
            if number not in valid_wavelengths:
                raise ParameterError(
                    f"Wavelength {number} does not exist.",
                    details={"valid_wavelengths": sorted(valid_wavelengths)},
                )

    def get_analysis(self) -> AnalysisSnapshot:
        self._require_session()
        if self.task is None:
            return AnalysisSnapshot(source=self.source)
        if self.task.state is TaskState.QUEUED:
            self.task.state = TaskState.RUNNING
            self.task.started_at = _utc_now()
            self.task.progress = "Simulated analysis started."
            return AnalysisSnapshot(source=self.source, task=self.task.model_copy(deep=True))
        if self.task.state is TaskState.RUNNING:
            self._finish_task()
        snapshot = AnalysisSnapshot(source=self.source, task=self.task.model_copy(deep=True))
        if self.task.state is TaskState.SUCCEEDED:
            snapshot.first_order = self.result_payload.get("first_order")
            snapshot.spot_diagram = self.result_payload.get("spot_diagram")
            snapshot.mtf = self.result_payload.get("mtf")
            snapshot.native_plot = self.result_payload.get("native_plot")
            snapshot.wavefront = self.result_payload.get("wavefront")
        return snapshot

    def cancel_analysis(self) -> TaskInfo | None:
        self._require_session()
        if self.task is None:
            return None
        if self.task.state in {TaskState.QUEUED, TaskState.RUNNING}:
            self.task.state = TaskState.CANCELLED
            self.task.finished_at = _utc_now()
            self.task.progress = "Simulated cancellation request accepted."
        return self.task.model_copy(deep=True)

    def _finish_task(self) -> None:
        assert self.task is not None
        lens = self._require_lens()
        request: AnalysisRequest = self.result_payload["request"]
        try:
            if request.kind.value == "first_order":
                self.result_payload["first_order"] = self._first_order(lens, request)
            elif request.kind.value == "spot_diagram":
                self.result_payload["spot_diagram"] = self._spot_diagram(lens, request)
            elif request.kind.value == "mtf":
                self.result_payload["mtf"] = self._mtf(lens, request)
            elif request.kind.value == "native_plot":
                self.result_payload["native_plot"] = self._native_plot(lens, request)
            elif request.kind.value == "wavefront":
                self.result_payload["wavefront"] = self._wavefront(lens)
            else:
                raise UnsupportedError(f"Analysis kind {request.kind} is not supported.")
        except (NotReadyError, ParameterError, UnsupportedError):
            raise
        except Exception as exc:
            self.task.state = TaskState.FAILED
            self.task.finished_at = _utc_now()
            self.task.error = ComputationError(f"Simulated analysis failed: {exc}").to_info()
            return
        self.task.state = TaskState.SUCCEEDED
        self.task.finished_at = _utc_now()
        self.task.progress = "Simulated analysis finished."
        self.task.raw_output = "simulated backend: no CODE V output"

    def _sample(self, lens: LensData) -> SampleLens:
        return _sample_for(lens.path or "singlet")

    def _wavefront(self, lens: LensData) -> WavefrontResult:
        reference = next((w for w in lens.wavelengths if w.is_reference), None)
        if reference is None or reference.micrometers is None:
            raise ComputationError("Simulated lens has no reference wavelength.")
        fields = [WavefrontField(field_number=field.number,
                                 rms_waves=0.08 + index * 0.03,
                                 strehl=max(0.0, 0.8 - index * 0.12),
                                 rays_traced=400 - index * 40)
                  for index, field in enumerate(lens.fields)]
        return WavefrontResult(
            source=self.source, zoom_position=1,
            wavelength_numbers=[w.number for w in lens.wavelengths],
            reference_wavelength_number=reference.number,
            reference_wavelength_nm=reference.micrometers * 1000,
            fields=fields, weighted_rms_waves=sum(f.rms_waves for f in fields) / len(fields),
            weighted_strehl=sum(f.strehl for f in fields) / len(fields),
            raw_output="simulated WAV values; no CODE V command was executed",
            warnings=["Simulated values have no optical meaning."],
        )

    def _first_order(self, lens: LensData, request: AnalysisRequest) -> FirstOrderResult:
        sample = self._sample(lens)
        values = sample.first_order
        return FirstOrderResult(
            source=self.source,
            units=lens.units,
            zoom_position=request.options.zoom_position or lens.zoom_position,
            effective_focal_length=values.get("effective_focal_length"),
            back_focal_length=values.get("back_focal_length"),
            front_focal_length=values.get("front_focal_length"),
            f_number=values.get("f_number"),
            image_distance=values.get("image_distance"),
            overall_length=values.get("overall_length"),
            paraxial_image_height=values.get("paraxial_image_height"),
            entrance_pupil_diameter=values.get("entrance_pupil_diameter"),
            entrance_pupil_distance=values.get("entrance_pupil_distance"),
            exit_pupil_diameter=values.get("exit_pupil_diameter"),
            exit_pupil_distance=values.get("exit_pupil_distance"),
            raw_output="simulated first order listing",
            precision_note=(
                "Simulated values. The real backend reports the native listing plus the "
                "EvaluateExpression string, and keeps them distinguishable because the "
                "string is only as precise as the printed format."
            ),
        )

    def _spot_diagram(self, lens: LensData, request: AnalysisRequest) -> SpotDiagramResult:
        grid = request.options.ray_grid or 7
        statistics_grid = 15
        field_number = (request.options.field_numbers or [lens.fields[0].number])[0]
        zoom = request.options.zoom_position or lens.zoom_position
        field = lens.fields[field_number - 1]
        angle = abs(field.y_angle or 0.0) or abs(field.x_angle or 0.0)
        sigma = 0.0025 + 0.00035 * angle

        rng = random.Random(f"{lens.title}|{field_number}|{zoom}|{statistics_grid}")
        statistics_points = []
        for row in range(statistics_grid):
            for column in range(statistics_grid):
                u = (column + 0.5) / statistics_grid * 2 - 1
                v = (row + 0.5) / statistics_grid * 2 - 1
                if u * u + v * v > 1.0:
                    continue
                statistics_points.append(
                    (rng.gauss(0.0, sigma), rng.gauss(0.0, sigma) + 0.00008 * angle)
                )
        radii = [math.hypot(x, y) for x, y in statistics_points]
        rms = math.sqrt(sum(r * r for r in radii) / len(radii)) if radii else 0.0
        maximum = max(radii) if radii else 0.0

        plot_points = statistics_points[: grid * grid]
        image = self._render_scatter(
            plot_points,
            reference_radius=maximum,
            title=f"spot {lens.title[:24]} f{field_number} z{zoom}",
            task_id=self.task.task_id if self.task else "sim",
        )
        return SpotDiagramResult(
            source=self.source,
            units=lens.units,
            zoom_position=zoom,
            field_number=field_number,
            wavelength_numbers=request.options.wavelength_numbers
            or [w.number for w in lens.wavelengths],
            centroid_x=0.0,
            centroid_y=0.0,
            rms_radius=rms,
            max_radius=maximum,
            size_is_radius=True,
            plot_sample_count=len(plot_points),
            statistics_sample_count=len(statistics_points),
            image=image,
            raw_output="simulated spot diagram statistics",
            warnings=[
                "Statistics use a "
                f"{statistics_grid}x{statistics_grid} ray grid; only the first "
                f"{len(plot_points)} rays are drawn, so the picture must not be used to "
                "estimate the spot size."
            ],
        )

    def _mtf(self, lens: LensData, request: AnalysisRequest) -> MtfResult:
        frequencies = sorted(float(value) for value in (request.options.frequencies or []))
        if not frequencies:
            raise ParameterError("MTF analysis needs an explicit frequency grid.")
        if request.options.mtf_type is MtfType.GEOMETRIC:
            raise UnsupportedError(
                "The first release supports the diffraction sine wave MTF only.",
                details={"requested": request.options.mtf_type.value},
            )
        sample = self._sample(lens)
        f_number = sample.first_order.get("f_number") or 4.0
        reference = next(
            (w for w in lens.wavelengths if w.is_reference), lens.wavelengths[0]
        )
        wavelength_mm = (reference.micrometers or 0.5876) / 1000.0
        cutoff = 1.0 / (wavelength_mm * f_number)

        field_numbers = request.options.field_numbers or [f.number for f in lens.fields]
        curves: list[MtfCurve] = []
        series: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]] = []
        colours = [plotting.BLUE, plotting.RED, plotting.GREEN, plotting.ORANGE]
        for position, field_number in enumerate(field_numbers):
            penalty = 1.0 - 0.05 * position
            tangential = []
            sagittal = []
            analytic_limit: list[float] = []
            for frequency in frequencies:
                ratio = min(frequency / cutoff, 1.0)
                base = (2.0 / math.pi) * (math.acos(ratio) - ratio * math.sqrt(max(1.0 - ratio * ratio, 0.0)))
                tangential.append(base * penalty)
                sagittal.append(base * (penalty + 0.02 * position))
                analytic_limit.append(base)
            curves.append(
                MtfCurve(
                    field_number=field_number,
                    wavelength_numbers=request.options.wavelength_numbers
                    or [w.number for w in lens.wavelengths],
                    tangential=tangential,
                    sagittal=sagittal,
                    analytic_limit=analytic_limit,
                )
            )
            colour = colours[position % len(colours)]
            series.append((f"T{field_number}", list(zip(frequencies, tangential)), colour))
            series.append((f"S{field_number}", list(zip(frequencies, sagittal)), colour))

        image = self._render_lines(
            series,
            title=f"MTF {lens.title[:24]}",
            task_id=self.task.task_id if self.task else "sim",
        )
        return MtfResult(
            source=self.source,
            zoom_position=request.options.zoom_position or lens.zoom_position,
            frequencies=frequencies,
            frequency_unit="cycles/mm",
            azimuth=request.options.azimuth or 0.0,
            mtf_type=request.options.mtf_type,
            curves=curves,
            image=image,
            raw_output="simulated MTF curves",
            warnings=[
                f"Diffraction limited model with cutoff {cutoff:.1f} cycles/mm at the "
                "reference wavelength; a real optical system and real aberrations are not modelled."
            ],
        )

    def _native_plot(self, lens: LensData, request: AnalysisRequest) -> NativePlotResult:
        """A deterministic stand-in for the picture CODE V would have drawn.

        The real backend hands the drawing to CODE V and converts the neutral
        plot file that option writes. Nothing like that happens here: this draws
        a small labelled chart so the transport, the metadata and the MCP image
        content can be exercised without CODE V, and the result says so.
        """
        plot_type = request.options.plot_type
        if plot_type is None:  # pragma: no cover - checked by run_analysis
            raise ParameterError("A native plot needs a plot type.")
        zoom = request.options.zoom_position or lens.zoom_position
        frequencies = [10.0, 20.0, 40.0, 80.0]
        series = [
            (
                "sim",
                [(value, 1.0 - value / 200.0) for value in frequencies],
                plotting.BLUE,
            )
        ]
        canvas = plotting.line_plot(
            series, title=f"simulated {plot_type.value}", y_label="sim"
        )
        image = self._payload(self._native_plot_path(plot_type), canvas)
        return NativePlotResult(
            source=self.source,
            plot_type=plot_type,
            zoom_position=zoom,
            image=image,
            plot_file_path=None,
            plot_file_bytes=None,
            raw_output=(
                "simulated backend: no CODE V option ran and no neutral plot file "
                "was written"
            ),
            warnings=[
                "This picture was drawn by the simulated backend. No CODE V option "
                "ran, no neutral plot file exists and the image is not native CODE V "
                "output.",
            ],
        )

    def _native_plot_path(self, plot_type: NativePlotType) -> Path:
        """A result name that never overwrites an earlier picture.

        The sample data is deterministic, so the name is too; only a rerun that
        would land on an existing file is given a numbered sibling.
        """
        directory = self.working_directory / RESULT_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        task_id = self.task.task_id if self.task else "sim"
        base = f"{task_id}-native-{plot_type.value}"
        candidate = directory / f"{base}.png"
        index = 2
        while candidate.exists():
            candidate = directory / f"{base}-{index}.png"
            index += 1
        return candidate

    # ------------------------------------------------------------------ output

    def _result_path(self, task_id: str, suffix: str) -> Path:
        directory = self.working_directory / RESULT_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{task_id}-{suffix}.png"

    def _payload(self, path: Path, canvas: plotting.Canvas) -> ImagePayload:
        data = canvas.to_png()
        path.write_bytes(data)
        return ImagePayload(
            path=str(path),
            media_type="image/png",
            base64_data=base64.b64encode(data).decode("ascii"),
            width=canvas.width,
            height=canvas.height,
        )

    def _render_scatter(
        self, points: list[tuple[float, float]], *, reference_radius: float, title: str, task_id: str
    ) -> ImagePayload:
        canvas = plotting.scatter_plot(points, reference_radius=reference_radius, title=title)
        return self._payload(self._result_path(task_id, "spot"), canvas)

    def _render_lines(
        self,
        series: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]],
        *,
        title: str,
        task_id: str,
    ) -> ImagePayload:
        canvas = plotting.line_plot(series, title=title, y_label="MTF")
        return self._payload(self._result_path(task_id, "mtf"), canvas)

    # ------------------------------------------------------------------- files

    def save_lens_as(self, path: str) -> SaveResult:
        lens = self._require_lens()
        self._require_valid_session()
        target = Path(path)
        if not target.is_absolute():
            target = self.working_directory / target
        if lens.path and target.resolve() == Path(lens.path).resolve():
            raise ParameterError(
                "Refusing to overwrite the source lens file.",
                details={"path": str(target)},
            )
        if target.exists():
            raise ParameterError(
                "Target file already exists.",
                details={"path": str(target)},
                hint="Choose a new file name; this service never overwrites.",
            )
        if not target.parent.exists():
            raise NotFoundError(
                "Target directory does not exist.", details={"directory": str(target.parent)}
            )
        text = self._serialise_lens(lens)
        target.write_text(text, encoding="utf-8")
        return SaveResult(
            source=self.source,
            path=str(target),
            bytes_written=len(text.encode("utf-8")),
            overwritten=False,
            raw_output="simulated save",
        )

    @staticmethod
    def _serialise_lens(lens: LensData) -> str:
        lines = [
            "! Simulated lens file written by the CODE V MCP service.",
            f"! title: {lens.title}",
            f"! units: {lens.units.value}",
        ]
        for surface in lens.surfaces:
            radius = "INF" if surface.radius_is_infinite or surface.radius is None else f"{surface.radius:.8g}"
            thickness = (
                "INF"
                if surface.thickness_is_infinite or surface.thickness is None
                else f"{surface.thickness:.8g}"
            )
            glass = surface.glass or ""
            lines.append(f"{surface.number:>3} {radius:>16} {thickness:>16} {glass:<14} {surface.label or ''}")
        for wavelength in lens.wavelengths:
            lines.append(f"WL {wavelength.number} {wavelength.micrometers}")
        for field in lens.fields:
            lines.append(f"YAN {field.number} {field.y_angle}")
        return "\n".join(lines) + "\n"

    # ----------------------------------------------------------------- session

    def close_session(self) -> StatusInfo:
        self.session_open = False
        self.lens = None
        self.lens_surface_state = []
        self.task = None
        self.result_payload = {}
        self.restore_points.clear()
        self.session_valid = False
        self.lens_state = "invalid"
        status = self.get_status()
        status.details["cleanup_confirmed"] = True
        status.details["cleanup_remaining"] = []
        return status

    def stop(self) -> None:
        self.session_open = False


def editable_number(edit: ParameterEdit) -> int:
    return edit.surface


def normalise_value(edit: ParameterEdit) -> float | str:
    if edit.parameter == "glass":
        return str(edit.value)
    if edit.parameter == "is_reference":
        return float(int(float(edit.value)))
    return float(edit.value)
