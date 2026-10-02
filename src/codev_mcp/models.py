"""Public data models for the CODE V MCP service.

These models are the frozen contract from PLAN.md section 4. Both the real COM
backend and the simulated backend return them, so a client can never tell where
the numbers came from except through the explicit source field.

Conventions that are encoded here on purpose:

* Surface numbers are CODE V native numbers: the object surface is 0, the image
  surface is the highest number, and the stop is reported separately.
* Every numeric payload carries the units, the zoom position and the raw backend
  output it was derived from, plus a note about the precision of that output.
* None for a radius or thickness means an infinite value; radius_is_infinite is
  kept next to it because infinity is a meaningful optical value, not a missing
  one.
* Results are never presented as complete when the backend output was truncated
  or could not be parsed; the warnings list and the source field say so.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorInfo

EditTarget = Literal["surface", "field", "wavelength", "aperture"]

# Parameter names accepted per target. The legacy aperture names remain in the
# schema only so older clients receive a structured refusal; M2 uses the
# explicit radius name and a selectorless system-aperture target.
SURFACE_PARAMETERS = (
    "radius",
    "thickness",
    "glass",
    "clear_aperture_radius",
    # Accepted so old clients receive a structured refusal instead of a schema
    # failure. These two names never had a verified diameter/radius meaning.
    "semi_aperture",
    "clear_aperture",
)
FIELD_PARAMETERS = ("y_angle", "x_angle", "weight", "vux", "vlx", "vuy", "vly")
WAVELENGTH_PARAMETERS = ("micrometers", "weight", "is_reference")
APERTURE_PARAMETERS = ("value",)
PARAMETERS_BY_TARGET: dict[str, tuple[str, ...]] = {
    "surface": SURFACE_PARAMETERS,
    "field": FIELD_PARAMETERS,
    "wavelength": WAVELENGTH_PARAMETERS,
    "aperture": APERTURE_PARAMETERS,
}
class Source(str, Enum):
    """Where a result actually came from."""

    SIMULATED = "simulated"
    CODEV = "codev"


class Units(str, Enum):
    MM = "mm"
    CM = "cm"
    INCH = "inch"


class SurfaceRole(str, Enum):
    OBJECT = "object"
    SURFACE = "surface"
    IMAGE = "image"


class SurfaceAperture(BaseModel):
    """One explicit CODE V surface aperture definition.

    Dimensions are command-line semi-apertures in lens units. ``radius`` is
    therefore a circular radius, never a diameter. Default apertures computed
    from reference rays are not inserted into this list.
    """

    kind: Literal["clear", "obscuration", "edge", "hole"]
    shape: Literal["circular", "rectangular", "elliptical", "unknown"]
    label: str | None = None
    radius: float | None = None
    x_semi_aperture: float | None = None
    y_semi_aperture: float | None = None
    x_decenter: float = 0.0
    y_decenter: float = 0.0
    rotation_degrees: float = 0.0
    or_with_previous: bool = False
    zoom_position: int = 1


class AnalysisKind(str, Enum):
    FIRST_ORDER = "first_order"
    SPOT_DIAGRAM = "spot_diagram"
    MTF = "mtf"
    NATIVE_PLOT = "native_plot"
    WAVEFRONT = "wavefront"


class NativePlotType(str, Enum):
    """The CODE V native plots the service is allowed to export.

    Each value maps to one fixed option command inside the real backend, so the
    caller picks a plot by name and can never build a CODE V command itself.
    """

    LAYOUT = "layout"
    SPOT = "spot"
    MTF = "mtf"
    RAY_ABERRATION = "ray_aberration"
    FIELD_ABERRATION = "field_aberration"


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MtfType(str, Enum):
    DIFFRACTION = "diffraction"
    GEOMETRIC = "geometric"


class SurfaceData(BaseModel):
    """One surface of the lens, using CODE V native numbering."""

    number: int = Field(description="CODE V surface number; object is 0, image is the last one.")
    role: SurfaceRole
    is_stop: bool = False
    radius: float | None = Field(
        default=None,
        description="Vertex radius in lens units; None together with radius_is_infinite means a plane.",
    )
    radius_is_infinite: bool = False
    thickness: float | None = Field(
        default=None, description="Thickness to the next surface, same units."
    )
    thickness_is_infinite: bool = False
    glass: str | None = Field(default=None, description="Glass name, empty for air.")
    semi_aperture: float | None = Field(
        default=None,
        description=(
            "Effective maximum semi aperture in lens units from GetMaxAperture. "
            "It may be a reference-ray default and is read only."
        ),
    )
    apertures: list[SurfaceAperture] = Field(
        default_factory=list,
        description="Explicit user-defined clear apertures, obscurations, edges and holes.",
    )
    aperture_data_complete: bool = Field(
        default=True,
        description="False when the native aperture listing contained unsupported or incomplete data.",
    )
    label: str | None = Field(default=None, description="Backend label such as OBJ, STO or IMG.")


VIGNETTING_FACTORS = ("vux", "vlx", "vuy", "vly")


class LensField(BaseModel):
    """One field at the zoom position that was read.

    The four vignetting factors are CODE V's VUX/VLX/VUY/VLY: fractions of the
    entrance pupil radius removed at the upper/lower X and Y pupil edges. They
    are edited through update_lens like the other field items; AUT may
    recompute them with SET VIG.
    """

    number: int
    x_angle: float | None = Field(default=None, description="X field angle in degrees.")
    y_angle: float | None = Field(default=None, description="Y field angle in degrees.")
    weight: float | None = None
    vux: float | None = Field(default=None, description="Upper X vignetting factor (VUX).")
    vlx: float | None = Field(default=None, description="Lower X vignetting factor (VLX).")
    vuy: float | None = Field(default=None, description="Upper Y vignetting factor (VUY).")
    vly: float | None = Field(default=None, description="Lower Y vignetting factor (VLY).")


class LensWavelength(BaseModel):
    number: int
    micrometers: float | None = None
    weight: float | None = None
    is_reference: bool = False


class ApertureInfo(BaseModel):
    kind: Literal["epd", "fno", "na", "nao", "unknown"] = "unknown"
    value: float | None = None
    units: Units | None = None
    zoom_position: int = 1
    definition_source: Literal["listing", "unknown"] = "unknown"
    derived_epd: float | None = Field(
        default=None,
        description="Calculated entrance pupil diameter read from CODE V; not the defining value unless kind is epd.",
    )
    derived_epd_units: Units | None = None


class LensData(BaseModel):
    """A full read of the current lens."""

    source: Source
    path: str | None = Field(default=None, description="File the working copy was opened from.")
    title: str | None = None
    units: Units
    dimension_code: int = Field(description="Raw CODE V GetDimension value: 0 inch, 1 cm, 2 mm.")
    surfaces: list[SurfaceData]
    stop_surface: int
    fields: list[LensField] = Field(default_factory=list)
    wavelengths: list[LensWavelength] = Field(default_factory=list)
    aperture: ApertureInfo = Field(default_factory=ApertureInfo)
    aperture_usage: Literal[
        "user_and_default", "default_only", "user_only", "unknown"
    ] = "unknown"
    zoom_positions: int = 1
    zoom_position: int = 1
    raw_listing: str | None = Field(
        default=None, description="Verbatim surface listing the values were parsed from."
    )
    warnings: list[str] = Field(default_factory=list)


class SphericalSurfaceSpec(BaseModel):
    """One ordinary spherical surface for a newly constructed lens."""

    model_config = ConfigDict(extra="forbid")

    radius: float | None = Field(description="Radius in lens units; null means a plane.")
    thickness: float = Field(description="Distance to the next surface in lens units.")
    glass: str | None = Field(default=None, description="Glass following this surface; null means air.")


class FieldAngleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_angle: float
    y_angle: float


class CreateLensRequest(BaseModel):
    """Make a single-zoom, ordinary spherical lens from typed values only."""

    model_config = ConfigDict(extra="forbid")

    units: Units = Units.MM
    aperture_kind: Literal["epd", "fno", "na", "nao"] = "epd"
    aperture_value: float
    wavelengths_nm: list[float] = Field(min_length=1, max_length=10)
    fields: list[FieldAngleSpec] = Field(min_length=1, max_length=10)
    surfaces: list[SphericalSurfaceSpec] = Field(min_length=2, max_length=32)
    stop_surface: int = Field(ge=1)
    image_solve: Literal["pim"] | None = Field(
        default=None,
        description=(
            "pim: give the thickness of the last ordinary surface CODE V's paraxial image "
            "solve (PIM), so the image plane follows the paraxial focus. The thickness given "
            "for that surface is only the starting value."
        ),
    )

    @model_validator(mode="after")
    def _check_structure(self) -> "CreateLensRequest":
        if self.stop_surface > len(self.surfaces):
            raise ValueError("stop_surface must name one of the ordinary surfaces")
        if self.surfaces[-1].glass is not None:
            raise ValueError("the final ordinary surface must be followed by air")
        return self


class StructureOperation(BaseModel):
    """One typed structural operation; all surface numbers are native CODE V numbers."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["insert_sphere", "delete_surface", "set_stop"]
    before_surface: int | None = None
    surface: int | None = None
    radius: float | None = None
    thickness: float | None = None
    glass: str | None = None
    new_stop_surface: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "StructureOperation":
        present = set(self.model_fields_set)
        if self.kind == "insert_sphere":
            required = {"before_surface", "radius", "thickness"}
            permitted = required | {"kind", "glass"}
        elif self.kind == "delete_surface":
            required = {"surface"}
            permitted = required | {"kind", "new_stop_surface"}
        else:
            required = {"surface"}
            permitted = required | {"kind"}
        if missing := required - present:
            raise ValueError(f"{self.kind} needs {sorted(missing)}")
        if unexpected := present - permitted:
            raise ValueError(f"{self.kind} does not accept {sorted(unexpected)}")
        return self


class StructureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[StructureOperation] = Field(min_length=1, max_length=8)


class StructureStep(BaseModel):
    kind: Literal["insert_sphere", "delete_surface", "set_stop"]
    old_to_new: dict[int, int]
    stop_surface: int


class StructureResult(BaseModel):
    source: Source
    steps: list[StructureStep]
    lens: LensData | None = None
    applied: bool = False
    restore_point: str | None = None
    rolled_back: bool = False
    session_valid: bool = True
    warnings: list[str] = Field(default_factory=list)


class ParameterEdit(BaseModel):
    """A single requested change to an existing parameter.

    Exactly one target selector is used, chosen by target:

    * target surface: set surface, parameter from radius, thickness, glass or
      clear_aperture_radius;
    * target field: set field, parameter from y_angle, x_angle, weight or a
      vignetting factor vux, vlx, vuy, vly (-0.99..0.99);
    * target wavelength: set wavelength, parameter from micrometers, weight,
      is_reference.
    * target aperture: no selector, parameter value; edits the existing system
      pupil specification without converting its type.
    """

    model_config = ConfigDict(extra="forbid")

    target: EditTarget = "surface"
    surface: int | None = Field(default=None, description="CODE V surface number to edit.")
    field: int | None = Field(default=None, description="CODE V field number to edit.")
    wavelength: int | None = Field(default=None, description="CODE V wavelength number to edit.")
    parameter: str = Field(
        description=(
            "surface: radius | thickness | glass | clear_aperture_radius "
            "(semi_aperture and clear_aperture are legacy read-only names); "
            "field: y_angle | x_angle | weight | vux | vlx | vuy | vly; "
            "wavelength: micrometers | weight | is_reference; aperture: value"
        )
    )
    value: float | str
    zoom_position: int | None = Field(
        default=None,
        description="Required when the lens has more than one zoom position.",
    )

    @model_validator(mode="after")
    def _check_target(self) -> "ParameterEdit":
        selector = {
            "surface": self.surface,
            "field": self.field,
            "wavelength": self.wavelength,
        }
        chosen = {name: value for name, value in selector.items() if value is not None}
        if self.target == "aperture":
            if chosen:
                raise ValueError("target 'aperture' does not take a surface, field or wavelength selector")
            if self.parameter not in PARAMETERS_BY_TARGET[self.target]:
                raise ValueError("target 'aperture' accepts only parameter 'value'")
            return self
        if not chosen:
            raise ValueError(
                f"edit for target {self.target!r} needs one of surface, field or wavelength"
            )
        if len(chosen) > 1:
            raise ValueError(
                "an edit may set only one of surface, field or wavelength; "
                f"received {sorted(chosen)}"
            )
        name = next(iter(chosen))
        if name != self.target:
            raise ValueError(
                f"target is {self.target!r} but {name} was given; set target to {name!r}"
            )
        allowed = PARAMETERS_BY_TARGET[self.target]
        if self.parameter not in allowed:
            raise ValueError(
                f"parameter {self.parameter!r} is not valid for target {self.target!r}; "
                f"expected one of {', '.join(allowed)}"
            )
        return self

    @property
    def selector(self) -> int:
        """The number of the edited object."""
        for value in (self.surface, self.field, self.wavelength):
            if value is not None:
                return value
        if self.target == "aperture":
            return 0
        raise ValueError("edit has no selector")


