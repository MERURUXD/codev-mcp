"""Backend interface shared by the simulated and the real COM backend.

The worker process owns exactly one backend instance and calls it serially, so
backends do not need their own locking. Every method either returns one of the
public models or raises a CodeVError subclass.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from .models import (
    AnalysisKind,
    AnalysisOptions,
    AnalysisRequest,
    AnalysisSnapshot,
    CapabilityInfo,
    CreateLensRequest,
    LensData,
    SaveResult,
    Source,
    StatusInfo,
    StructureRequest,
    StructureResult,
    TaskInfo,
    UpdateRequest,
    UpdateResult,
)
from .errors import ParameterError, UnsupportedError

SIMULATED_BACKEND = "simulated"
COM_BACKEND = "com"

BACKEND_NAMES = (SIMULATED_BACKEND, COM_BACKEND)


def default_working_directory(module_file: str | Path = __file__, environ=None) -> str:
    """Service owned scratch directory, used as the CODE V working directory.

    A source or editable install keeps ``<repository>/.codev-run``. Any other install would land
    beside site-packages (for example ``C:\\Python313\\Lib\\.codev-run``), which may not be
    writable, may be shared by every user of that Python and makes plot paths longer, so it uses
    ``%LOCALAPPDATA%\\codev-mcp`` instead.
    """
    checkout = Path(module_file).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file():
        return str(checkout / ".codev-run")
    environ = os.environ if environ is None else environ
    base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return str(Path(base) / "codev-mcp")


def check_native_plot_options(
    lens: LensData, options: AnalysisOptions, kind: AnalysisKind
) -> None:
    """Apply the native plot settings rules both backends have to agree on.

    A native plot is drawn by CODE V from the lens exactly as it is stored, so
    it takes no field, wavelength, frequency or sampling selection at all: those
    settings are refused instead of being quietly ignored, because a caller who
    passes them would otherwise believe the picture shows only what they asked
    for. The first release exports single zoom position lenses only.
    """
    if kind is not AnalysisKind.NATIVE_PLOT:
        if options.plot_type is not None:
            raise ParameterError(
                f"plot_type is only used by {AnalysisKind.NATIVE_PLOT.value}.",
                details={
                    "kind": kind.value,
                    "plot_type": options.plot_type.value,
                },
            )
        return
    if options.plot_type is None:
        raise ParameterError(
            "A native plot needs an explicit plot_type.",
            hint=(
                "Pass options.plot_type: layout, spot, mtf, ray_aberration or "
                "field_aberration."
            ),
        )
    if lens.zoom_positions > 1:
        raise UnsupportedError(
            "The first release exports native plots for single zoom position lenses "
            f"only; this lens has {lens.zoom_positions}.",
            details={"zoom_positions": lens.zoom_positions},
        )
    not_accepted = sorted(
        name
        for name, value in (
            ("field_numbers", options.field_numbers),
            ("wavelength_numbers", options.wavelength_numbers),
            ("ray_grid", options.ray_grid),
            ("frequencies", options.frequencies),
            ("azimuth", options.azimuth),
        )
        if value is not None
    )
    if "mtf_type" in options.model_fields_set:
        # The plot uses CODE V's own settings, so an explicitly supplied type is
        # refused even when it happens to be the default one: accepting it would
        # suggest the caller had influenced a choice that was never theirs.
        not_accepted.append("mtf_type")
    if not_accepted:
        raise ParameterError(
            "A native plot draws every enabled field and wavelength, so these "
            "settings are not accepted.",
            details={"not_accepted": sorted(not_accepted)},
        )


def check_mtf_azimuth(azimuth: float | None) -> None:
    """MTF reports the tangential and the sagittal curve; another azimuth would be echoed but unused."""
    if azimuth not in (None, 0, 0.0):
        raise ParameterError(
            "MTF always reports tangential (azimuth 0) and sagittal (azimuth 90) curves; "
            "another azimuth is not supported.",
            details={"azimuth": azimuth},
            hint="Leave azimuth out or pass 0.",
        )


def check_wavefront_options(lens: LensData, options: AnalysisOptions, kind: AnalysisKind) -> None:
    if kind is not AnalysisKind.WAVEFRONT:
        return
    if lens.zoom_positions != 1 or (options.zoom_position not in (None, 1)):
        raise UnsupportedError("Wavefront analysis currently supports single-zoom lenses only.")
    all_fields = [field.number for field in lens.fields]
    all_waves = [wave.number for wave in lens.wavelengths]
    if options.field_numbers is not None and options.field_numbers != all_fields:
        raise ParameterError("WAV analyses all fields; field_numbers must list all fields in order.")
    if options.wavelength_numbers is not None and options.wavelength_numbers != all_waves:
        raise ParameterError("WAV analyses all wavelengths; wavelength_numbers must list all wavelengths in order.")
    not_accepted = [name for name in ("ray_grid", "frequencies", "azimuth", "plot_type")
                    if getattr(options, name) is not None]
    if "mtf_type" in options.model_fields_set:
        not_accepted.append("mtf_type")
    if not_accepted:
        raise ParameterError("WAV does not accept settings for other analyses.",
                             details={"not_accepted": not_accepted})


class Backend(ABC):
    """Contract implemented by every backend."""

    name: str = "abstract"
    source: Source = Source.SIMULATED

    #: Methods that the worker is allowed to forward, in call order.
    METHODS = (
        "get_status",
        "open_lens",
        "create_lens",
        "get_lens",
        "update_lens",
        "edit_lens_structure",
        "run_analysis",
        "get_analysis",
        "cancel_analysis",
        "save_lens_as",
        "close_session",
    )

    @abstractmethod
    def get_status(self) -> StatusInfo:
        """Report backend, version, session, current lens, task and capabilities."""

    @abstractmethod
    def open_lens(self, path: str) -> LensData:
        """Open a working copy of an existing lens file."""

    @abstractmethod
    def create_lens(self, request: CreateLensRequest) -> LensData:
        """Create a bounded simple spherical lens in this service's session."""

    @abstractmethod
    def get_lens(self, zoom_position: int | None = None) -> LensData:
        """Read surfaces, materials, units, aperture, wavelengths and fields.

        zoom_position selects which zoom position the zoom dependent values are
        read from; None means the lens data already held by the backend.
        """

    @abstractmethod
    def update_lens(self, request: UpdateRequest) -> UpdateResult:
        """Apply a batch of parameter edits, with a restore point per batch."""

    @abstractmethod
    def edit_lens_structure(self, request: StructureRequest) -> StructureResult:
        """Apply bounded structural edits to a trusted service-created lens."""

    @abstractmethod
    def run_analysis(self, request: AnalysisRequest) -> TaskInfo:
        """Submit an analysis and return the task record."""

    @abstractmethod
    def get_analysis(self) -> AnalysisSnapshot:
        """Return the task state plus any finished results."""

    @abstractmethod
    def cancel_analysis(self) -> TaskInfo | None:
        """Request cancellation of the current analysis."""

    @abstractmethod
    def save_lens_as(self, path: str) -> SaveResult:
        """Save the working copy to a new file that does not exist yet."""

    @abstractmethod
    def close_session(self) -> StatusInfo:
        """Release the session this service created."""

    def stop(self) -> None:
        """Release resources held by the backend. Safe to call twice."""

    @property
    def session_start_seconds(self) -> float:
        """Extra time the next call may need because it first has to start a session."""
        return 0.0

    def release_leftovers(self) -> list[int]:
        """Stop what a killed predecessor left running in this working directory."""
        return []

    def capabilities(self) -> list[CapabilityInfo]:
        return []


def create_backend(name: str, **kwargs) -> Backend:
    """Instantiate a backend by name without importing the other one."""
    if name == SIMULATED_BACKEND:
        from .simulated import SimulatedBackend

        return SimulatedBackend(**kwargs)
    if name == COM_BACKEND:
        from .com_backend import ComBackend

        return ComBackend(**kwargs)
    raise ValueError(f"unknown backend {name!r}; expected one of {', '.join(BACKEND_NAMES)}")