class FieldSpec(BaseModel):
    """One field of a replacement field set (angle fields, degrees)."""

    model_config = ConfigDict(extra="forbid")

    y_angle: float
    x_angle: float = 0.0
    weight: float | None = Field(
        default=None,
        description="Field weight; omitted keeps the weight of the field with the same number, 1 for a new field.",
    )
    vux: float | None = Field(default=None, description="Upper X vignetting factor; same default rule as weight (0 for a new field).")
    vlx: float | None = Field(default=None, description="Lower X vignetting factor.")
    vuy: float | None = Field(default=None, description="Upper Y vignetting factor.")
    vly: float | None = Field(default=None, description="Lower Y vignetting factor.")


class FieldSetReplacement(BaseModel):
    """Replace the whole field set of a single-zoom, angle-field lens.

    CODE V keeps weight and vignetting factors by field number when the count
    changes, so an omitted weight or factor keeps the value of the field with
    the same number and starts a new field at weight 1 and factors 0. Values
    beyond a shorter set are dropped. Nothing else in the lens changes; an image
    solve such as PIM stays in place.
    """

    model_config = ConfigDict(extra="forbid")

    fields: list[FieldSpec] = Field(min_length=1, max_length=10)


class UpdateRequest(BaseModel):
    """A batch of parameter edits, or one replacement of the field set (not both)."""

    model_config = ConfigDict(extra="forbid")

    edits: list[ParameterEdit] = Field(default_factory=list)
    field_set: FieldSetReplacement | None = Field(
        default=None,
        description=(
            "Replace the field set (count, angles, weights, vignetting factors) as its own "
            "transaction. It cannot be combined with edits in the same request."
        ),
    )
    zoom_position: int | None = Field(
        default=None,
        description="Target zoom position applied to every edit that does not set its own.",
    )

    @model_validator(mode="after")
    def _check_content(self) -> "UpdateRequest":
        if self.field_set is not None and self.edits:
            raise ValueError("field_set is its own transaction; send it without edits")
        if self.field_set is None and not self.edits:
            raise ValueError("an update needs edits or a field_set")
        return self


class EditOutcome(BaseModel):
    edit: ParameterEdit
    applied: bool
    previous_value: float | str | None = None
    new_value: float | str | None = None
    rejected_reason: str | None = None


class FieldSetOutcome(BaseModel):
    applied: bool
    previous_fields: list[LensField] = Field(default_factory=list)
    fields: list[LensField] = Field(
        default_factory=list, description="The field set read back after the change (empty when not applied)."
    )
    rejected_reason: str | None = None


class UpdateResult(BaseModel):
    source: Source
    outcomes: list[EditOutcome]
    field_set: FieldSetOutcome | None = Field(
        default=None, description="Present when the request replaced the field set."
    )
    restore_point: str | None = Field(
        default=None, description="Identifier of the recovery point taken before editing."
    )
    rolled_back: bool = False
    session_valid: bool = Field(
        default=True, description="False once the session can no longer be trusted for edits."
    )
    raw_output: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SaveResult(BaseModel):
    source: Source
    path: str
    bytes_written: int | None = None
    overwritten: bool = False
    raw_output: str | None = None


#: Upper bounds that keep one analysis inside the call budget: a spot grid traces grid squared rays
#: through COM, and the comparison CLIs use the same limits.
MAX_RAY_GRID = 101
MAX_FREQUENCIES = 101
Frequency = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class AnalysisOptions(BaseModel):
    """Optional analysis settings; the backend reports what it actually used."""

    model_config = ConfigDict(extra="forbid")

    zoom_position: int | None = None
    field_numbers: list[int] | None = Field(
        default=None, description="Empty or None means every field."
    )
    wavelength_numbers: list[int] | None = None
    ray_grid: int | None = Field(
        default=None, ge=1, le=MAX_RAY_GRID,
        description="Spot diagram grid size, for example 7 for a 7x7 grid (1 to 101).",
    )
    frequencies: list[Frequency] | None = Field(
        default=None, max_length=MAX_FREQUENCIES,
        description=(
            "Spatial frequencies for MTF in cycles/mm, finite and nonnegative, at most 101; "
            "the caller always supplies them."
        ),
    )
    azimuth: float | None = Field(
        default=None, allow_inf_nan=False,
        description=(
            "MTF azimuth in degrees; MTF always reports tangential (0) and sagittal (90), "
            "so only 0 is accepted."
        ),
    )
    mtf_type: MtfType = MtfType.DIFFRACTION
    plot_type: NativePlotType | None = Field(
        default=None,
        description=(
            "Required when kind is native_plot: layout, spot, mtf, "
            "ray_aberration or field_aberration."
        ),
    )


class AnalysisSettings(BaseModel):
    """What the backend really used, echoed back with the result."""

    zoom_position: int = 1
    field_numbers: list[int] = Field(default_factory=list)
    wavelength_numbers: list[int] = Field(default_factory=list)
    ray_grid: int | None = None
    frequencies: list[float] | None = None
    frequency_unit: str | None = Field(default=None, description="For example cycles/mm.")
    azimuth: float | None = None
    mtf_type: MtfType | None = None
    plot_type: NativePlotType | None = Field(
        default=None, description="Native plot that was drawn, when one was drawn."
    )
    wavefront_nrd: int | None = Field(default=None, description="Requested rays across pupil diameter for WAV.")
    notes: list[str] = Field(default_factory=list)


class ImagePayload(BaseModel):
    """A rendered image plus the local file it was written to."""

    path: str = Field(description="Absolute path of the result file kept on disk.")
    media_type: str = "image/png"
    base64_data: str = Field(description="Base64 encoded image bytes for MCP image content.")
    width: int | None = None
    height: int | None = None


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: AnalysisKind
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)


class TaskInfo(BaseModel):
    task_id: str
    kind: AnalysisKind
    state: TaskState
    source: Source
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: str | None = None
    error: ErrorInfo | None = None
    settings: AnalysisSettings | None = None
    raw_output: str | None = Field(
        default=None,
        description="Verbatim backend output; truncated content is flagged in warnings.",
    )
    output_truncated: bool = False
    history_only: bool = Field(
        default=False,
        description=(
            "True when the results belong to an earlier lens or lens revision, so they "
            "are kept as history rather than describing the lens that is open now."
        ),
    )
    warnings: list[str] = Field(default_factory=list)


class FirstOrderResult(BaseModel):
    source: Source
    units: Units
    zoom_position: int
    effective_focal_length: float | None = None
    back_focal_length: float | None = None
    front_focal_length: float | None = None
    f_number: float | None = None
    image_distance: float | None = None
    overall_length: float | None = None
    paraxial_image_height: float | None = None
    entrance_pupil_diameter: float | None = None
    entrance_pupil_distance: float | None = None
    exit_pupil_diameter: float | None = None
    exit_pupil_distance: float | None = None
    raw_output: str | None = None
    warnings: list[str] = Field(default_factory=list)
    precision_note: str = Field(
        default="",
        description="How the numbers were obtained and how precise they are.",
    )


class SpotDiagramResult(BaseModel):
    source: Source
    units: Units
    zoom_position: int
    field_number: int
    wavelength_numbers: list[int] = Field(default_factory=list)
    centroid_x: float | None = Field(
        default=None,
        description="Spot centroid X displacement from the chief ray, in image units.",
    )
    centroid_y: float | None = Field(
        default=None,
        description="Spot centroid Y displacement from the chief ray, in image units.",
    )
    rms_radius: float | None = Field(
        default=None, description="RMS spot radius from the native statistics."
    )
    max_radius: float | None = Field(
        default=None, description="Largest ray radius from the native statistics."
    )
    size_is_radius: bool = Field(
        default=True, description="True when the reported sizes are radii, not diameters."
    )
    plot_sample_count: int | None = Field(
        default=None, description="Ray count used for plotting, not for the statistics."
    )
    statistics_sample_count: int | None = Field(
        default=None,
        description="Ray count the native statistics were computed from, as reported by the option listing.",
    )
    image: ImagePayload | None = None
    raw_output: str | None = None
    warnings: list[str] = Field(default_factory=list)


class MtfCurve(BaseModel):
    field_number: int
    wavelength_numbers: list[int] = Field(default_factory=list)
    tangential: list[float] = Field(default_factory=list)
    sagittal: list[float] = Field(default_factory=list)
    analytic_limit: list[float] = Field(
        default_factory=list,
        description=(
            "The analytic diffraction limit reported by the same calculation, one value "
            "per frequency; the modulation never exceeds it."
        ),
    )


class MtfResult(BaseModel):
    source: Source
    zoom_position: int
    frequencies: list[float]
    frequency_unit: str = "cycles/mm"
    azimuth: float = 0.0
    mtf_type: MtfType = MtfType.DIFFRACTION
    curves: list[MtfCurve] = Field(default_factory=list)
    image: ImagePayload | None = None
    raw_output: str | None = None
    warnings: list[str] = Field(default_factory=list)


class WavefrontField(BaseModel):
    field_number: int
    rms_waves: float = Field(description="Native NOM RMS in waves at the saved image surface.")
    strehl: float = Field(description="CODE V's displayed Strehl approximation, including a reported zero.")
    rays_traced: int = Field(description="Actual number of pupil rays listed for this field.")


class WavefrontResult(BaseModel):
    source: Source
    zoom_position: int
    focus: Literal["nominal"] = "nominal"
    nrd: int = 20
    wavelength_numbers: list[int]
    reference_wavelength_number: int
    reference_wavelength_nm: float
    rms_equivalent_wavelength_nm: float | None = Field(
        default=None, description="Unavailable when the native WAV listing omits this value."
    )
    fields: list[WavefrontField]
    weighted_rms_waves: float
    weighted_strehl: float
    raw_output: str
    precision_note: str = "Values are parsed from the CODE V WAV listing at its printed precision."
    warnings: list[str] = Field(default_factory=list)


class NativePlotResult(BaseModel):
    """A plot drawn and exported by CODE V itself.

    The image is CODE V's own rendering of the option output, converted from the
    neutral plot file that option wrote, so it is deliberately unlike the
    service drawn spot and MTF pictures: those are redrawn from parsed numbers,
    this one carries no numeric result at all. The service only chooses which
    plot to draw, where to write it and how to verify the produced files.
    """

    source: Source
    plot_type: NativePlotType
    zoom_position: int
    image: ImagePayload | None = Field(
        default=None, description="The exported PNG, also returned as MCP image content."
    )
    plot_file_path: str | None = Field(
        default=None, description="The neutral .PLT file CODE V wrote, kept next to the PNG."
    )
    plot_file_bytes: int | None = Field(
        default=None, description="Size of the .PLT file in bytes."
    )
    raw_output: str | None = Field(
        default=None, description="Verbatim output of the drawing command."
    )
    warnings: list[str] = Field(default_factory=list)


class AnalysisSnapshot(BaseModel):
    """Everything currently known about the analysis slot."""

    source: Source
    task: TaskInfo | None = None
    first_order: FirstOrderResult | None = None
    spot_diagram: SpotDiagramResult | None = None
    mtf: MtfResult | None = None
    native_plot: NativePlotResult | None = None
    wavefront: WavefrontResult | None = None


class CancelResult(BaseModel):
    """Outcome of a cancellation request.

    A cancel request and an actually stopped calculation are different things,
    so the task state after the request is reported alongside the request flag.
    """

    source: Source
    requested: bool = Field(description="True when a task existed and was asked to stop.")
    still_running: bool = Field(
        default=False, description="True when the backend could not confirm that it stopped."
    )
    task: TaskInfo | None = None
    notes: str = ""


class CapabilityInfo(BaseModel):
    name: str
    supported: bool
    note: str | None = None


class StatusInfo(BaseModel):
    backend: str
    source: Source
    service_version: str
    codev_version: str | None = None
    ready: bool = False
    session_open: bool = False
    lens_open: bool = False
    current_lens: str | None = None
    working_directory: str | None = None
    task: TaskInfo | None = None
    capabilities: list[CapabilityInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
