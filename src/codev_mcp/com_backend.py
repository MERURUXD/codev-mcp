"""Real CODE V backend: lens read, parameter edit and save as.

Calling conventions come from the Phase A report and from the three Phase C
probes (local validation records). The behaviours that shaped this code:

* values are read with Macro-PLUS database items through EvaluateExpression,
  which is fast but silently echoes the previous result when an item name is
  wrong, so every value is parsed strictly and cross-checked against the "lis"
  listing;
* an infinite radius comes back as 0.1E+19 and an infinite thickness as about
  9.9E+11, so infinity is detected by magnitude;
* CODE V ignores some edits without any error at all, for example when a
  parameter is controlled by a solve or when a zoom specific value is written
  for a parameter that was never zoomed, so every edit is confirmed by reading
  the value back and the whole batch is rolled back when a read back differs;
* the restore point is a lens file written with SAV and read back with RES.

Since the lens recovery design (local validation records) every successful
batch is also published as a checkpoint: the lens file is written to its own
revision file, restored, compared element by element against the verified
state, and only then pointed at by current.json. That is what an engine exit
restores from, so a committed edit is never lost and the original lens file is
never used as a silent fallback. The lens inside the engine is tracked by its
own state (empty, ready, updating, recovering, invalid) next to the session
state, and a lens that cannot be confirmed stays refused until the service is
restarted.
"""

from __future__ import annotations

import copy
import json
import math
import struct
import sys
import threading
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import aiming
from . import plotting
from .backend import (
    COM_BACKEND,
    Backend,
    check_native_plot_options,
    check_wavefront_options,
    default_working_directory,
)
from .checkpoints import (
    Checkpoint,
    CheckpointError,
    CheckpointPublishError,
    CheckpointVerificationError,
    CURRENT_POINTER_NAME,
    LensCheckpointStore,
    LensSnapshot,
    UnexpectedChangeError,
    change_expectations,
    call_hook,
    check_expected_values,
    checkpoint_format_version,
    collapse_expectations,
    compare_snapshots,
    describe_changes,
    edit_key,
    expand_allowed_changes,
    with_radius_flags,
    expectation_keys,
    hash_file,
    parameter_identity,
    read_snapshot,
    solve_coupled_changes,
    summarise,
    utc_now,
)
from .com_session import ERROR_LINE, ComSession, engine_is_alive
from .errors import (
    CodeVError,
    ComputationError,
    ErrorInfo,
    ErrorKind,
    NotReadyError,
    ParameterError,
    SessionInvalidError,
    UnsupportedError,
)
from .evaluation import MICROMETERS_PER_UNIT
from .fieldset import (
    VIGNETTING_LIMIT,
    field_set_commands,
    field_set_differences,
    resolve_field_set,
)
from .listing import (Listing, parse_listing, parse_nominal_wavefront, parse_spot_listing,
                      vignetting_mismatches)
from .modeling import (
    create_commands,
    created_lens_differences,
    plan_structure,
    structure_differences,
)
from .models import (
    AnalysisKind,
    AnalysisRequest,
    AnalysisSnapshot,
    AnalysisSettings,
    ApertureInfo,
    CapabilityInfo,
    CreateLensRequest,
    StructureRequest,
    StructureResult,
    EditOutcome,
    FieldSetOutcome,
    FieldSetReplacement,
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
from .safety import (
    INFINITE_RADIUS_THRESHOLD,
    INFINITE_THICKNESS_THRESHOLD,
    check_number,
    command_filespec,
    format_float,
    validate_filespec,
    validate_glass_name,
)

DIMENSION_TO_UNITS = {0: Units.INCH, 1: Units.CM, 2: Units.MM}

#: Tolerances used when comparing a requested value with the value CODE V
#: reports after the edit. The numbers round trip exactly in the probes, so a
#: loose tolerance here only hides real problems.
ABSOLUTE_TOLERANCE = 1e-9
RELATIVE_TOLERANCE = 1e-7

#: The surface listing is printed with about six significant digits, so the
#: cross-check against the database items has to allow for that rounding. A
#: stale value from a misspelled item differs by far more than this.
LISTING_ABSOLUTE_TOLERANCE = 1e-6
LISTING_RELATIVE_TOLERANCE = 1e-5

SURFACE_ITEM = {"radius": "RDY", "thickness": "THI"}
FIELD_ITEM = {"y_angle": "YAN", "x_angle": "XAN", "weight": "WTF",
              "vux": "VUX", "vlx": "VLX", "vuy": "VUY", "vly": "VLY"}
WAVELENGTH_ITEM = {"micrometers": "WL", "weight": "WTW", "is_reference": "REF"}

#: Parameters that cannot take a zoom qualifier in the commands verified so far.
NO_ZOOM_QUALIFIER = {"glass", "micrometers", "weight", "is_reference"}

#: Set once the Phase D acceptance script has produced real machine evidence:
#: the first order, spot diagram and MTF sections passed on 2026-09-17
#: on CODE V 10.2. Detailed records are retained locally.
ANALYSES_VERIFIED = True

#: Set only once the Phase H acceptance script has exported all five native
#: plots on a real machine. It is deliberately separate from ANALYSES_VERIFIED:
#: the export is new code with its own failure modes, and the Phase D evidence
#: covers the analytic results, not a plot file CODE V converted for us.
NATIVE_PLOT_VERIFIED = True
#: Nominal WAV passed the 2026-09-26 two-run real MCP comparison acceptance.
#: The separate W1 probe confirmed the exact command and no observable lens edit.
WAVEFRONT_VERIFIED = True

#: The only CODE V commands this service sends for a native plot. The caller
#: chooses a plot type; the command text is built here and nowhere else.
NATIVE_PLOT_COMMANDS: dict[NativePlotType, str] = {
    NativePlotType.LAYOUT: "vie;lab no;go",
    NativePlotType.SPOT: "spo;air yes;go",
    NativePlotType.MTF: "mtf;mfr 100;ifr 10;go",
    NativePlotType.RAY_ABERRATION: "rim;go",
    NativePlotType.FIELD_ABERRATION: "fie;lsa;go",
}

#: PNG file signature, checked before a converted plot is reported as an image.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: How much decompressed picture data is produced per step while the converted
#: PNG is checked. A real 6268x5892 plot inflates to about 148 MB, so the check
#: streams it instead of holding the whole bitmap in memory.
PNG_DECOMPRESS_STEP = 1 << 16

#: CODE V truncates a plot file filespec at this many characters and only
#: appends the .PLT extender when it still fits afterwards. Measured on this
#: machine with the Phase H probe: a 70 character result directory plus a 26
#: character name produced a 10 character file name, a name of 8 characters
#: under a 71 character directory produced no extender at all, and 5 characters
#: produced abcde.plt. Staying inside the limit is not cosmetic: the conversion
#: command looks for the logical name, so a plot file CODE V had to shorten can
#: no longer be converted.
MAX_PLOT_FILESPEC = 80
PLOT_FILE_SUFFIX = ".PLT"
#: The shortest file stem that can still be made unique.
MIN_PLOT_STEM = 8

#: MTF_1FLD enum values from the type library: DIF/GEO and SIW/SQW.
MTF_TYPE_DIF = 0
MTF_TYPE_GEO = 1
MTF_TYPE_SINE = 0
MTF_TYPE_SQUARE = 1

#: The default ray grid for the plotted spot diagram, and for the independent
#: cross-check of the native spot statistics.
DEFAULT_PLOT_GRID = 7
DEFAULT_STATISTICS_GRID = 9

#: The aimed pupil map may deviate from its calibration rays by this fraction
#: of the pupil radius before the plotted grid is flagged as possibly distorted.
PUPIL_MAP_TOLERANCE = 0.005

#: How long one get_analysis poll waits for the running option, and how long a
#: cancellation waits for StopCommand to take effect.
POLL_WAIT_SECONDS = 5
CANCEL_CONFIRM_SECONDS = 10

#: CODE V starts its windowless engine unreliably on this machine: the engine
#: sometimes dies behind an application error dialog before StartCodeV returns.
#: The failure is transient, so the session is retried a few times, each time
#: after the crashed processes have been cleaned up.
START_ATTEMPTS = 4
START_RETRY_DELAY_SECONDS = 3.0


class LensState(str, Enum):
    """How far the lens inside the engine may be trusted.

    The state is separate from the engine state: a running engine with an
    unverified or lost lens is not a usable session, and a rebuild never clears
    the invalid state on its own.
    """

    EMPTY = "empty"
    READY = "ready"
    UPDATING = "updating"
    RECOVERING = "recovering"
    INVALID = "invalid"


#: States in which an automatic recovery is allowed to run. A lens that was
#: being edited or restored when the engine died is completed by the recovery
#: path instead, and an invalid session stays invalid until the service restarts.
RECOVERABLE_STATES = frozenset({LensState.READY, LensState.UPDATING, LensState.RECOVERING})

#: States in which the lens inside the engine may be read for a result.
READABLE_LENS_STATES = frozenset({LensState.READY})

#: States in which the lens may be changed or analysed.
WRITABLE_LENS_STATES = frozenset({LensState.READY})

#: States in which another lens file may be opened. The recovery states are
#: included on purpose: opening a lens is how a session that lost its lens is
#: brought back, while an invalid or closed session stays refused.
OPEN_LENS_STATES = frozenset(RECOVERABLE_STATES | {LensState.EMPTY})

#: Where the engine is killed on purpose by a fault injection test.
TAKEDOWN_BEFORE_COMMIT = "before_commit"
TAKEDOWN_BEFORE_METADATA = "before_metadata"
TAKEDOWN_BEFORE_POINTER = "before_pointer"


class PublishOutcome(str, Enum):
    """Result of committing a verified revision to disk."""

    COMMITTED = "committed"
    FAILED = "failed"

#: Relative differences allowed between the native SPO statistics and the spot
#: size recomputed from the traced ray grid. The two use different sampling, so
#: a tolerance is expected; a large difference signals a problem with the pupil
#: mapping or the listing parse.
#: The RMS radius is a mean like quantity and is stable against sample size;
#: Phase D measured agreement within a few percent. The 100% spot size is an
#: extreme value: with 531 traced rays against the 1756 rays the option used,
#: the traced maximum came out 26% smaller, so the bound for it is looser.
SPOT_RMS_TOLERANCE = 0.25
SPOT_MAX_TOLERANCE = 0.60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CommittedBatch:
    """The batch this backend published last, tied to its own transaction.

    A commit fact belongs to one transaction of one lens, never to the backend
    as a whole: a later batch that fails before its own commit must not inherit
    the success of an earlier one.
    """

    transaction_id: str
    lens_id: str | None
    revision: int
    result: UpdateResult

@dataclass
class PlannedEdit:
    """One edit together with the command and the read back that confirms it."""

    edit: ParameterEdit
    command: str
    read_item: str
    kind: str
    expected: float | int | str | None
    previous: float | str | None
    note: str | None = None


class ComBackend(Backend):
    """Drives a real, windowless CODE V 10.2 session."""

    name = COM_BACKEND
    source = Source.CODEV

    def __init__(
        self,
        *,
        working_directory: str | Path | None = None,
        session: Any = None,
        session_factory: Callable[[], Any] | None = None,
        log: Callable[[str], None] | None = None,
        trace: Callable[[str], None] | None = None,
        **_ignored: object,
    ) -> None:
        self.working_directory = Path(working_directory or default_working_directory())
        self.working_directory.mkdir(parents=True, exist_ok=True)
        self.result_directory = self.working_directory / "results"
        self.checkpoint_store = LensCheckpointStore(
            self.working_directory / "checkpoints", backend_id=uuid.uuid4().hex
        )
        self.checkpoint_store.ensure()
        if log is not None and not callable(log):
            raise TypeError("log must be callable")
        self.log = log or (lambda message: None)
        self._log_failure_reported = False
        self.trace = trace
        #: Test hook: called with the name of a fault injection point.
        self.fault_hook: Callable[[str], None] | None = None

        self._session = session
        self._session_factory = session_factory
        self._start_error: str | None = None
        self._session_valid = True
        #: How many times the session had to be rebuilt after the engine died.
        self.session_restarts = 0
        self.start_attempts = START_ATTEMPTS

        self._lens: LensData | None = None
        self._lens_open = False
        self._closed = False
        self._listing: Listing | None = None
        self._source_path: str | None = None
        self._restore_seq = 0

        # ---------------------------------------------------- lens integrity
        self._lens_state = LensState.EMPTY
        self._lens_id: str | None = None
        self._lens_directory: Path | None = None
        self._committed_revision: int | None = None
        self._modelled_lens_ids: set[str] = set()
        self._last_known_snapshot: LensSnapshot | None = None
        self._checkpoint_load_attempted = False
        self._reload_required = False
        self._lens_lock = threading.Lock()
        self._transaction_seq = 0
        #: The commit fact of the batch this instance published last. It is tied
        #: to its transaction, so it can never answer for another request.
        self._committed_result: CommittedBatch | None = None
        self.recovery_count = 0
        self.last_recovery: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None

        self._task: TaskInfo | None = None
        self._analysis_seq = 0
        self._analysis_payload: dict[str, Any] = {}
        #: Committed lens revision the stored analysis results belong to.
        self._analysis_revision: int | None = None
        #: Lens identity the stored analysis results belong to, so results of a
        #: different lens are never presented as belonging to the open one.
        self._analysis_lens_id: str | None = None
        #: Set while an option based analysis is still running inside CODE V.
        self._pending_spot: dict[str, Any] | None = None
        #: Set while a native plot option is still drawing inside CODE V.
        self._pending_native_plot: dict[str, Any] | None = None
        #: How long cancel_analysis waits for StopCommand to be confirmed.
        self.cancel_confirm_seconds = CANCEL_CONFIRM_SECONDS

    # ------------------------------------------------------------------ logging

    @property
    def log(self) -> Callable[[str], None]:
        """The diagnostics sink; assigning one wraps it so it can never raise.

        A log sink (a file, a pipe, a callback provided by the caller) can fail
        at any time, and diagnostics must never change an outcome: a committed
        revision stays committed even when writing about it is impossible. Every
        assignment goes through :func:`_safe_log` for that reason.
        """
        return self._log

    @log.setter
    def log(self, sink: Callable[[str], None]) -> None:
        self._log = self._safe_log(sink)
        self._log_failure_reported = False

    @staticmethod
    def _safe_log(sink: Callable[[str], None] | None) -> Callable[[str], None]:
        """Wrap a log sink so a failing sink is reported, never propagated."""
        if sink is None:
            return lambda message: None

        state = {"broken": False}

        def safe(message: str) -> None:
            try:
                sink(message)
            except Exception as exc:  # noqa: BLE001 - diagnostics are not outcomes
                if not state["broken"]:
                    state["broken"] = True
                    print(
                        f"codev-mcp: the log sink failed ({type(exc).__name__}: {exc}); "
                        f"dropping: {message}",
                        file=sys.stderr,
                    )

        return safe

    # ---------------------------------------------------------------- session

    def _new_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        return ComSession(
            starting_directory=self.working_directory, log=self.log, trace=self.trace
        )

    def _get_session(self) -> Any:
        if self._session is not None:
            if not self._session_is_dead(self._session):
                return self._session
            # The engine died under the COM server. Releasing it here keeps the
            # next call from blocking forever on a session that cannot answer.
            self.log("the CODE V engine died; discarding the session and starting a new one")
            self._note_engine_lost("the CODE V engine exited")
        if self._closed:
            raise NotReadyError(
                "The session was closed; restart the service to open a new one.",
            )
        if self._start_error is not None:
            raise SessionInvalidError(
                f"The CODE V session could not be started: {self._start_error}",
                hint="Fix the reported problem and restart the service.",
            )
        session, version = self._start_session_with_retries()
        self._session = session
        self.log(f"CODE V session started, version {version}")
        return session

    @staticmethod
    def _session_is_dead(session: Any) -> bool:
        """Whether a session can no longer serve calls.

        The COM server outlives a crashed engine and then blocks on every call,
        so the watchdog flag and the engine liveness are both consulted before
        the session is trusted again.
        """
        if getattr(session, "engine_dead", False) or getattr(session, "closed", False):
            return True
        if not getattr(session, "started", True):
            return True
        engine_pids = getattr(session, "engine_pids", None)
        if not engine_pids:
            return False
        return not any(engine_is_alive(pid) for pid in engine_pids)

    def _note_engine_lost(self, reason: str) -> None:
        """Release a session whose engine died and record what that breaks.

        The pending batch is never replayed: while a batch was in flight the
        engine may have applied part of it, so the lens is only brought back by
        the recovery path, from the last committed checkpoint. An engine loss
        that a rollback already reported keeps the invalid state. Either way the
        recorded analysis task is marked as failed and its lens revision is
        kept, so a restarted lens is never presented as the lens that produced
        an old result.
        """
        session = self._session
        self._session = None
        # The engine is gone, so the cached read describes a lens that no longer
        # exists; the next call re-reads whatever the recovery restored.
        self._lens = None
        self._listing = None
        self._reload_required = False
        self._pending_spot = None
        self._pending_native_plot = None
        self._fail_running_task(reason)
        if self._lens_state is LensState.EMPTY:
            pass
        elif self._lens_state is LensState.INVALID:
            self._checkpoint_load_attempted = True
        elif self._committed_revision is not None:
            self._lens_state = LensState.RECOVERING
            self._reload_required = True
            self._log_recovery(
                reason=reason,
                revision=self._committed_revision,
                result="pending",
                detail="the last committed checkpoint will be restored",
                failed_operation="engine_lost",
            )
            self.log(
                "the engine was lost while a lens was open; the last committed "
                f"checkpoint (revision {self._committed_revision}) will be restored "
                "and the interrupted batch is not replayed"
            )
        else:
            # A batch was being applied and no committed revision describes the
            # lens: nothing may be trusted after the engine died.
            self._invalidate_lens(
                "the engine died while the lens was being changed and no committed "
                "revision describes it"
            )
        if session is None:
            return
        self.session_restarts += 1
        try:
            session.stop()
        except Exception as exc:  # noqa: BLE001 - discarding must not fail the call
            self.log(f"could not stop the dead session: {type(exc).__name__}: {exc}")

    def _invalidate_lens(self, reason: str) -> None:
        """Mark the session unusable for lens work and say why."""
        self._lens_state = LensState.INVALID
        self._session_valid = False
        self._reload_required = False
        self._checkpoint_load_attempted = True
        self.log(f"the lens session is now invalid: {reason}")
        self._log_recovery(
            reason=reason,
            revision=self._committed_revision,
            result="failed",
            detail=reason,
            failed_operation="session_invalid",
        )

    def _fail_running_task(self, reason: str) -> None:
        """Record why a running analysis cannot continue.

        The interrupted task keeps its identifier, its lens revision and the
        reason, so a later get_analysis call can answer what happened even after
        the engine was rebuilt.
        """
        task = self._task
        if task is None:
            return
        message = "The CODE V engine exited while the analysis was running."
        task.error = ErrorInfo(
            kind=ErrorKind.SESSION_INVALID,
            message=message,
            details={
                "reason": reason,
                "interrupted": True,
                "lens_id": self._lens_id,
                "lens_revision": self._committed_revision,
            },
            hint="Restart the analysis after the service has restored the lens.",
        )
        if task.state in {TaskState.RUNNING, TaskState.QUEUED}:
            task.state = TaskState.FAILED
            task.finished_at = _utc_now()
            task.progress = message
            task.warnings.append(
                "The analysis was interrupted by an engine exit; any file it already "
                "wrote is kept, but the result is not complete."
            )
            # An incomplete task has no usable result; a task that already
            # SUCCEEDED keeps its payload, because the results of the last
            # committed lens revision are still valid and are reported as
            # history rather than thrown away.
            self._analysis_payload = {}
        self._log_recovery(
            reason=reason,
            revision=self._committed_revision,
            result="failed",
            detail=f"analysis {task.task_id} failed: {message}",
            failed_operation="analysis_interrupted",
        )

    # ------------------------------------------------- lens state and recovery

    def _lens_ready(self) -> bool:
        return self._lens_state is LensState.READY

    def _ensure_ready(
        self,
        allowed: frozenset[LensState],
        *,
        operation: str,
        action: str,
    ) -> None:
        """Make sure the lens is in a state this operation may use.

        Called at the top of every public lens operation, so a failed recovery,
        a crashed engine or a restore that could not be confirmed can never be
        walked around by a later call. Only the recovery path itself is allowed
        to move the lens out of the invalid state, and only after an engine
        restart has established a new session.

        The engine is brought back first: a rebuild is what turns a lost engine
        into a lens that the recovery path has to restore from its checkpoint.
        """
        session = self._session
        try:
            # Runs the rebuild, which also records that the lens has to be
            # restored; an invalid or closed session raises here.
            self._require_session()
        except NotReadyError as exc:
            # A session that was closed on request keeps its own wording, but the
            # refusal is reported as a session that cannot be used.
            raise SessionInvalidError(exc.message, hint=exc.hint) from exc
        if not self._session_valid and not self._rebuild_is_pending():
            # A session that was marked invalid must not be walked around by a
            # rebuild: the refusal names the invalid session. A session that only
            # lost its engine is rebuilt and restored below instead.
            raise SessionInvalidError(
                "The session was marked invalid after a failed recovery.",
                hint="Writes are refused until the service is restarted.",
            )
        if not self._recovery_needed():
            if self._lens_state in allowed:
                return
            raise SessionInvalidError(
                f"{operation} is refused because the lens state is {self._lens_state.value}.",
                details={
                    "lens_state": self._lens_state.value,
                    "session_valid": self._session_valid,
                    "lens_id": self._lens_id,
                    "committed_revision": self._committed_revision,
                },
                hint=action,
            )
        if self._checkpoint_load_attempted:
            raise SessionInvalidError(
                f"{operation} is refused: the session was marked invalid and the recovery "
                "of the last committed checkpoint did not succeed.",
                details={
                    "lens_state": self._lens_state.value,
                    "session_valid": self._session_valid,
                    "last_recovery": self.last_recovery,
                },
                hint="Restart the service to establish a new, trusted session.",
            )
        if self._lens_state not in RECOVERABLE_STATES:
            raise SessionInvalidError(
                f"{operation} is refused because the lens state is {self._lens_state.value}.",
                details={
                    "lens_state": self._lens_state.value,
                    "session_valid": self._session_valid,
                    "last_recovery": self.last_recovery,
                },
                hint=action,
            )
        self._recover_lens()
        if self._lens_state not in allowed:
            raise SessionInvalidError(
                f"{operation} is refused because the lens state is {self._lens_state.value}.",
                details={"lens_state": self._lens_state.value},
                hint=action,
            )

    def _ensure_safe_for(self, operation: str, *, action: str) -> None:
        """Guard a lens operation that only reads the current lens."""
        self._ensure_ready(READABLE_LENS_STATES, operation=operation, action=action)

    def _recovery_needed(self) -> bool:
        """Whether the open lens has to be restored from its checkpoint now."""
        return self._lens_state is LensState.RECOVERING and self._rebuild_is_pending()

    def _rebuild_is_pending(self) -> bool:
        """A committed lens is waiting to be restored into the engine."""
        if not self._reload_required or self._committed_revision is None:
            return False
        if self._lens_id is None or self._lens_directory is None:
            return False
        session = self._session
        return session is None or not self._session_is_dead(session)

    # ------------------------------------------------------- checkpoint helpers

    def _require_lens_directory(self) -> Path:
        if self._lens_directory is None or self._lens_id is None:
            raise SessionInvalidError(
                "No lens session is open, so there is no checkpoint directory.",
                hint="Call open_lens first.",
            )
        return self._lens_directory

    def _current_checkpoint(self) -> Checkpoint:
        """The committed checkpoint of the lens that is currently open.

        The revision is read through ``current.json`` rather than from its file
        name, so the revision the recovery restores is the one the service
        committed last, and it is checked against the lens identity this backend
        holds: a pointer that was replaced, damaged or belongs to another lens
        is refused instead of being restored.
        """
        if self._lens_directory is None or self._committed_revision is None:
            raise SessionInvalidError(
                "No committed lens checkpoint is available for the open lens.",
                details={
                    "lens_id": self._lens_id,
                    "committed_revision": self._committed_revision,
                },
                hint="Call open_lens to establish a lens with a checkpoint.",
            )
        checkpoint = self.checkpoint_store.load_current(self._lens_directory)
        if checkpoint.revision != self._committed_revision:
            raise CheckpointError(
                "The committed checkpoint pointer does not name the revision this "
                "session last committed.",
                details={
                    "committed_revision": self._committed_revision,
                    "pointer_revision": checkpoint.revision,
                    "path": str(self._lens_directory / CURRENT_POINTER_NAME),
                },
            )
        if self._lens_id and checkpoint.lens_id != self._lens_id:
            raise CheckpointError(
                "The committed checkpoint belongs to another lens of this backend.",
                details={
                    "lens_id": self._lens_id,
                    "checkpoint_lens_id": checkpoint.lens_id,
                },
            )
        return checkpoint

    def _committed_lens_path(self) -> Path | None:
        if self._lens_directory is None or self._committed_revision is None:
            return None
        return self._lens_directory / f"revision-{self._committed_revision:06d}.len"

    def _next_transaction_id(self) -> str:
        self._transaction_seq += 1
        return f"tx-{self._transaction_seq:06d}"

    @staticmethod
    def _describe_failure(failure: dict[str, Any] | None) -> str | None:
        if not failure:
            return None
        message = str(failure.get("message") or "")
        detail = str(failure.get("detail") or "")
        return f"{message}: {detail}" if detail and detail not in message else message

    def _log_recovery(
        self,
        *,
        reason: str,
        revision: int | None,
        result: str,
        detail: str,
        failed_operation: str,
    ) -> dict[str, Any]:
        record = {
            "at": utc_now(),
            "reason": reason,
            "revision": revision,
            "result": result,
            "detail": detail,
            "operation": failed_operation,
        }
        self.last_recovery = record
        self._write_recovery_record(record)
        return record

    def _write_recovery_record(self, record: dict[str, Any]) -> None:
        path = getattr(self.checkpoint_store, "recovery_log", None)
        if path is None:
            return
        try:
            call_hook(self.fault_hook, "recovery_record")
        except Exception as exc:  # noqa: BLE001 - the record is diagnostics
            self.log(f"the recovery record hook failed: {type(exc).__name__}: {exc}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.log(f"could not append to the recovery log: {exc}")

    # ------------------------------------------------------------- recovery

    def _recover_lens(self) -> None:
        """Restore the last committed checkpoint into a freshly started engine.

        Nothing is replayed: the checkpoint is a lens file, so restoring it puts
        the engine back in the state the last successful batch was verified in.
        """
        with self._lens_lock:
            self._reload_required = False
            revision = self._committed_revision
            lens_id = self._lens_id
            lens_path = self._committed_lens_path()
            try:
                checkpoint = self._current_checkpoint()
            except CheckpointError as exc:
                self._checkpoint_load_attempted = True
                self._invalidate_lens(
                    f"the last committed checkpoint could not be used: {exc.message}"
                )
                raise
            self._lens_state = LensState.RECOVERING
            try:
                self._restore_checkpoint(checkpoint)
            except (CheckpointError, SessionInvalidError) as exc:
                details = getattr(exc, "details", {})
                if self._lens_state is LensState.RECOVERING:
                    if details.get("engine_dead"):
                        # The engine died again while the checkpoint was being
                        # loaded. The checkpoint itself is still sound, so the
                        # next call may retry against a rebuild.
                        self._log_recovery(
                            reason="the engine exited while the checkpoint was being restored",
                            revision=revision,
                            result="failed",
                            detail=exc.message,
                            failed_operation="recovery_load",
                        )
                    else:
                        self._invalidate_lens(
                            f"the last committed checkpoint could not be restored: {exc.message}"
                        )
                raise
            self._lens_state = LensState.READY
            self._session_valid = True
            self._checkpoint_load_attempted = False
            self.recovery_count += 1
            self._log_recovery(
                reason="the CODE V engine exited and had to be rebuilt",
                revision=revision,
                result="succeeded",
                detail=(
                    f"lens {lens_id} was restored from the committed checkpoint at "
                    f"{lens_path} and verified"
                ),
                failed_operation="engine_lost",
            )
            self.log(
                f"the lens was restored from checkpoint revision {revision} and verified"
            )

    def _restore_checkpoint(self, checkpoint: Checkpoint) -> LensSnapshot:
        """Load a checkpoint file into the engine and verify what came back."""
        session = self._require_session()
        try:
            output = session.command(f"res {command_filespec(checkpoint.lens_path)}")
        except Exception as exc:  # noqa: BLE001 - a dead engine is not a bad checkpoint
            self._lens = None
            self._listing = None
            self._lens_open = False
            raise SessionInvalidError(
                "The CODE V engine did not confirm that the checkpoint was restored.",
                details={
                    "path": str(checkpoint.lens_path),
                    "revision": checkpoint.revision,
                    "engine_dead": self._engine_is_gone(session),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                hint="Retry the call; if it fails again, restart the service.",
            ) from exc
        if "has been restored" not in str(output):
            self._lens = None
            self._listing = None
            self._lens_open = False
            raise CheckpointError(
                "CODE V did not confirm that the checkpoint lens file was restored.",
                details={"path": str(checkpoint.lens_path), "revision": checkpoint.revision},
                raw_output=str(output),
            )
        self._lens = None
        self._listing = None
        self._lens_open = True
        lens = self._read_lens()
        snapshot = read_snapshot(session, lens, self._listing)
        problems = compare_snapshots(checkpoint.snapshot, snapshot)
        if problems:
            raise CheckpointVerificationError(
                "The lens restored from the checkpoint does not match the stored state.",
                details={
                    "path": str(checkpoint.lens_path),
                    "revision": checkpoint.revision,
                    "differences": problems[:10],
                    "summary": summarise(problems),
                },
            )
        self._last_known_snapshot = snapshot
        return snapshot

    @staticmethod
    def _engine_is_gone(session: Any) -> bool:
        if session is None:
            return True
        if getattr(session, "engine_dead", False):
            return True
        engine_pids = getattr(session, "engine_pids", None)
        if not engine_pids:
            return False
        return not any(engine_is_alive(pid) for pid in engine_pids)

    # ------------------------------------------------------------ publishing

    def _publish_checkpoint(
        self,
        directory: Path,
        lens_id: str,
        revision: int,
        candidate: Path,
        snapshot: LensSnapshot,
    ) -> PublishOutcome:
        """Verify a candidate lens file and commit it as a new revision.

        The candidate is only a revision once ``current.json`` points at it, so
        every failure before that leaves the previous revision committed. This
        deliberately does not rely on modification times.

        Once the pointer is replaced the revision is committed, so nothing after
        that point may turn the outcome into a failure: bookkeeping and logging
        are best effort, because a rollback then would leave the pointer, the
        in memory revision and the lens inside the engine disagreeing.
        """
        started = time.perf_counter()
        try:
            if self._engine_is_gone(self._session):
                return PublishOutcome.FAILED
            self._check_candidate(candidate)
            restored = self._load_candidate(candidate)
            problems = compare_snapshots(snapshot, restored)
            if problems:
                raise CheckpointVerificationError(
                    "The candidate lens file did not restore to the state it was saved from.",
                    details={
                        "path": str(candidate),
                        "revision": revision,
                        "differences": problems[:10],
                        "summary": summarise(problems),
                    },
                )
            call_hook(self.fault_hook, TAKEDOWN_BEFORE_METADATA)
            checkpoint = self.checkpoint_store.publish(
                directory,
                lens_id,
                revision,
                candidate,
                restored,
                source_path=self._source_path,
            )
        except Exception as exc:  # noqa: BLE001 - any failure keeps the old pointer
            self.log(
                f"publishing checkpoint revision {revision} failed: "
                f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            )
            return PublishOutcome.FAILED
        self._committed_revision = revision
        self._last_known_snapshot = restored
        # From here on the commit stands; only diagnostics are recorded, and
        # they must not raise: the caller would otherwise roll back a revision
        # that is already the committed one.
        try:
            self._record_committed_checkpoint(checkpoint, started)
        except Exception:  # noqa: BLE001 - the commit is already durable
            # Keep the bookkeeping that matters and report nothing through the
            # failing sink: a log that throws is not a reason to undo a commit.
            self.last_checkpoint = {
                "revision": checkpoint.revision,
                "path": str(checkpoint.lens_path),
                "source_path": self._source_path,
                "sha256": checkpoint.lens_sha256,
                "size": checkpoint.lens_size,
                "created_at": checkpoint.created_at,
                "com_calls": getattr(self._session, "call_count", None),
            }
        return PublishOutcome.COMMITTED

    def _record_committed_checkpoint(self, checkpoint: Checkpoint, started: float) -> None:
        """Bookkeeping and logging of a revision that is already committed."""
        elapsed = time.perf_counter() - started
        self.last_checkpoint = {
            "revision": checkpoint.revision,
            "path": str(checkpoint.lens_path),
            "source_path": self._source_path,
            "sha256": checkpoint.lens_sha256,
            "size": checkpoint.lens_size,
            "created_at": checkpoint.created_at,
            "write_seconds": round(elapsed, 3),
            "com_calls": getattr(self._session, "call_count", None),
        }
        self.log(
            f"checkpoint revision {checkpoint.revision} committed at "
            f"{checkpoint.lens_path} ({checkpoint.lens_size} bytes, {elapsed:.2f}s)"
        )

    @staticmethod
    def _check_candidate(candidate: Path) -> None:
        if not candidate.exists():
            raise CheckpointPublishError(
                "CODE V did not write the lens file for the new revision.",
                details={"path": str(candidate)},
            )
        size = candidate.stat().st_size
        if size <= 0:
            raise CheckpointPublishError(
                "The lens file CODE V wrote for the new revision is empty.",
                details={"path": str(candidate)},
            )

    def _load_candidate(self, candidate: Path) -> LensSnapshot:
        """Load a candidate lens file and read back what it contains."""
        self._load_lens_file(self._require_session(), candidate)
        lens = self._read_lens()
        return read_snapshot(self._require_session(), lens, self._listing)

    def _load_lens_file(self, session: Any, path: Path) -> None:
        """RES a lens file into the engine and confirm that it arrived."""
        output = session.command(f"res {command_filespec(path)}")
        if "has been restored" not in str(output):
            raise ComputationError(
                "CODE V did not confirm that the lens file was restored.",
                details={"path": str(path)},
                raw_output=str(output),
            )
        self._lens = None
        self._listing = None
        self._lens_open = True

    def _save_lens_file(self, session: Any, path: Path, action: str) -> None:
        """Run SAV for a recovery file or a checkpoint candidate."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        output = session.command(f"sav {command_filespec(path)}")
        if not path.exists():
            raise ComputationError(
                f"CODE V did not write {action}.",
                details={"path": str(path)},
                raw_output=str(output),
            )
        size = path.stat().st_size
        if size <= 0:
            raise ComputationError(
                f"CODE V wrote an empty file for {action}.",
                details={"path": str(path)},
            )
        self.log(f"{action}: {path} ({size} bytes)")

    # ------------------------------------------------------ transaction records

    def _transaction_prefix(self, transaction_id: str) -> str:
        prefix = transaction_id
        if self._lens_id:
            prefix = f"{prefix}-{self._lens_id[:8]}"
        if self._committed_revision is not None:
            prefix = f"{prefix}-r{self._committed_revision:06d}"
        return prefix

    def _write_transaction_record(
        self, directory: Path, transaction_id: str, payload: dict[str, Any]
    ) -> bool:
        """Write a transaction record; False when it could not be written."""
        try:
            record = dict(payload)
            record.setdefault("lens_id", self._lens_id)
            if self.checkpoint_store is None:  # pragma: no cover - always present
                return False
            self.checkpoint_store.write_transaction(directory, transaction_id, record)
            return True
        except Exception as exc:  # noqa: BLE001 - records are diagnostics only
            self._log_failed(
                f"could not write the transaction record: {type(exc).__name__}: {exc}"
            )
            return False

    def _log_failed(self, message: str) -> None:
        """Report a diagnostics failure at most once, and never let it raise."""
        if self._log_failure_reported:
            return
        self._log_failure_reported = True
        self.log(message)

    def _start_session_with_retries(self) -> tuple[Any, str]:
        """Start a session, retrying while the engine fails to come up.

        On this machine StartCodeV sometimes loses the engine behind an
        application error dialog. The failure clears on its own, so the attempt
        is repeated a few times instead of surfacing as a service failure.
        """
        last_error: Exception | None = None
        attempts = max(1, self.start_attempts)
        for attempt in range(1, attempts + 1):
            session = self._new_session()
            try:
                return session, session.start()
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last_error = exc
                self.log(
                    f"session start attempt {attempt}/{attempts} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    session.stop()
                except Exception:  # noqa: BLE001 - a failed attempt is abandoned
                    pass
                if attempt < attempts:
                    time.sleep(START_RETRY_DELAY_SECONDS)
        self._start_error = f"{type(last_error).__name__}: {last_error}"
        raise last_error

    def _require_session(self) -> Any:
        session = self._get_session()
        if not getattr(session, "started", True):
            raise SessionInvalidError("The CODE V session is not running.")
        return session

    def _require_lens(self) -> LensData:
        self._require_session()
        if self._lens is None:
            if self._lens_open:
                return self._read_lens()
            raise NotReadyError(
                "No lens is open.",
                hint="Call open_lens with a lens file first.",
            )
        return self._lens

    def _require_valid_session(self) -> None:
        if not self._session_valid:
            raise SessionInvalidError(
                "The session was marked invalid after a failed recovery.",
                hint="Writes are refused until the service is restarted.",
            )

    def _require_no_running_task(self, action: str) -> None:
        """Refuse lens work while an analysis is still running inside CODE V.

        The plan requires that no other lens operation is inserted while a long
        analysis runs; calls that could reset the output buffer would also
        destroy the result of the asynchronous command.
        """
        if self._task is not None and self._task.state is TaskState.RUNNING:
            raise ParameterError(
                f"{action} is refused while analysis {self._task.task_id} is running.",
                details={"task_id": self._task.task_id, "state": self._task.state.value},
                hint="Call get_analysis until the task finishes, or cancel_analysis to stop it.",
            )

    def stop(self) -> None:
        if self._session is not None:
            self._session.stop()

    def capabilities(self) -> list[CapabilityInfo]:
        return [
            CapabilityInfo(name="read_lens", supported=True),
            CapabilityInfo(name="update_lens", supported=True),
            CapabilityInfo(name="create_lens", supported=True,
                           note="empty session; single-zoom simple spherical lens only"),
            CapabilityInfo(name="edit_lens_structure", supported=True,
                           note="only simple lenses created by this backend instance; "
                                "insert, delete and set stop with verified rollback"),
            CapabilityInfo(name="save_lens_as", supported=True),
            CapabilityInfo(
                name="first_order",
                supported=ANALYSES_VERIFIED,
                note=(
                    "implemented with verified database items and the listing"
                    if ANALYSES_VERIFIED
                    else "implemented, waiting for the real machine verification"
                ),
            ),
            CapabilityInfo(
                name="spot_diagram",
                supported=ANALYSES_VERIFIED,
                note=(
                    "native SPO statistics plus a ray grid traced with RAYTRA"
                    if ANALYSES_VERIFIED
                    else "implemented, waiting for the real machine verification"
                ),
            ),
            CapabilityInfo(
                name="mtf",
                supported=ANALYSES_VERIFIED,
                note=(
                    "MTF_1FLD, diffraction sine wave response"
                    if ANALYSES_VERIFIED
                    else "implemented, waiting for the real machine verification"
                ),
            ),
            CapabilityInfo(
                name="native_plot_export",
                supported=NATIVE_PLOT_VERIFIED,
                note=(
                    "CODE V draws the plot itself and the service converts its "
                    "neutral plot file to PNG: layout, spot, mtf, ray_aberration "
                    "and field_aberration"
                    if NATIVE_PLOT_VERIFIED
                    else "implemented, waiting for the Phase H real machine verification"
                ),
            ),
            CapabilityInfo(
                name="wavefront",
                supported=WAVEFRONT_VERIFIED,
                note="WAV nominal-focus RMS and Strehl at the saved image surface; real acceptance pending"
                if not WAVEFRONT_VERIFIED else "WAV nominal-focus RMS and Strehl at the saved image surface",
            ),
            CapabilityInfo(
                name="afocal_mtf",
                supported=False,
                note="not supported in the first release",
            ),
            CapabilityInfo(
                name="aperture_edit",
                supported=True,
                note=(
                    "single-zoom lenses only: edits the value of the existing system pupil "
                    "type or the radius of one existing centered circular clear aperture; "
                    "type conversion, automatic apertures, complex apertures and obscurations "
                    "remain read only"
                ),
            ),
            CapabilityInfo(
                name="field_set_edit",
                supported=True,
                note=(
                    "update_lens field_set replaces the whole field set (1-10 angle fields, "
                    "weights, vignetting factors) of a single-zoom lens as its own verified "
                    "transaction; create_lens image_solve=pim adds a PIM image solve"
                ),
            ),
        ]

    def get_status(self) -> StatusInfo:
        warnings: list[str] = []
        version: str | None = None
        session_open = False
        if self._closed:
            warnings.append("The session was closed on request; no CODE V session is running.")
        else:
            session = self._session
            if session is None or self._session_is_dead(session):
                # A status call never starts an engine by itself: it reports the
                # last known state and the next lens operation performs the
                # recovery.
                warnings.append(
                    "The CODE V engine is not running; the next lens operation will "
                    "restore the last committed checkpoint."
                )
                if self._lens_state in {LensState.READY, LensState.UPDATING}:
                    self._lens_state = LensState.RECOVERING
                    self._reload_required = self._committed_revision is not None
            else:
                try:
                    version = session.get_version()
                    session_open = True
                except CodeVError as exc:
                    warnings.append(f"{exc.kind.value}: {exc.message}")
                except Exception as exc:  # noqa: BLE001 - status must never raise
                    warnings.append(f"internal: {type(exc).__name__}: {exc}")
        if self._lens_state is LensState.INVALID:
            warnings.append(
                "The lens state cannot be confirmed; every operation that depends on a "
                "trusted lens is refused until the service is restarted."
            )

        return StatusInfo(
            backend=self.name,
            source=self.source,
            service_version=__version__,
            codev_version=version,
            ready=session_open,
            session_open=session_open,
            lens_open=self._lens_ready() and self._lens_open,
            current_lens=self._lens.title if self._lens else None,
            working_directory=str(self.working_directory),
            task=self._task,
            capabilities=self.capabilities(),
            warnings=warnings,
            details={
                "session_valid": self._session_valid,
                "source_path": self._source_path,
                "restore_points": self._restore_seq,
                "com_calls": getattr(self._session, "call_count", None),
                "session_startup_seconds": getattr(self._session, "startup_seconds", None),
                "session_restarts": self.session_restarts,
                "lens_state": self._lens_state.value,
                "lens_id": self._lens_id,
                "committed_revision": self._committed_revision,
                "checkpoint_path": (
                    str(self._committed_lens_path())
                    if self._committed_lens_path() is not None
                    else None
                ),
                "checkpoint_directory": (
                    str(self._lens_directory) if self._lens_directory is not None else None
                ),
                "recovery_count": self.recovery_count,
                "last_recovery": self.last_recovery,
                "last_checkpoint": self.last_checkpoint,
                "checkpoint_format_version": self.checkpoint_store_format_version(),
                "analysis_revision": self._analysis_revision,
                "analysis_lens_id": self._analysis_lens_id,
                "analysis_history_only": bool(
                    self._task is not None and self._task.history_only
                ),
            },
        )

    @staticmethod
    def checkpoint_store_format_version() -> int:
        return checkpoint_format_version()

    # ------------------------------------------------------------------ read

    def create_lens(self, request: CreateLensRequest) -> LensData:
        """Construct a simple spherical lens and publish verified revision 0."""
        commands = create_commands(request)  # reject unsafe input before COM
        self._require_no_running_task("create_lens")
        self._ensure_ready(
            frozenset({LensState.EMPTY}),
            operation="create_lens",
            action="Start a new service to create a lens while one is already open.",
        )
        session = self._require_session()
        lens_id, directory = self.checkpoint_store.create_lens()
        self._lens_id = lens_id
        self._lens_directory = directory
        self._committed_revision = None
        self._last_known_snapshot = None
        self._source_path = None
        self._lens = None
        self._listing = None
        try:
            # LEN prints Invalid lens until the pupil, wavelength and first
            # ordinary surface exist. Judge the complete readback, not that
            # intermediate diagnostic.
            for command in commands:
                call_hook(self.fault_hook, "before_model_command")
                output = session.command_raw(command)
                if session.output_is_truncated(output):
                    raise ComputationError("A model command filled the CODE V text buffer.")
            self._lens_open = True
            lens = self._read_lens()
            snapshot = read_snapshot(session, lens, self._listing)
            differences = created_lens_differences(request, snapshot)
            if differences:
                raise CheckpointVerificationError(
                    "The newly constructed lens did not match the typed request.",
                    details={"differences": differences},
                )
            candidate = directory / "revision-000000.len"
            self._save_lens_file(session, candidate, "new spherical lens")
            restored = self._load_candidate(candidate)
            problems = compare_snapshots(snapshot, restored)
            if problems:
                raise CheckpointVerificationError(
                    "The new lens changed when saved and reopened.",
                    details={"differences": problems[:10]},
                )
            # The reopened lens is already the verified value. Capture it before
            # publishing: a post-commit read must never turn a committed lens
            # into a reported creation failure.
            verified_lens = self._require_lens().model_copy(deep=True)
            call_hook(self.fault_hook, "before_model_publish")
            published = self.checkpoint_store.publish(
                directory, lens_id, 0, candidate, restored, source_path=None,
            )
        except Exception as exc:
            self._lens_state = LensState.INVALID
            self._session_valid = False
            self._lens_open = False
            self._lens = None
            if isinstance(exc, CodeVError):
                raise
            raise ComputationError(
                "The new lens could not be verified or checkpointed.",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        self._committed_revision = 0
        self._last_known_snapshot = restored
        self._modelled_lens_ids.add(lens_id)
        self._lens_state = LensState.READY
        self._lens_open = True
        self._reload_required = True
        self._checkpoint_load_attempted = False
        self.last_checkpoint = {
            "revision": 0,
            "path": str(published.lens_path),
            "source_path": None,
            "sha256": published.lens_sha256,
            "size": published.lens_size,
            "created_at": published.created_at,
        }
        return verified_lens

    def edit_lens_structure(self, request: StructureRequest) -> StructureResult:
        """Apply a bounded structural batch with full readback and rollback."""
        self._require_no_running_task("edit_lens_structure")
        self._ensure_ready(
            WRITABLE_LENS_STATES,
            operation="edit_lens_structure",
            action="Open a trusted service-created spherical lens.",
        )
        if self._lens_id not in self._modelled_lens_ids:
            raise UnsupportedError(
                "Structural editing is limited to simple lenses created by this service."
            )
        session = self._require_session()
        lens = self._require_lens()
        before = read_snapshot(session, lens, self._listing)
        if self._last_known_snapshot is None or compare_snapshots(self._last_known_snapshot, before):
            self._invalidate_lens("the current lens differs from its last verified checkpoint")
            raise SessionInvalidError("The lens changed outside its committed checkpoint.")
        if (
            before.zoom_positions != 1 or before.solves or before.pickups
            or before.aperture_commands or before.zoom_aperture_commands
            or not before.relation_data_complete or not before.aperture_data_complete
            or any(surface.apertures for zoom in before.zooms for surface in zoom.surfaces)
        ):
            raise UnsupportedError(
                "This lens has zoom, solve, pickup or explicit aperture state outside the simple spherical model."
            )
        planned = plan_structure(
            request,
            surface_count=before.surface_count or 0,
            stop_surface=before.stop_surface or 0,
        )
        directory = self._require_lens_directory()
        self._restore_seq += 1
        restore_name = f"rp-{self._restore_seq:04d}"
        transaction_id = self._next_transaction_id()
        restore_path = self.checkpoint_store.restore_point_path(
            directory, restore_name, unique=transaction_id
        )
        self._save_lens_file(session, restore_path, "structural restore point")
        self._lens_state = LensState.UPDATING
        current = before
        try:
            for step in planned:
                for command in step.commands:
                    call_hook(self.fault_hook, "before_structure_command")
                    session.command(command)
                new_lens = self._read_lens()
                following = read_snapshot(session, new_lens, self._listing)
                problems = structure_differences(current, following, step)
                if problems:
                    raise CheckpointVerificationError(
                        "The structural operation changed more than its mapped surfaces.",
                        details={"differences": problems},
                    )
                current = following
            revision = (self._committed_revision or 0) + 1
            candidate = directory / f"revision-{revision:06d}.len"
            call_hook(self.fault_hook, "before_structure_commit")
            self._save_lens_file(session, candidate, f"structural checkpoint revision {revision}")
            outcome = self._publish_checkpoint(
                directory, self._lens_id or "", revision, candidate, current
            )
            if outcome is not PublishOutcome.COMMITTED:
                raise CheckpointPublishError("The structural checkpoint was not committed.")
        except Exception as exc:
            failure = self._rollback_transaction(
                session, restore_path, before, transaction_id=transaction_id,
            )
            reason = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            if isinstance(exc, CodeVError) and exc.details:
                reason += f" ({exc.details})"
            return StructureResult(
                source=self.source,
                steps=[step.result for step in planned],
                lens=self._lens if failure is None else None,
                applied=False,
                restore_point=restore_name,
                rolled_back=failure is None,
                session_valid=self._session_valid,
                warnings=[reason] + ([] if failure is None else [self._describe_failure(failure)]),
            )
        self._lens_state = LensState.READY
        self._lens_open = True
        self._reload_required = True
        return StructureResult(
            source=self.source,
            steps=[step.result for step in planned],
            lens=self._lens.model_copy(deep=True) if self._lens is not None else None,
            applied=True,
            restore_point=restore_name,
        )

    def open_lens(self, path: str) -> LensData:
        self._require_no_running_task("open_lens")
        # Opening a lens is a lens operation like any other: a session whose
        # lens could not be confirmed must not be able to earn a new trusted
        # lens by opening another file, not even through the failure path.
        self._ensure_ready(
            OPEN_LENS_STATES,
            operation="open_lens",
            action="Restart the service to establish a new, trusted session.",
        )
        session = self._require_session()
        source = validate_filespec(path, must_exist=True)
        source_hash = hash_file(source)

        # Remember what is being replaced: a failed open has to leave the session
        # on a lens this service can still vouch for.
        previous_state = self._lens_state
        previous = (
            self._lens_id,
            self._lens_directory,
            self._committed_revision,
            self._last_known_snapshot,
            self._source_path,
            self._lens,
            self._listing,
            self._lens_open,
            previous_state,
        )

        lens_id, directory = self.checkpoint_store.create_lens()
        self._lens_id = lens_id
        self._lens_directory = directory
        self._committed_revision = None
        self._last_known_snapshot = None
        # The commit fact of the previous lens must not survive the switch.
        self._committed_result = None
        self._source_path = str(source)
        self._lens_state = LensState.EMPTY
        self._reload_required = False

        try:
            call_hook(self.fault_hook, "open_lens")
            # The source lens is loaded first: a save before the load would only
            # write the empty system the engine starts with.
            self._load_lens_file(session, source)
            # The working copy already has its final name, so the committed
            # revision is the file that was verified.
            candidate = directory / "revision-000000.len"
            self._save_lens_file(session, candidate, "the working copy of the lens")
            restored = self._load_candidate(candidate)
            published = self.checkpoint_store.publish(
                directory,
                lens_id,
                0,
                candidate,
                restored,
                source_path=str(source),
            )
        except Exception as exc:  # noqa: BLE001 - reported with the previous lens intact
            return self._open_lens_failed(exc, previous, source, source_hash)

        self._committed_revision = 0
        self._last_known_snapshot = restored
        self._lens_state = LensState.READY
        self._lens_open = True
        self._reload_required = True
        self._checkpoint_load_attempted = False
        self.last_checkpoint = {
            "revision": 0,
            "path": str(published.lens_path),
            "source_path": str(source),
            "sha256": published.lens_sha256,
            "size": published.lens_size,
            "source_sha256": source_hash,
            "created_at": published.created_at,
            "com_calls": getattr(session, "call_count", None),
        }
        self.log(
            f"lens {lens_id} opened from {source}; checkpoint revision 0 committed at "
            f"{published.lens_path}"
        )
        self._lens = None
        return self._read_lens(zoom_position=1)

    def _open_lens_failed(
        self,
        exc: BaseException,
        previous: tuple[Any, ...],
        source: Path,
        source_hash: str,
    ) -> LensData:
        """Report a failed open; restore the previous lens or refuse the session."""
        (
            lens_id,
            directory,
            revision,
            snapshot,
            source_path,
            lens,
            listing,
            lens_open,
            state,
        ) = previous
        message = str(getattr(exc, "message", exc))
        if state is LensState.INVALID:
            # The session was already invalid before this call; the failure path
            # must not hand it a new trusted lens.
            self._lens_state = LensState.INVALID
            self._session_valid = False
            raise SessionInvalidError(
                "The session was invalid before the lens was opened; restart the "
                "service before opening another lens.",
                details={
                    "path": str(source),
                    "lens_state": LensState.INVALID.value,
                    "error": f"{type(exc).__name__}: {message}",
                },
            ) from exc
        if revision is None or directory is None:
            # The lens inside the engine may be half initialised and no committed
            # checkpoint can describe it, so the session stops here.
            self._invalidate_lens(
                f"opening {source} failed and no committed lens can describe the engine "
                f"({type(exc).__name__}: {message})"
            )
            raise SessionInvalidError(
                "The lens could not be opened and the engine may hold a partly loaded "
                "lens; restart the service before trying again.",
                details={
                    "path": str(source),
                    "source_sha256": source_hash,
                    "error": f"{type(exc).__name__}: {message}",
                },
            ) from exc

        self._lens_id = lens_id
        self._lens_directory = directory
        self._committed_revision = revision
        self._last_known_snapshot = snapshot
        self._source_path = source_path
        self._lens = lens
        self._listing = listing
        self._lens_open = lens_open
        self._lens_state = LensState.RECOVERING
        self._reload_required = False
        self._checkpoint_load_attempted = False
        try:
            self._recover_lens()
        except Exception:  # noqa: BLE001 - the previous lens is unrecoverable too
            pass
        if self._lens_state is LensState.READY:
            raise SessionInvalidError(
                "The lens could not be opened; the previous lens was restored from its "
                "last committed checkpoint instead.",
                details={
                    "path": str(source),
                    "source_sha256": source_hash,
                    "recovered_revision": self._committed_revision,
                    "error": f"{type(exc).__name__}: {message}",
                },
                hint="The session still holds the previous lens; fix the file and retry.",
            ) from exc
        raise SessionInvalidError(
            "The lens could not be opened and the previous lens could not be restored "
            "either; restart the service before trying again.",
            details={
                "path": str(source),
                "source_sha256": source_hash,
                "error": f"{type(exc).__name__}: {message}",
                "last_recovery": self.last_recovery,
            },
        ) from exc

    def get_lens(self, zoom_position: int | None = None) -> LensData:
        self._require_no_running_task("get_lens")
        self._ensure_safe_for(
            "get_lens", action="Call open_lens to establish a trusted lens first."
        )
        # The lens is re-read instead of returning the data of the session that
        # died, and a rebuilt session is verified against its checkpoint first.
        self._require_lens()
        return self._read_lens(zoom_position=zoom_position)

    def _read_lens(self, zoom_position: int | None = None) -> LensData:
        session = self._require_session()
        surface_count = session.get_surface_count()
        zoom_positions = session.get_zoom_count()
        zoom = zoom_position or 1
        if not 1 <= zoom <= zoom_positions:
            raise ParameterError(
                f"zoom_position {zoom} is outside 1..{zoom_positions}.",
                details={"zoom_positions": zoom_positions},
            )

        listing_text = session.command("lis")
        listing = parse_listing(listing_text)
        if session.get_zoom_count() > 1 and listing.zoom_data_positions != session.get_zoom_count():
            listing.aperture_data_complete = False
            listing.aperture_unknown_lines.append("ZOOM DATA does not cover every zoom position")
        listing_truncated = session.output_is_truncated(listing_text)
        dimension = session.get_dimension()
        units = DIMENSION_TO_UNITS.get(dimension, Units.MM)
        stop_surface = session.get_stop_surface()
        warnings: list[str] = []
        if listing_truncated:
            warnings.append(
                "The surface listing filled the text buffer, so the cross check against "
                "it may be incomplete."
            )

        title = None
        try:
            title = session.evaluate_optional("(TIT)") or None
        except CodeVError:
            title = listing.title
        title = title or listing.title

        surfaces: list[SurfaceData] = []
        for number in range(surface_count):
            radius = session.evaluate_number(f"(RDY S{number} Z{zoom})")
            thickness = session.evaluate_number(f"(THI S{number} Z{zoom})")
            glass_name = ""
            catalog = ""
            try:
                glass_name = session.evaluate_optional(f"(GLA S{number})")
                if glass_name:
                    catalog = session.evaluate_optional(f"(GLA S{number} CAT)")
            except CodeVError as exc:
                warnings.append(f"surface {number}: glass could not be read ({exc.message})")

            radius_infinite = abs(radius) >= INFINITE_RADIUS_THRESHOLD
            thickness_infinite = abs(thickness) >= INFINITE_THICKNESS_THRESHOLD
            glass = f"{glass_name}_{catalog}" if glass_name and catalog else (glass_name or None)

            if number == 0:
                role, label = SurfaceRole.OBJECT, "OBJ"
            elif number == surface_count - 1:
                role, label = SurfaceRole.IMAGE, "IMG"
            else:
                role = SurfaceRole.SURFACE
                label = "STO" if number == stop_surface else str(number)

            semi_aperture = None
            if role is SurfaceRole.SURFACE:
                try:
                    semi_aperture = session.get_max_aperture(number, zoom)
                except CodeVError:
                    semi_aperture = None

            explicit_apertures = [
                SurfaceAperture(
                    kind=entry.kind,
                    shape=entry.shape,
                    label=entry.label,
                    radius=entry.radius,
                    x_semi_aperture=entry.x_semi_aperture,
                    y_semi_aperture=entry.y_semi_aperture,
                    x_decenter=entry.x_decenter,
                    y_decenter=entry.y_decenter,
                    rotation_degrees=entry.rotation_degrees,
                    or_with_previous=entry.or_with_previous,
                    zoom_position=zoom,
                )
                for entry in listing.apertures_at(zoom)
                if entry.surface == number
            ]
            surfaces.append(
                SurfaceData(
                    number=number,
                    role=role,
                    is_stop=number == stop_surface,
                    radius=None if radius_infinite else radius,
                    radius_is_infinite=radius_infinite,
                    thickness=None if thickness_infinite else thickness,
                    thickness_is_infinite=thickness_infinite,
                    glass=glass,
                    semi_aperture=semi_aperture,
                    apertures=explicit_apertures,
                    aperture_data_complete=(
                        listing.aperture_data_complete and not listing_truncated
                    ),
                    label=label,
                )
            )

        warnings.extend(self._cross_check(listing, surfaces))

        fields = []
        for number in range(1, session.get_field_count() + 1):
            fields.append(
                LensField(
                    number=number,
                    x_angle=session.evaluate_number(f"(XAN F{number} Z{zoom})"),
                    y_angle=session.evaluate_number(f"(YAN F{number} Z{zoom})"),
                    weight=session.evaluate_number(f"(WTF F{number} Z{zoom})"),
                    **{
                        name: session.evaluate_number(f"({name.upper()} F{number} Z{zoom})")
                        for name in VIGNETTING_FACTORS
                    },
                )
            )
        if zoom == 1 and not listing_truncated:
            # SPECIFICATION DATA prints zoom position 1 (probe 2026-09-28, orapho21);
            # the other positions are covered by the zoom-qualified items only.
            warnings.extend(
                "vignetting cross check: " + problem
                for problem in vignetting_mismatches(
                    listing.specification, [item.model_dump() for item in fields]
                )
            )

        reference = None
        try:
            reference = int(session.evaluate_number("(REF)"))
        except CodeVError:
            reference = listing.specification.reference_wavelength

        wavelengths = []
        for number in range(1, session.get_wavelength_count() + 1):
            nanometers = session.evaluate_number(f"(WL W{number})")
            weight = session.evaluate_number(f"(WTW W{number})")
            wavelengths.append(
                LensWavelength(
                    number=number,
                    micrometers=nanometers / 1000.0,
                    weight=weight,
                    is_reference=(number == reference),
                )
            )

        if not listing.aperture_data_complete:
            warnings.append(
                "The native aperture block contains unsupported or incomplete definitions: "
                + "; ".join(listing.aperture_unknown_lines[:5])
            )
        aperture = self._read_aperture(listing, session, units, zoom)

        lens = LensData(
            source=self.source,
            path=self._source_path,
            title=title,
            units=units,
            dimension_code=dimension,
            surfaces=surfaces,
            stop_surface=stop_surface,
            fields=fields,
            wavelengths=wavelengths,
            aperture=aperture,
            aperture_usage=("unknown" if listing_truncated else listing.aperture_usage),
            zoom_positions=zoom_positions,
            zoom_position=zoom,
            raw_listing=listing_text,
            warnings=warnings,
        )
        self._lens = lens
        self._listing = listing
        return lens.model_copy(deep=True)

    def _read_aperture(
        self, listing: Listing, session: Any, units: Units, zoom: int
    ) -> ApertureInfo:
        specification = listing.specification
        kind = specification.aperture_kind
        derived_epd = None
        try:
            derived_epd = session.evaluate_number(f"(EPD Z{zoom})")
        except CodeVError:
            pass
        if kind not in {"epd", "fno", "na", "nao"} or specification.aperture_value is None:
            return ApertureInfo(
                kind="unknown",
                value=None,
                units=None,
                zoom_position=zoom,
                definition_source="unknown",
                derived_epd=derived_epd,
                derived_epd_units=units if derived_epd is not None else None,
            )
        try:
            value = session.evaluate_number(f"({kind.upper()} Z{zoom})")
        except CodeVError:
            value = specification.aperture_value
        return ApertureInfo(
            kind=kind,
            value=value,
            units=units if kind == "epd" else None,
            zoom_position=zoom,
            definition_source="listing",
            derived_epd=derived_epd,
            derived_epd_units=units if derived_epd is not None else None,
        )

    @staticmethod
    def _cross_check(listing: Listing, surfaces: list[SurfaceData]) -> list[str]:
        """Compare the database item values with the listing and report drift."""
        warnings: list[str] = []
        rows = listing.surfaces
        if len(rows) != len(surfaces):
            warnings.append(
                f"The listing shows {len(rows)} surface rows but {len(surfaces)} were read; "
                "values are taken from the database items."
            )
        for row, surface in zip(rows, surfaces):
            for label, listing_value, item_value, flag in (
                ("radius", row.radius, surface.radius, surface.radius_is_infinite),
                ("thickness", row.thickness, surface.thickness, surface.thickness_is_infinite),
            ):
                if flag or listing_value is None or item_value is None:
                    continue
                if abs(listing_value - item_value) > max(
                    LISTING_ABSOLUTE_TOLERANCE, abs(item_value) * LISTING_RELATIVE_TOLERANCE
                ):
                    warnings.append(
                        f"surface {surface.number} {label}: listing {listing_value} differs from "
                        f"the database item {item_value}"
                    )
        return warnings

    # ------------------------------------------------------------------ edit

    def _validate_edit(
        self,
        lens: LensData,
        edit: ParameterEdit,
        probe_warnings: list[str] | None = None,
    ) -> str | None:
        """Return a rejection reason, or None when the edit may be attempted."""
        if edit.target == "surface":
            numbers = [surface.number for surface in lens.surfaces]
            if edit.surface not in numbers:
                return (
                    f"Surface {edit.surface} does not exist in this lens; valid surfaces are "
                    f"{min(numbers)}..{max(numbers)}."
                )
            surface = lens.surfaces[numbers.index(edit.surface)]
            if edit.parameter in {"semi_aperture", "clear_aperture"}:
                return (
                    f"{edit.parameter} is an ambiguous legacy name and remains read only; "
                    "use clear_aperture_radius for a verified command-line radius."
                )
            if edit.parameter in {"radius", "glass"} and surface.role is not SurfaceRole.SURFACE:
                return f"{edit.parameter} cannot be set on the {surface.role.value} surface."
            if edit.parameter == "thickness" and surface.role is SurfaceRole.IMAGE:
                return "thickness cannot be set on the image surface."
            solve = self._solve_type(edit, probe_warnings)
            if solve:
                return (
                    f"{edit.parameter} on surface {edit.surface} is controlled by the "
                    f"{solve} solve and cannot be edited."
                )
        elif edit.target == "field":
            numbers = [item.number for item in lens.fields]
            if edit.field not in numbers:
                return f"Field {edit.field} does not exist; valid fields are {min(numbers)}..{max(numbers)}."
        elif edit.target == "wavelength":
            numbers = [item.number for item in lens.wavelengths]
            if edit.wavelength not in numbers:
                return (
                    f"Wavelength {edit.wavelength} does not exist; valid wavelengths are "
                    f"{min(numbers)}..{max(numbers)}."
                )
        elif edit.target == "aperture":
            if lens.zoom_positions != 1:
                return "System aperture editing currently supports single-zoom lenses only."
            if lens.aperture.kind == "unknown" or lens.aperture.definition_source != "listing":
                return "The defining system aperture type is not readable from the native listing."
            if any(not surface.aperture_data_complete for surface in lens.surfaces):
                return (
                    "The native surface aperture state is incomplete or truncated, so a system "
                    "aperture edit cannot be verified safely."
                )
        else:  # pragma: no cover - the model validator rejects other targets
            return f"Unknown edit target {edit.target!r}."

        if edit.target == "surface" and edit.parameter == "clear_aperture_radius":
            if lens.zoom_positions != 1:
                return "Surface clear aperture editing currently supports single-zoom lenses only."
            surface = next(item for item in lens.surfaces if item.number == edit.surface)
            if surface.role is not SurfaceRole.SURFACE:
                return "clear_aperture_radius can only be set on an optical surface."
            if not surface.aperture_data_complete:
                return "The native surface aperture definition is incomplete or unsupported."
            if len(surface.apertures) != 1:
                return (
                    "The surface must have exactly one explicit aperture; defaults, obscurations, "
                    "edges, holes and compound definitions are read only."
                )
            if lens.aperture_usage not in {"user_and_default", "user_only"}:
                return "Surface clear apertures cannot be edited while CA NO/default-only mode ignores user definitions."
            aperture = surface.apertures[0]
            if (
                aperture.kind != "clear"
                or aperture.shape != "circular"
                or aperture.radius is None
                or aperture.x_decenter != 0.0
                or aperture.y_decenter != 0.0
                or aperture.rotation_degrees != 0.0
                or aperture.or_with_previous
            ):
                return (
                    "Only an existing centered circular clear aperture without OR, "
                    "obscuration, edge or hole data can be edited."
                )

        if lens.zoom_positions > 1 and edit.zoom_position is None:
            return "This lens has multiple zoom positions; specify zoom_position for the edit."
        if edit.zoom_position is not None and not 1 <= edit.zoom_position <= lens.zoom_positions:
            return f"zoom_position {edit.zoom_position} is outside 1..{lens.zoom_positions}."

        problem = self._validate_value(edit, lens)
        if problem:
            return problem
        return None

    @staticmethod
    def _validate_value(edit: ParameterEdit, lens: LensData) -> str | None:
        """Return a rejection reason for an invalid value, or None."""
        try:
            if edit.parameter == "glass":
                validate_glass_name(edit.value)
                return None
            if edit.parameter == "radius":
                check_number(edit.value, field_name="radius", minimum=-1e12, maximum=1e12)
            elif edit.parameter == "thickness":
                check_number(edit.value, field_name="thickness", minimum=-1e9, maximum=1e9)
            elif edit.parameter in {"y_angle", "x_angle"}:
                check_number(edit.value, field_name=edit.parameter, minimum=-180.0, maximum=180.0)
            elif edit.parameter in VIGNETTING_FACTORS:
                check_number(edit.value, field_name=edit.parameter, minimum=-VIGNETTING_LIMIT,
                             maximum=VIGNETTING_LIMIT)
            elif edit.parameter == "weight":
                check_number(edit.value, field_name="weight", minimum=0.0, maximum=1e6)
                if edit.target == "wavelength" and float(edit.value) != int(float(edit.value)):
                    return (
                        "CODE V requires an integer wavelength weight (WTW expects integer data)."
                    )
            elif edit.parameter == "micrometers":
                nanometers = (
                    check_number(
                        edit.value, field_name="micrometers", minimum=0.01, maximum=1000.0
                    )
                    * 1000
                )
                if not 10.0 <= nanometers <= 1e6:
                    return "The wavelength must be between 0.01 and 1000 micrometers."
            elif edit.parameter == "is_reference":
                number = int(
                    check_number(edit.value, field_name="is_reference", minimum=1, maximum=1e3)
                )
                if number > len(lens.wavelengths):
                    return f"is_reference must be one of 1..{len(lens.wavelengths)}."
            elif edit.parameter == "clear_aperture_radius":
                check_number(
                    edit.value,
                    field_name="clear_aperture_radius",
                    minimum=1e-12,
                    maximum=1e9,
                )
            elif edit.target == "aperture" and edit.parameter == "value":
                kind = lens.aperture.kind
                maximum = 0.999999999 if kind in {"na", "nao"} else 1e9
                check_number(
                    edit.value,
                    field_name=f"{kind} aperture value",
                    minimum=1e-12,
                    maximum=maximum,
                )
        except ParameterError as exc:
            return exc.message
        return None

    def _solve_type(
        self, edit: ParameterEdit, warnings: list[str] | None = None
    ) -> str | None:
        """Return the solve that controls the parameter, if any.

        A failed probe is not the same as "no solve": the edit is attempted
        anyway because the read-back check catches a silent ignore, but the
        caller is told through warnings that the pre-check was unavailable.
        """
        if edit.target != "surface":
            return None
        if edit.parameter == "thickness":
            item = f"TYP SOL S{edit.surface} THI"
        elif edit.parameter == "radius":
            item = f"TYP SOL S{edit.surface} CUY"
        else:
            return None
        probe_failed = False
        try:
            value = self._session.evaluate(f"({item})").strip()
        except CodeVError as exc:
            probe_failed = True
            value = ""
            self.log(f"solve probe for ({item}) failed: {exc.message}")
        if value and value.upper() not in {"NO", "NONE", " "}:
            return value
        try:
            cux = self._session.evaluate(f"(TYP SOL S{edit.surface} CUX)").strip()
        except CodeVError as exc:
            probe_failed = True
            cux = ""
            self.log(f"solve probe for (TYP SOL S{edit.surface} CUX) failed: {exc.message}")
        if probe_failed and warnings is not None:
            warnings.append(
                f"The solve probe for {edit.parameter} on surface {edit.surface} could not "
                "be read; the edit is attempted anyway and relies on the read-back check "
                "to detect a solve or pickup controlled parameter."
            )
        return cux if cux and cux.upper() not in {"NO", "NONE", " "} else None

    def _zoom_qualifier(self, edit: ParameterEdit, lens: LensData) -> tuple[str, str | None]:
        """Return the zoom qualifier and an optional note about zoom handling."""
        if edit.parameter in NO_ZOOM_QUALIFIER:
            if lens.zoom_positions > 1:
                return "", (
                    f"{edit.parameter} has no per zoom position form in this release; "
                    "the value applies to the whole lens."
                )
            return "", None
        if lens.zoom_positions == 1:
            return "", None
        zoom = edit.zoom_position or 1
        if edit.target == "surface":
            base = SURFACE_ITEM[edit.parameter]
            item = f"{base} S{edit.surface}"
        else:
            item = FIELD_ITEM[edit.parameter] + f" F{edit.field}"
        if self._is_zoomed(item, lens.zoom_positions):
            return f" Z{zoom}", None
        return "", (
            f"{edit.parameter} at this element is not zoomed: all {lens.zoom_positions} zoom "
            "positions share one value, so the change applies to every position."
        )

    def _is_zoomed(self, item: str, zoom_positions: int) -> bool:
        values = []
        for zoom in range(1, zoom_positions + 1):
            values.append(self._session.evaluate(f"({item} Z{zoom})"))
        return len(set(values)) > 1

    def _solve_coupled_changes(
        self,
        reference: LensSnapshot,
        actual: LensSnapshot,
    ) -> list[tuple[str, int, int, str]]:
        """Parameters a solve re-derived after the batch was applied.

        Only parameters that really moved and that CODE V itself reports as
        solve controlled are returned, so a change nobody asked for still fails
        the batch. A probe that cannot be read counts as "no solve" and leaves
        the difference in place.
        """
        candidates: list[tuple[str, int, int, str]] = []
        if reference.zooms:
            for surface in reference.zooms[0].surfaces:
                for parameter in ("thickness", "radius"):
                    candidates.append(("surface", surface.number, 1, parameter))
        coupled: list[tuple[str, int, int, str]] = []
        for entry in solve_coupled_changes(reference, actual, candidates):
            target, selector, _zoom, parameter = entry
            if self._solve_type_for(selector, parameter):
                coupled.append(entry)
        return coupled

    def _solve_type_for(self, surface: int, parameter: str) -> str | None:
        """The solve that controls a surface parameter, if one is readable."""
        items = {
            "thickness": (f"TYP SOL S{surface} THI", f"TYP SOL S{surface} CUX"),
            "radius": (f"TYP SOL S{surface} CUY", f"TYP SOL S{surface} CUX"),
        }.get(parameter)
        if items is None:
            return None
        for item in items:
            try:
                value = str(self._require_session().evaluate(f"({item})")).strip()
            except CodeVError:
                continue
            if value and value.upper() not in {"NO", "NONE"}:
                return value
        return None
    def _plan_edit(self, lens: LensData, edit: ParameterEdit) -> PlannedEdit:
        qualifier, note = self._zoom_qualifier(edit, lens)
        zoom = edit.zoom_position or 1

        if edit.target == "aperture":
            number = check_number(edit.value, field_name="aperture value")
            kind = lens.aperture.kind.upper()
            return PlannedEdit(
                edit=edit,
                command=f"{kind} {format_float(number)}",
                read_item=f"({kind} Z1)",
                kind="number",
                expected=number,
                previous=lens.aperture.value,
                note=None,
            )

        if edit.target == "surface":
            if edit.surface is None:  # pragma: no cover - the model guarantees it
                raise ParameterError("A surface edit needs a surface number.")
            previous = next(
                (self._surface_value(surface, edit.parameter) for surface in lens.surfaces
                 if surface.number == edit.surface),
                None,
            )
            if edit.parameter == "glass":
                value = validate_glass_name(edit.value)
                command = f"GLA S{edit.surface} {value}"
                return PlannedEdit(
                    edit=edit, command=command, read_item=f"(GLA S{edit.surface})",
                    kind="glass", expected=value.split("_")[0].lower(), previous=previous,
                    note=note,
                )
            if edit.parameter == "clear_aperture_radius":
                aperture = next(
                    surface.apertures[0]
                    for surface in lens.surfaces
                    if surface.number == edit.surface
                )
                number = check_number(
                    edit.value, field_name="clear_aperture_radius", minimum=1e-12
                )
                label = f" L'{aperture.label}'" if aperture.label else ""
                return PlannedEdit(
                    edit=edit,
                    command=f"CIR S{edit.surface} CLR{label} {format_float(number)}",
                    read_item="",
                    kind="surface_aperture",
                    expected=number,
                    previous=aperture.radius,
                    note=None,
                )
            number = check_number(edit.value, field_name=edit.parameter)
            command = f"{SURFACE_ITEM[edit.parameter]} S{edit.surface}{qualifier} {format_float(number)}"
            return PlannedEdit(
                edit=edit,
                command=command,
                read_item=f"({SURFACE_ITEM[edit.parameter]} S{edit.surface}{qualifier})",
                kind="number", expected=number, previous=previous, note=note,
            )

        if edit.target == "field":
            if edit.field is None:  # pragma: no cover
                raise ParameterError("A field edit needs a field number.")
            number = check_number(edit.value, field_name=edit.parameter)
            previous = next(
                (self._field_value(field, edit.parameter) for field in lens.fields
                 if field.number == edit.field),
                None,
            )
            command = f"{FIELD_ITEM[edit.parameter]} F{edit.field}{qualifier} {format_float(number)}"
            return PlannedEdit(
                edit=edit,
                command=command,
                read_item=f"({FIELD_ITEM[edit.parameter]} F{edit.field}{qualifier})",
                kind="number", expected=number, previous=previous, note=note,
            )

        if edit.wavelength is None:  # pragma: no cover
            raise ParameterError("A wavelength edit needs a wavelength number.")
        wavelength_number = edit.wavelength
        previous = next(
            (self._wavelength_value(item, edit.parameter) for item in lens.wavelengths
             if item.number == wavelength_number),
            None,
        )
        if edit.parameter == "is_reference":
            reference = int(check_number(edit.value, field_name="is_reference", minimum=1))
            return PlannedEdit(
                edit=edit, command=f"REF {reference}", read_item="(REF)",
                kind="reference", expected=reference, previous=previous, note=note,
            )
        if edit.parameter == "weight":
            weight = int(check_number(edit.value, field_name="weight", minimum=0))
            return PlannedEdit(
                edit=edit, command=f"WTW W{wavelength_number} {weight}",
                read_item=f"(WTW W{wavelength_number})", kind="number",
                expected=float(weight), previous=previous, note=note,
            )
        micrometers = check_number(edit.value, field_name="micrometers", minimum=0.01, maximum=1000.0)
        return PlannedEdit(
            edit=edit, command=f"WL W{wavelength_number} {format_float(micrometers * 1000)}",
            read_item=f"(WL W{wavelength_number})", kind="number",
            expected=micrometers * 1000.0, previous=previous, note=note,
        )

    @staticmethod
    def _surface_value(surface: SurfaceData, parameter: str) -> float | str | None:
        if parameter == "radius":
            return surface.radius
        if parameter == "thickness":
            return surface.thickness
        if parameter == "clear_aperture_radius":
            return surface.apertures[0].radius if len(surface.apertures) == 1 else None
        return surface.glass

    @staticmethod
    def _field_value(field: LensField, parameter: str) -> float | None:
        return {
            "y_angle": field.y_angle,
            "x_angle": field.x_angle,
            "weight": field.weight,
            **{name: getattr(field, name) for name in VIGNETTING_FACTORS},
        }.get(parameter)

    @staticmethod
    def _wavelength_value(item: LensWavelength, parameter: str) -> float | None:
        if parameter == "micrometers":
            return item.micrometers
        if parameter == "weight":
            return item.weight
        return None

    def update_lens(self, request: UpdateRequest) -> UpdateResult:
        effective = [
            edit.model_copy(update={"zoom_position": edit.zoom_position or request.zoom_position})
            for edit in request.edits
        ]
        self._require_no_running_task("update_lens")
        self._ensure_ready(
            WRITABLE_LENS_STATES,
            operation="update_lens",
            action="Call the tool again once the lens state is ready.",
        )
        lens = self._require_lens()
        session = self._require_session()
        if request.field_set is not None:
            return self._replace_field_set(request.field_set)

        probe_warnings: list[str] = []
        reasons = [
            self._validate_edit(lens, edit, probe_warnings) for edit in effective
        ]
        self._restore_seq += 1
        restore_name = f"rp-{self._restore_seq:04d}"

        if any(reasons):
            outcomes = [
                EditOutcome(
                    edit=edit,
                    applied=False,
                    previous_value=self._previous_value(lens, edit),
                    new_value=None,
                    rejected_reason=reason
                    or "Batch rejected: another edit in the same batch was refused.",
                )
                for edit, reason in zip(effective, reasons)
            ]
            return UpdateResult(
                source=self.source,
                outcomes=outcomes,
                restore_point=None,
                rolled_back=True,
                session_valid=self._session_valid,
                warnings=[
                    "No parameter was changed: a batch is applied as a whole so that a partly "
                    "edited lens is never reported as success."
                ],
            )

        plans, warnings = [], list(probe_warnings)
        for edit in effective:
            plan = self._plan_edit(lens, edit)
            if plan.note:
                warnings.append(plan.note)
            plans.append(plan)

        transaction_id = self._next_transaction_id()
        try:
            result = self._run_transaction(
                effective,
                plans,
                warnings,
                restore_name,
                transaction_id=transaction_id,
                zoom_position=request.zoom_position or effective[0].zoom_position,
            )
            if result.session_valid and not self._session_valid:
                # The commit ran into a dead engine: nothing was published and
                # the lens is restored from the last committed checkpoint on the
                # next call, so the result has to say so.
                result.session_valid = False
                result.warnings.append(
                    "The CODE V engine exited while the batch was committed; the batch "
                    "was not published and the lens will be restored from the last "
                    "committed checkpoint on the next call."
                )
            return result
        except Exception as exc:
            committed = self._committed_batch_outcome(transaction_id)
            if committed is not None:
                # The revision was published before the exception, so the only
                # honest report is the committed one: diagnostics and records
                # can fail, the commit cannot be undone by them.
                return committed
            self._session_valid = False
            # Whatever went wrong happened before this batch published its own
            # revision, so the state of the lens is not confirmed while the last
            # committed checkpoint still describes it: the next call restores
            # that revision instead of trusting the lens as it is.
            self._lens = None
            self._listing = None
            self._reload_required = True
            self._lens_state = LensState.RECOVERING
            return UpdateResult(
                source=self.source,
                outcomes=[
                    EditOutcome(
                        edit=plan.edit,
                        applied=False,
                        previous_value=plan.previous,
                        new_value=None,
                        rejected_reason=(
                            f"The batch failed before it finished: {type(exc).__name__}: "
                            f"{getattr(exc, 'message', exc)}"
                        ),
                    )
                    for plan in plans
                ],
                restore_point=restore_name,
                rolled_back=False,
                session_valid=False,
                warnings=warnings
                + [
                    "No further write is accepted in this session; restart the service "
                    "to establish a new trusted lens."
                ],
            )

    def _field_set_refusal(self, lens: LensData) -> str | None:
        if lens.zoom_positions != 1:
            return "Replacing the field set currently supports single-zoom lenses only."
        if self._field_kind() not in {None, "angle"}:
            return f"This lens defines fields as {self._field_kind()}; only angle fields can be replaced."
        return None

    def _replace_field_set(self, spec: FieldSetReplacement) -> UpdateResult:
        """Replace the field set as one verified transaction.

        The same restore point, readback, whole-lens comparison and checkpoint as
        a structural batch: XAN/YAN set the count and the angles, weights and
        factors CODE V does not keep by number are written per field, and the
        result must be the requested set with nothing else moved.
        """
        lens = self._require_lens()
        session = self._require_session()
        previous = [item.model_copy(deep=True) for item in lens.fields]
        reason = self._field_set_refusal(lens)
        target: list[LensField] = []
        if reason is None:
            try:
                target = resolve_field_set(spec, lens.fields)
            except ParameterError as exc:
                reason = exc.message
        if reason is not None:
            return UpdateResult(
                source=self.source,
                outcomes=[],
                field_set=FieldSetOutcome(applied=False, previous_fields=previous,
                                          rejected_reason=reason),
                rolled_back=True,
                session_valid=self._session_valid,
                warnings=["No field was changed: the replacement is refused as a whole."],
            )
        directory = self._require_lens_directory()
        before = read_snapshot(session, lens, self._listing)
        self._restore_seq += 1
        restore_name = f"rp-{self._restore_seq:04d}"
        transaction_id = self._next_transaction_id()
        restore_path = self.checkpoint_store.restore_point_path(
            directory, restore_name, unique=transaction_id
        )
        self._save_lens_file(session, restore_path, "field set restore point")
        self._last_known_snapshot = before
        self._lens_state = LensState.UPDATING
        commands = field_set_commands(target, lens.fields)
        try:
            for command in commands:
                call_hook(self.fault_hook, "before_field_set_command")
                session.command(command)
            new_lens = self._read_lens()
            following = read_snapshot(session, new_lens, self._listing)
            problems = field_set_differences(target, following.zooms[0].fields if following.zooms else [])
            moved = copy.deepcopy(before)
            other = copy.deepcopy(following)
            for snapshot in (moved, other):
                for zoom in snapshot.zooms:
                    zoom.fields = []
            unexpected = compare_snapshots(moved, other)
            if problems or unexpected:
                raise CheckpointVerificationError(
                    "The field set replacement did not produce exactly the requested set.",
                    details={"differences": problems + [summarise(unexpected)] if unexpected else problems},
                )
            revision = (self._committed_revision or 0) + 1
            candidate = directory / f"revision-{revision:06d}.len"
            call_hook(self.fault_hook, "before_field_set_commit")
            self._save_lens_file(session, candidate, f"field set checkpoint revision {revision}")
            outcome = self._publish_checkpoint(
                directory, self._lens_id or "", revision, candidate, following
            )
            if outcome is not PublishOutcome.COMMITTED:
                raise CheckpointPublishError("The field set checkpoint was not committed.")
        except Exception as exc:  # noqa: BLE001 - every failure rolls the batch back
            failure = self._rollback_transaction(
                session, restore_path, before, transaction_id=transaction_id,
            )
            detail = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            if isinstance(exc, CodeVError) and exc.details:
                detail += f" ({exc.details})"
            return UpdateResult(
                source=self.source,
                outcomes=[],
                field_set=FieldSetOutcome(applied=False, previous_fields=previous,
                                          rejected_reason=detail),
                restore_point=restore_name,
                rolled_back=failure is None,
                session_valid=self._session_valid,
                warnings=[detail] + ([self._describe_failure(failure)] if failure is not None else [
                    f"The field set was rolled back to the restore point {restore_name} and the "
                    "previous state was confirmed."]),
            )
        self._lens_state = LensState.READY
        self._lens_open = True
        self._reload_required = True
        return UpdateResult(
            source=self.source,
            outcomes=[],
            field_set=FieldSetOutcome(applied=True, previous_fields=previous, fields=[
                LensField(**item.to_dict()) for item in following.zooms[0].fields]),
            restore_point=restore_name,
            rolled_back=False,
            session_valid=self._session_valid,
            warnings=[f"The field set was replaced by: {'; '.join(commands)}."],
        )

    def _committed_batch_outcome(
        self, transaction_id: str
    ) -> UpdateResult | None:
        """Report this transaction's committed batch, or None when there is none.

        Used as the outer guard of the transaction: whatever fails after the
        pointer was replaced, the batch is reported as applied, never as a
        failed write that the engine and the checkpoint already contradict. The
        commit fact is only used when it belongs to the very transaction that is
        being asked about, so a batch that failed before its own commit can never
        inherit the outcome of an earlier one.
        """
        committed = self._committed_result
        if committed is None or committed.transaction_id != transaction_id:
            return None
        if committed.lens_id is not None and committed.lens_id != self._lens_id:
            return None
        if committed.revision != self._committed_revision:
            return None
        result = committed.result
        outcomes = result.outcomes
        if not any(outcome.applied for outcome in outcomes):
            return None
        warning = result.warnings[-1] if result.warnings else None
        if warning is not None and "bookkeeping after the commit failed" not in warning:
            result.warnings.append(
                "The batch was committed before the failure was noticed; the reported "
                "failure only affects the records of this service, not the lens."
            )
        return result

    def _run_transaction(
        self,
        effective: list[ParameterEdit],
        plans: list[PlannedEdit],
        warnings: list[str],
        restore_name: str,
        *,
        transaction_id: str,
        zoom_position: int,
    ) -> UpdateResult:
        """Apply one batch: restore point, edits, read back, commit or roll back."""
        directory = self._require_lens_directory()
        lens_id = self._lens_id or ""
        session = self._require_session()
        record: dict[str, Any] = {
            "state": "started",
            "started_at": utc_now(),
            "from_revision": self._committed_revision,
            "edits": [
                {
                    "target": edit.target,
                    "selector": edit.selector,
                    "parameter": edit.parameter,
                    "value": edit.value,
                    "zoom_position": edit.zoom_position,
                }
                for edit in effective
            ],
        }
        self._write_transaction_record(directory, transaction_id, record)

        # The restore point is written before anything is edited; if it cannot be
        # written, no edit is attempted at all.
        try:
            restore_path = self.checkpoint_store.restore_point_path(directory, restore_name)
            if restore_path.exists():
                restore_path = self.checkpoint_store.restore_point_path(
                    directory, restore_name, unique=transaction_id
                )
            self._save_lens_file(session, restore_path, f"restore point {restore_name}")
        except Exception as exc:  # noqa: BLE001 - the batch is refused, nothing ran
            self._session_valid = False
            self._lens_state = LensState.INVALID
            detail = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            return UpdateResult(
                source=self.source,
                outcomes=[
                    EditOutcome(
                        edit=plan.edit,
                        applied=False,
                        previous_value=plan.previous,
                        new_value=None,
                        rejected_reason=(
                            "No parameter was changed: the restore point for this batch "
                            f"could not be written ({detail})."
                        ),
                    )
                    for plan in plans
                ],
                restore_point=restore_name,
                rolled_back=False,
                session_valid=False,
                warnings=warnings
                + [
                    "The session is marked invalid because the restore point could not be "
                    "written; no write is accepted until the service is restarted."
                ],
            )

        self._lens_state = LensState.UPDATING
        self._reload_required = False
        record.update({"state": "editing", "restore_point": str(restore_path)})
        self._write_transaction_record(directory, transaction_id, record)
        try:
            pre_snapshot = read_snapshot(session, self._require_lens(), self._listing)
        except Exception as exc:  # noqa: BLE001 - a refused batch, nothing has run
            self._lens_state = LensState.INVALID
            self._session_valid = False
            detail = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            return UpdateResult(
                source=self.source,
                outcomes=[
                    EditOutcome(
                        edit=plan.edit,
                        applied=False,
                        previous_value=plan.previous,
                        new_value=None,
                        rejected_reason=(
                            "No parameter was changed: the lens state could not be read "
                            f"before editing ({detail})."
                        ),
                    )
                    for plan in plans
                ],
                restore_point=restore_name,
                rolled_back=True,
                session_valid=False,
                warnings=warnings,
            )
        self._last_known_snapshot = pre_snapshot
        record["before_sha256"] = self._snapshot_checksum(pre_snapshot)
        self._write_transaction_record(directory, transaction_id, record)

        outcomes: list[EditOutcome] = []
        for plan in plans:
            try:
                call_hook(self.fault_hook, "before_edit_command")
                session.command(plan.command)
            except CodeVError as exc:
                return self._failed_batch(
                    outcomes,
                    plan,
                    reason=str(exc.message),
                    warnings=warnings,
                    raw_output=exc.raw_output,
                    session=session,
                    restore_path=restore_path,
                    pre_snapshot=pre_snapshot,
                    directory=directory,
                    transaction_id=transaction_id,
                    record=record,
                    restore_name=restore_name,
                )
            try:
                confirmed, actual = self._confirm(plan, session)
            except Exception as exc:  # noqa: BLE001 - a failed read back is a failed edit
                # The command was already accepted by CODE V, so a read back
                # that raises is not a refusal: the batch has to be rolled back
                # like any other unconfirmed edit, or the change stays behind.
                return self._failed_batch(
                    outcomes,
                    plan,
                    reason=(
                        f"The value of {self._describe(plan)} could not be read back "
                        f"({type(exc).__name__}: {getattr(exc, 'message', exc)})."
                    ),
                    warnings=warnings,
                    raw_output=getattr(exc, "raw_output", None),
                    session=session,
                    restore_path=restore_path,
                    pre_snapshot=pre_snapshot,
                    directory=directory,
                    transaction_id=transaction_id,
                    record=record,
                    restore_name=restore_name,
                )
            if not confirmed:
                reason = (
                    f"CODE V accepted the command but the value did not change to "
                    f"{self._describe(plan)} (read back {actual!r}). The parameter may be "
                    "controlled by a solve, a pickup or a zoom setting that this release "
                    "does not modify."
                )
                return self._failed_batch(
                    outcomes,
                    plan,
                    reason=reason,
                    warnings=warnings,
                    session=session,
                    restore_path=restore_path,
                    pre_snapshot=pre_snapshot,
                    directory=directory,
                    transaction_id=transaction_id,
                    record=record,
                    restore_name=restore_name,
                )
            outcomes.append(
                EditOutcome(
                    edit=plan.edit,
                    applied=True,
                    previous_value=plan.previous,
                    new_value=actual,
                )
            )

        # The commands were accepted one by one; the batch is only applied when
        # the whole final state is the requested one and nothing else moved.
        z_target = zoom_position or 1
        try:
            final_snapshot = read_snapshot(session, self._require_lens(), self._listing)
        except Exception as exc:  # noqa: BLE001 - an unreadable state is a failed batch
            return self._failed_batch(
                outcomes,
                None,
                reason=(
                    "The lens state could not be read after editing "
                    f"({type(exc).__name__}: {getattr(exc, 'message', exc)})."
                ),
                warnings=warnings,
                session=session,
                restore_path=restore_path,
                pre_snapshot=pre_snapshot,
                directory=directory,
                transaction_id=transaction_id,
                record=record,
                restore_name=restore_name,
            )
        expectations, superseded = collapse_expectations(
            [(plan.edit, plan.edit.zoom_position or 1) for plan in plans],
            final_snapshot,
        )
        if superseded:
            warnings.append(
                f"The batch edits the same parameter more than once "
                f"({superseded} earlier assignment(s)); CODE V applies them in order, so "
                "only the last value of each parameter is required to hold."
            )
        missing = check_expected_values(final_snapshot, expectations)
        if missing:
            return self._failed_batch(
                outcomes,
                None,
                reason=(
                    "The requested values were not all present after the batch: "
                    f"{summarise(missing)}"
                ),
                warnings=warnings,
                session=session,
                restore_path=restore_path,
                pre_snapshot=pre_snapshot,
                directory=directory,
                transaction_id=transaction_id,
                record=record,
                restore_name=restore_name,
            )
        allowed = with_radius_flags(expand_allowed_changes(
            final_snapshot,
            [edit_key(plan.edit, plan.edit.zoom_position or 1) for plan in plans],
        ))
        coupled = self._solve_coupled_changes(pre_snapshot, final_snapshot)
        if coupled:
            warnings.append(
                "CODE V re-derived the solved parameters "
                f"{describe_changes(pre_snapshot, final_snapshot, coupled)}; those values "
                "follow their solve, not this batch."
            )
        unexpected = compare_snapshots(
            pre_snapshot,
            final_snapshot,
            allowed_changes=allowed + coupled,
            expected_values=change_expectations(
                final_snapshot,
                allowed + coupled + expectation_keys(final_snapshot, allowed + coupled),
            ),
        )
        if unexpected:
            return self._failed_batch(
                outcomes,
                None,
                reason=(
                    "The batch changed more than the requested parameters: "
                    f"{summarise(unexpected)}"
                ),
                warnings=warnings,
                session=session,
                restore_path=restore_path,
                pre_snapshot=pre_snapshot,
                directory=directory,
                transaction_id=transaction_id,
                record=record,
                restore_name=restore_name,
            )

        revision = (self._committed_revision or 0) + 1
        candidate = directory / f"revision-{revision:06d}.len"
        try:
            call_hook(self.fault_hook, TAKEDOWN_BEFORE_COMMIT)
            # One file per revision and no retry: a save that failed leaves the
            # previous pointer in place and moves no history file.
            self._save_lens_file(session, candidate, f"checkpoint revision {revision}")
            selected = revision
            call_hook(self.fault_hook, "after_candidate_saved")
            outcome = self._publish_checkpoint(
                # The candidate has to restore to the state the batch left behind.
                directory, lens_id, selected, candidate, final_snapshot
            )
        except Exception as exc:  # noqa: BLE001 - reported through the rollback path
            if self._engine_is_gone(self._session):
                # The engine died while the checkpoint was being written, which
                # releases the session the same way any engine loss does.
                self._note_engine_lost("the engine exited while the checkpoint was written")
            return self._failed_batch(
                outcomes,
                None,
                reason=(
                    f"The new checkpoint could not be written ({type(exc).__name__}: "
                    f"{getattr(exc, 'message', exc)})."
                ),
                warnings=warnings,
                session=session if not self._engine_is_gone(self._session) else None,
                restore_path=restore_path,
                pre_snapshot=pre_snapshot,
                directory=directory,
                transaction_id=transaction_id,
                record=record,
                restore_name=restore_name,
            )
        if outcome is not PublishOutcome.COMMITTED:
            if self._engine_is_gone(self._session):
                # A checkpoint that failed because the engine died releases the
                # session the same way any engine loss does.
                self._note_engine_lost("the engine exited while the checkpoint was written")
            return self._failed_batch(
                outcomes,
                None,
                reason="The new checkpoint could not be committed.",
                warnings=warnings,
                session=session if not self._engine_is_gone(self._session) else None,
                restore_path=restore_path,
                pre_snapshot=pre_snapshot,
                directory=directory,
                transaction_id=transaction_id,
                record=record,
                restore_name=restore_name,
            )

        # The revision is committed: the pointer and the in memory revision
        # already agree, so nothing below may report the batch as unapplied.
        committed_outcomes = [
            EditOutcome(
                edit=outcome.edit,
                applied=True,
                previous_value=outcome.previous_value,
                new_value=outcome.new_value,
            )
            for outcome in outcomes
        ]
        # The commit fact of this transaction is established at the atomic
        # boundary, before any diagnostics run.
        self._committed_result = CommittedBatch(
            transaction_id=transaction_id,
            lens_id=self._lens_id,
            revision=selected,
            result=UpdateResult(
                source=self.source,
                outcomes=list(committed_outcomes),
                restore_point=restore_name,
                rolled_back=False,
                session_valid=True,
                warnings=list(warnings),
            ),
        )
        self._committed_revision = selected
        self._last_known_snapshot = final_snapshot
        self._lens_state = LensState.READY
        # The committed revision is what a rebuilt engine has to be restored
        # from, so the checkpoint stays the source of truth for this lens.
        self._reload_required = True
        self._lens = None
        follow_up: str | None = None
        try:
            self._read_lens(zoom_position=z_target)
        except Exception as exc:  # noqa: BLE001 - the batch itself is committed
            # The commit stands; the failed read only means the lens has to be
            # restored from the committed checkpoint before it is read again.
            follow_up = (
                "The batch is committed as revision "
                f"{selected}, but the lens could not be read afterwards "
                f"({type(exc).__name__}: {getattr(exc, 'message', exc)}). The next "
                "operation restores the committed revision before reading it."
            )
            warnings.append(follow_up)
            self._lens = None
            self._listing = None
            if self._committed_revision is not None:
                self._lens_state = LensState.RECOVERING
                self._reload_required = True
            else:  # pragma: no cover - the revision was just committed
                self._invalidate_lens(follow_up)
        return self._finalize_committed_batch(
            committed_outcomes,
            warnings,
            follow_up,
            directory=directory,
            transaction_id=transaction_id,
            record=record,
            candidate=candidate,
            selected=selected,
            restore_name=restore_name,
        )

    def _finalize_committed_batch(
        self,
        outcomes: list[EditOutcome],
        warnings: list[str],
        follow_up: str | None,
        *,
        directory: Path,
        transaction_id: str,
        record: dict[str, Any],
        candidate: Path,
        selected: int,
        restore_name: str,
    ) -> UpdateResult:
        """Report a batch whose revision is already committed.

        Only the transaction record is left to write, and a disk or log failure
        there must not turn a committed revision into a failed batch. The
        committed outcome is captured first, so even an unexpected failure can
        only add a warning.
        """
        result = UpdateResult(
            source=self.source,
            outcomes=list(outcomes),
            restore_point=restore_name,
            rolled_back=False,
            session_valid=self._session_valid,
            warnings=list(warnings),
        )
        self._committed_result = CommittedBatch(
            transaction_id=transaction_id,
            lens_id=self._lens_id,
            revision=selected,
            result=result,
        )
        record.update(
            {
                "state": "committed",
                "finished_at": utc_now(),
                "to_revision": selected,
                "checkpoint": str(candidate),
                "follow_up_read_error": follow_up,
            }
        )
        try:
            written = self._write_transaction_record(directory, transaction_id, record)
        except Exception:  # noqa: BLE001 - the record is diagnostics, the commit stands
            written = False
        if not written:
            message = (
                "The batch is committed as revision "
                f"{selected}, but the record of the committed revision could not be "
                "written; the disk or the log sink refused it."
            )
            result.warnings.append(message)
        return result

    @staticmethod
    def _snapshot_checksum(snapshot: LensSnapshot) -> str:
        import hashlib

        payload = json.dumps(snapshot.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _failed_batch(
        self,
        outcomes: list[EditOutcome],
        plan: PlannedEdit | None,
        *,
        reason: str,
        warnings: list[str],
        session: Any,
        restore_path: Path,
        pre_snapshot: LensSnapshot,
        directory: Path,
        transaction_id: str,
        record: dict[str, Any],
        restore_name: str,
        raw_output: str | None = None,
    ) -> UpdateResult:
        """Report a batch that failed, after putting the lens back if possible."""
        failure = self._rollback_transaction(
            session or self._session,
            restore_path,
            pre_snapshot,
            transaction_id=transaction_id,
        )
        if failure is not None:
            record.update(
                {
                    "state": "rollback_failed",
                    "finished_at": utc_now(),
                    "failure": failure["message"],
                    "detail": failure["detail"],
                }
            )
            self._write_transaction_record(directory, transaction_id, record)
            rolled_back = False
            failure_text = self._describe_failure(failure)
        else:
            record.update({"state": "rolled_back", "finished_at": utc_now()})
            self._write_transaction_record(directory, transaction_id, record)
            rolled_back = True
            failure_text = (
                f"The batch was rolled back to the restore point {restore_name} and the "
                "previous values were confirmed."
            )

        # Nothing in the batch counts as applied once the lens is back on the
        # restore point; the caller must not see a partial success.
        reported = [
            EditOutcome(
                edit=item.edit,
                applied=False,
                previous_value=item.previous_value,
                new_value=None,
                rejected_reason=(
                    item.rejected_reason
                    or ("Not applied: the batch was rolled back." if rolled_back else reason)
                ),
            )
            for item in outcomes
        ]
        if plan is not None:
            reported.append(
                EditOutcome(
                    edit=plan.edit,
                    applied=False,
                    previous_value=plan.previous,
                    new_value=None,
                    rejected_reason=reason,
                )
            )

        result_warnings = list(warnings)
        result_warnings.append(reason)
        if failure_text:
            result_warnings.append(failure_text)
        return UpdateResult(
            source=self.source,
            outcomes=reported,
            restore_point=restore_name,
            rolled_back=rolled_back,
            session_valid=self._session_valid,
            raw_output=raw_output,
            warnings=result_warnings,
        )

    def _rollback_transaction(
        self,
        session: Any,
        restore_path: Path,
        pre_snapshot: LensSnapshot,
        *,
        transaction_id: str,
    ) -> dict[str, Any] | None:
        """Put the lens back and prove it, or mark the session invalid.

        The confirmation is a full comparison against the state that was read
        before the batch started, so a restore that silently returns a third,
        wrong state is caught instead of being reported as a rollback.
        """
        if session is None or self._engine_is_gone(session):
            self._lens = None
            self._listing = None
            # The batch is not replayed and the lens is not confirmed either; the
            # committed checkpoint is the only thing a rebuilt engine may be
            # restored from, which is what the recovery path does next.
            self._session_valid = False
            if self._committed_revision is not None:
                self._lens_state = LensState.RECOVERING
                self._reload_required = True
                self._log_recovery(
                    reason="the engine exited before the batch could be rolled back",
                    revision=self._committed_revision,
                    result="pending",
                    detail="the last committed checkpoint will be restored next",
                    failed_operation="rollback",
                )
            else:
                self._invalidate_lens(
                    "the engine exited before the batch could be rolled back; the state "
                    "of the lens cannot be confirmed"
                )
            return {
                "message": "The batch could not be rolled back because the engine exited.",
                "detail": "No checkpoint was published, so the last committed revision still stands.",
            }
        self._lens_state = LensState.RECOVERING
        try:
            self._restore_checkpoint(
                Checkpoint(
                    lens_id=self._lens_id or "",
                    revision=self._committed_revision if self._committed_revision is not None else -1,
                    lens_path=Path(restore_path),
                    metadata_path=Path(restore_path).with_suffix(".json"),
                    snapshot=pre_snapshot,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported as an unconfirmed rollback
            detail = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            self._lens = None
            self._listing = None
            self._invalidate_lens(f"the restore point could not be restored ({detail})")
            return {
                "message": "The recovery file could not be restored.",
                "detail": detail,
            }
        try:
            session_now = self._require_session()
            snapshot = read_snapshot(session_now, self._require_lens(), self._listing)
        except Exception as exc:  # noqa: BLE001 - an unreadable state is unconfirmed
            detail = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
            self._lens = None
            self._listing = None
            self._invalidate_lens(f"the rolled back lens state could not be read ({detail})")
            return {
                "message": "The rolled back lens state could not be read.",
                "detail": detail,
            }
        problems = compare_snapshots(pre_snapshot, snapshot)
        if problems:
            detail = summarise(problems)
            self._invalidate_lens(
                f"the restore point was loaded but the previous values were not "
                f"confirmed ({detail})"
            )
            return {
                "message": "The restore point was loaded, but the previous values were not confirmed.",
                "detail": detail,
            }
        self._last_known_snapshot = snapshot
        self._lens_state = LensState.READY
        self._checkpoint_load_attempted = False
        return None

    def _previous_value(self, lens: LensData, edit: ParameterEdit) -> float | str | None:
        try:
            if edit.target == "aperture":
                return lens.aperture.value
            if edit.target == "surface":
                surface = next(s for s in lens.surfaces if s.number == edit.surface)
                return self._surface_value(surface, edit.parameter)
            if edit.target == "field":
                field = next(f for f in lens.fields if f.number == edit.field)
                return self._field_value(field, edit.parameter)
            item = next(w for w in lens.wavelengths if w.number == edit.wavelength)
            return self._wavelength_value(item, edit.parameter)
        except StopIteration:
            return None

    def _confirm(self, plan: PlannedEdit, session: Any) -> tuple[bool, Any]:
        if plan.kind == "surface_aperture":
            listing_text = session.command("lis")
            listing = parse_listing(listing_text)
            self._listing = listing
            matches = [
                entry
                for entry in listing.apertures
                if entry.surface == plan.edit.surface
                and entry.kind == "clear"
                and entry.shape == "circular"
                and entry.label
                == next(
                    (
                        aperture.label
                        for surface in self._require_lens().surfaces
                        if surface.number == plan.edit.surface
                        for aperture in surface.apertures[:1]
                    ),
                    None,
                )
            ]
            actual = matches[0].radius if len(matches) == 1 else None
            if actual is None:
                return False, actual
            expected = float(plan.expected)
            return (
                abs(actual - expected)
                <= max(ABSOLUTE_TOLERANCE, abs(expected) * LISTING_RELATIVE_TOLERANCE),
                actual,
            )
        value = session.evaluate(plan.read_item)
        if plan.kind == "glass":
            base = value.split("_")[0].strip().lower()
            return base == plan.expected, value
        if plan.kind == "reference":
            try:
                return int(float(value)) == int(plan.expected), value
            except (TypeError, ValueError):
                return False, value
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False, value
        expected = float(plan.expected)
        return abs(number - expected) <= max(ABSOLUTE_TOLERANCE, abs(expected) * RELATIVE_TOLERANCE), number

    @staticmethod
    def _describe(plan: PlannedEdit) -> str:
        edit = plan.edit
        if edit.target == "surface":
            return f"surface {edit.surface} {edit.parameter}"
        if edit.target == "field":
            return f"field {edit.field} {edit.parameter}"
        if edit.target == "aperture":
            return f"system aperture {edit.parameter}"
        return f"wavelength {edit.wavelength} {edit.parameter}"

    # ------------------------------------------------------------------ save

    def save_lens_as(self, path: str) -> SaveResult:
        self._ensure_safe_for(
            "save_lens_as", action="Open a trusted lens before saving it."
        )
        self._require_no_running_task("save_lens_as")
        lens = self._require_lens()
        session = self._require_session()
        target = validate_filespec(path, must_not_exist=True)
        if lens.path and target.resolve() == Path(lens.path).resolve():
            raise ParameterError(
                "Refusing to overwrite the source lens file.",
                details={"path": str(target)},
            )
        output = session.command(f"sav {command_filespec(target)}")
        if not target.exists():
            raise ComputationError(
                "CODE V reported no error but the file was not written.",
                details={"path": str(target)},
                raw_output=output,
            )
        return SaveResult(
            source=self.source,
            path=str(target),
            bytes_written=target.stat().st_size,
            overwritten=False,
            raw_output=output,
        )

    # -------------------------------------------------------------- analyses

    def run_analysis(self, request: AnalysisRequest) -> TaskInfo:
        self._require_no_running_task("run_analysis")
        self._ensure_safe_for(
            "run_analysis", action="Open a trusted lens before running an analysis."
        )
        lens = self._require_lens()
        self._require_session()

        settings = self._analysis_settings(lens, request)
        self._analysis_seq += 1
        task = TaskInfo(
            task_id=f"codev-{self._analysis_seq:04d}",
            kind=request.kind,
            state=TaskState.QUEUED,
            source=self.source,
            created_at=_utc_now(),
            settings=settings,
            raw_output=None,
        )
        self._task = task
        self._analysis_payload = {}
        self._analysis_revision = self._committed_revision
        self._analysis_lens_id = self._lens_id

        try:
            if request.kind is AnalysisKind.SPOT_DIAGRAM:
                # The spot diagram is produced by the SPO option, which is long
                # enough to run asynchronously; get_analysis polls it.
                self._start_spot(settings)
                task.state = TaskState.RUNNING
                task.started_at = _utc_now()
                task.progress = "SPO submitted; poll get_analysis until it finishes."
                return task.model_copy(deep=True)
            if request.kind is AnalysisKind.FIRST_ORDER:
                task.state = TaskState.RUNNING
                task.started_at = _utc_now()
                result, raw = self._analyse_first_order(lens, settings)
                self._analysis_payload["first_order"] = result
            elif request.kind is AnalysisKind.MTF:
                task.state = TaskState.RUNNING
                task.started_at = _utc_now()
                result, raw = self._analyse_mtf(lens, settings)
                self._analysis_payload["mtf"] = result
            elif request.kind is AnalysisKind.WAVEFRONT:
                task.state = TaskState.RUNNING
                task.started_at = _utc_now()
                result, raw = self._analyse_wavefront(lens, settings)
                self._analysis_payload["wavefront"] = result
            elif request.kind is AnalysisKind.NATIVE_PLOT:
                # CODE V draws the plot itself, into a plot file the service
                # chose; get_analysis polls the option and exports the file.
                self._start_native_plot(task, settings)
                task.state = TaskState.RUNNING
                task.started_at = _utc_now()
                task.progress = (
                    "Native plot submitted; poll get_analysis until it finishes."
                )
                return task.model_copy(deep=True)
            else:  # pragma: no cover - the model restricts the values
                raise UnsupportedError(f"Analysis kind {request.kind} is not supported.")
        except CodeVError as exc:
            return self._fail_task(exc)

        task.state = TaskState.SUCCEEDED
        task.finished_at = _utc_now()
        task.raw_output = raw
        return task.model_copy(deep=True)

    def _fail_task(self, exc: CodeVError) -> TaskInfo:
        """Record a failure on the current task and return a copy of it."""
        assert self._task is not None
        self._task.state = TaskState.FAILED
        self._task.finished_at = _utc_now()
        self._task.error = exc.to_info()
        self._task.raw_output = exc.raw_output
        self._pending_spot = None
        self._pending_native_plot = None
        return self._task.model_copy(deep=True)

    def get_analysis(self) -> AnalysisSnapshot:
        self._require_session()
        if self._task is not None and self._task.state is TaskState.RUNNING:
            if self._pending_spot is not None:
                try:
                    finished = self._advance_spot()
                except CodeVError as exc:
                    self._fail_task(exc)
                else:
                    if finished:
                        self._task.state = TaskState.SUCCEEDED
                        self._task.finished_at = _utc_now()
                        self._task.progress = "SPO finished."
            elif self._pending_native_plot is not None:
                pending = self._pending_native_plot
                try:
                    finished = self._advance_native_plot()
                except CodeVError as exc:
                    # The poll itself failed, so the option may still be running
                    # and its plot file is still open: the graphics state is
                    # recovered before the task is failed.
                    self._abandon_native_plot(pending, exc.message)
                    self._fail_task(exc)
                else:
                    if finished:
                        self._task.state = TaskState.SUCCEEDED
                        self._task.finished_at = _utc_now()
                        self._task.progress = "Native plot exported."
        snapshot = AnalysisSnapshot(source=self.source)
        if self._task is not None:
            snapshot.task = self._task.model_copy(deep=True)
        if self._task is not None and self._task.state is TaskState.SUCCEEDED:
            same_lens = self._analysis_lens_id is not None and self._analysis_lens_id == self._lens_id
            same_revision = self._analysis_revision == self._committed_revision
            if not self._lens_ready() or not same_lens or not same_revision:
                # The lens changed (or was restored to another revision) after
                # this result was computed, so the numbers no longer describe
                # the lens that is open now. They stay readable as history, but
                # they are marked as such instead of claiming fresh CODE V
                # results for the open lens.
                history_note = (
                    "These results were computed on lens "
                    f"{self._analysis_lens_id} revision {self._analysis_revision}; the "
                    f"open lens is {self._lens_id} revision {self._committed_revision}, "
                    "so they do not belong to the current lens and are reported as "
                    "history only."
                )
                if history_note not in self._task.warnings:
                    self._task.warnings.append(history_note)
                # The results are real CODE V results, so the source stays
                # codev; the task and the snapshot say that they are history
                # instead of pretending they describe the open lens.
                self._task.history_only = True
                snapshot.first_order = self._analysis_payload.get("first_order")
                snapshot.spot_diagram = self._analysis_payload.get("spot_diagram")
                snapshot.mtf = self._analysis_payload.get("mtf")
                snapshot.native_plot = self._analysis_payload.get("native_plot")
                snapshot.wavefront = self._analysis_payload.get("wavefront")
                snapshot.task = self._task.model_copy(deep=True)
            else:
                snapshot.first_order = self._analysis_payload.get("first_order")
                snapshot.spot_diagram = self._analysis_payload.get("spot_diagram")
                snapshot.mtf = self._analysis_payload.get("mtf")
                snapshot.native_plot = self._analysis_payload.get("native_plot")
                snapshot.wavefront = self._analysis_payload.get("wavefront")
        return snapshot

    def cancel_analysis(self) -> TaskInfo | None:
        self._require_session()
        if self._task is None:
            return None
        if self._task.state is not TaskState.RUNNING:
            # Nothing is running: report the final state unchanged so the caller
            # can tell a request from an actual stop.
            self._task.progress = "No analysis was running; the final state is reported."
            return self._task.model_copy(deep=True)

        session = self._require_session()
        cancelled = False
        try:
            session.stop_command()
            cancelled = self._wait_for_stop(session)
        except CodeVError as exc:
            self._task.progress = f"StopCommand failed: {exc.message}"
        if cancelled:
            self._pending_spot = None
            pending_native = self._pending_native_plot
            self._pending_native_plot = None
            if pending_native is not None:
                # The aborted option already opened a plot file, and CODE V only
                # closes it when the graphics destination changes, so the
                # session is left with its graphics output restored.
                try:
                    self._release_native_graphics(session, pending_native["plot_type"])
                except CodeVError as exc:
                    self._task.warnings.append(
                        "The plot file of the aborted native plot could not be "
                        f"closed: {exc.message}"
                    )
            self._task.state = TaskState.CANCELLED
            self._task.finished_at = _utc_now()
            self._task.progress = "StopCommand was sent and CODE V confirmed the stop."
        else:
            # The stop could not be confirmed, so the calculation may still be
            # running: keep the task running, mark the session invalid and let
            # the caller decide (CancelResult reports still_running).
            self._task.progress = (
                "StopCommand was sent but CODE V did not confirm the stop; the session is "
                "marked invalid."
            )
            self._invalidate_lens(
                f"the stop of analysis {self._task.task_id} was not confirmed by CODE V"
            )
        return self._task.model_copy(deep=True)

    def _wait_for_stop(self, session: Any) -> bool:
        """Wait until CODE V reports that nothing is executing."""
        deadline = time.monotonic() + self.cancel_confirm_seconds
        while time.monotonic() < deadline:
            if not session.is_executing_command():
                return True
            if session.wait(2) == 0:
                return not session.is_executing_command()
        return not session.is_executing_command()
    # --------------------------------------------------------- analysis setup

    def _field_kind(self) -> str | None:
        if self._listing is None:
            return None
        return self._listing.specification.field_kind

    def _analysis_settings(self, lens: LensData, request: AnalysisRequest) -> AnalysisSettings:
        options = request.options
        zoom = options.zoom_position or lens.zoom_position or 1
        if not 1 <= zoom <= lens.zoom_positions:
            raise ParameterError(
                f"zoom_position {zoom} is outside 1..{lens.zoom_positions}."
            )

        field_numbers = options.field_numbers or [field.number for field in lens.fields]
        valid_fields = {field.number for field in lens.fields}
        for number in field_numbers:
            if number not in valid_fields:
                raise ParameterError(
                    f"Field {number} does not exist.",
                    details={"valid_fields": sorted(valid_fields)},
                )

        wavelength_numbers = options.wavelength_numbers or [
            wavelength.number for wavelength in lens.wavelengths
        ]
        valid_wavelengths = {wavelength.number for wavelength in lens.wavelengths}
        for number in wavelength_numbers:
            if number not in valid_wavelengths:
                raise ParameterError(
                    f"Wavelength {number} does not exist.",
                    details={"valid_wavelengths": sorted(valid_wavelengths)},
                )

        if request.kind is AnalysisKind.MTF:
            if not options.frequencies:
                raise ParameterError(
                    "MTF analysis needs an explicit frequency grid.",
                    hint="Pass options.frequencies, for example [10, 20, 40, 80] cycles/mm.",
                )
            if options.mtf_type is MtfType.GEOMETRIC:
                raise UnsupportedError(
                    "The first release supports the diffraction sine wave MTF only.",
                    details={"requested": options.mtf_type.value},
                )

        if request.kind is AnalysisKind.SPOT_DIAGRAM:
            if options.field_numbers and len(field_numbers) != 1:
                raise ParameterError(
                    "A spot diagram task covers exactly one field in this release.",
                    details={"field_numbers": field_numbers},
                    hint="Run the task once per field.",
                )

        check_native_plot_options(lens, options, request.kind)
        check_wavefront_options(lens, options, request.kind)

        notes = [
            "The service never refocuses: the lens is analysed as stored.",
        ]
        if request.kind is AnalysisKind.SPOT_DIAGRAM and not options.field_numbers:
            notes.append(
                f"No field was requested, so field {field_numbers[0]} was used; pass "
                "options.field_numbers to choose another field."
            )
        if request.kind is AnalysisKind.MTF:
            notes.append(
                "tangential is azimuth 0 degrees, sagittal is azimuth 90 degrees; "
                "diffraction sine wave response."
            )
        if request.kind is AnalysisKind.NATIVE_PLOT:
            notes.append(
                "CODE V drew this plot itself; the service chose the plot type, the "
                "output file and the checks on it, and did not redraw the numbers."
            )
        return AnalysisSettings(
            zoom_position=zoom,
            field_numbers=field_numbers,
            wavelength_numbers=wavelength_numbers,
            ray_grid=options.ray_grid,
            frequencies=sorted(options.frequencies) if options.frequencies else None,
            frequency_unit="cycles/mm" if request.kind is AnalysisKind.MTF else None,
            azimuth=options.azimuth,
            mtf_type=options.mtf_type if request.kind is AnalysisKind.MTF else None,
            plot_type=(
                options.plot_type if request.kind is AnalysisKind.NATIVE_PLOT else None
            ),
            wavefront_nrd=20 if request.kind is AnalysisKind.WAVEFRONT else None,
            notes=notes,
        )

    def _conjugate_block(self, listing_text: str) -> str:
        """Return the first order block of a listing, for the raw output."""
        lines = listing_text.splitlines()
        collected: list[str] = []
        inside = False
        for line in lines:
            if line.strip().startswith(("INFINITE CONJUGATES", "FINITE CONJUGATES")):
                inside = True
            elif inside and line.strip().startswith(("REFRACTIVE", "SOLVES", "APERTURE")):
                break
            if inside:
                collected.append(line)
        return "\n".join(collected)

    # ------------------------------------------------------- first order

    def _analyse_first_order(
        self, lens: LensData, settings: AnalysisSettings
    ) -> tuple[FirstOrderResult, str]:
        session = self._require_session()
        zoom = settings.zoom_position
        items = {
            "efy": f"(EFY Z{zoom})",
            "fno": f"(FNO Z{zoom})",
            "epd": f"(EPD Z{zoom})",
            "enp": f"(ENP Z{zoom})",
            "exp": f"(EXP Z{zoom})",
            "exd": f"(EXD Z{zoom})",
            "imd": f"(IMD Z{zoom})",
            "oal": f"(OAL Z{zoom})",
        }
        values: dict[str, float] = {}
        raw_lines = [f"EvaluateExpression results at zoom {zoom}:"]
        for name, item in items.items():
            values[name] = session.evaluate_number(item)
            raw_lines.append(f"  {item:<14} = {values[name]}")

        afocal = 0.0
        try:
            afocal = session.evaluate_number(f"(AFC Z{zoom})")
        except CodeVError:
            afocal = 0.0

        listing_text = session.command("lis")
        listing = parse_listing(listing_text)
        first_order = listing.first_order
        raw_lines.append("")
        raw_lines.append(self._conjugate_block(listing_text))

        warnings: list[str] = []
        if afocal:
            warnings.append(
                "This system is afocal (AFC is non zero); the focal length is not a "
                "meaningful number for it."
            )
        if first_order.effective_focal_length is not None:
            difference = abs(first_order.effective_focal_length - values["efy"])
            if difference > max(1e-3, abs(values["efy"]) * 1e-5):
                warnings.append(
                    "The listing focal length "
                    f"{first_order.effective_focal_length} differs from the database item "
                    f"{values['efy']}; the database item is reported."
                )

        result = FirstOrderResult(
            source=self.source,
            units=lens.units,
            zoom_position=zoom,
            effective_focal_length=values["efy"],
            back_focal_length=first_order.back_focal_length,
            front_focal_length=first_order.front_focal_length,
            f_number=values["fno"],
            image_distance=values["imd"],
            overall_length=values["oal"],
            paraxial_image_height=first_order.paraxial_image_height,
            entrance_pupil_diameter=values["epd"],
            entrance_pupil_distance=values["enp"],
            exit_pupil_diameter=values["exd"],
            exit_pupil_distance=values["exp"],
            raw_output="\n".join(raw_lines),
            precision_note=(
                "Focal length, f number, image distance, overall length and the pupil "
                "diameters come from Macro-PLUS database items through "
                "EvaluateExpression, which returns a string with about sixteen "
                "significant digits. The back and front focal length and the paraxial "
                "image height are read from the printed listing, which only carries the "
                "printed precision, and are reported for reference. "
                "ENP and EXP are pupil distances measured from the first surface, not "
                "diameters; the diameters are EPD and EXD."
            ),
            warnings=warnings,
        )
        return result, "\n".join(raw_lines)

    def _analyse_wavefront(
        self, lens: LensData, settings: AnalysisSettings
    ) -> tuple[WavefrontResult, str]:
        """Run only the verified, non-refocusing NOM variant of WAV."""
        session = self._require_session()
        trusted = self._last_known_snapshot
        if trusted is None:
            reason = "the committed lens state is unavailable before WAV"
            self._invalidate_lens(reason)
            raise SessionInvalidError(reason)
        try:
            before = read_snapshot(session, lens)
            prior_changes = compare_snapshots(trusted, before)
        except Exception as exc:  # noqa: BLE001 - an incomplete check cannot prove safety
            reason = f"the lens state could not be verified before WAV ({type(exc).__name__}: {exc})"
            self._invalidate_lens(reason)
            raise SessionInvalidError(reason) from exc
        if prior_changes:
            reason = "the live lens differs from its committed state before WAV"
            self._invalidate_lens(reason)
            raise SessionInvalidError(reason, details={"changes": prior_changes})
        raw: str | None = None
        try:
            raw = session.command("wav;nom yes;bes no;nrd 20;go", error_kind=ComputationError)
        finally:
            # Read the native output first; only then may a listing/EVA call
            # overwrite CODE V's command buffer. Check even when WAV fails.
            try:
                after = read_snapshot(session, lens)
                changes = compare_snapshots(before, after)
            except Exception as exc:  # noqa: BLE001 - uncertainty is a write barrier
                reason = f"the lens state could not be verified after WAV ({type(exc).__name__}: {exc})"
                self._invalidate_lens(reason)
                raise SessionInvalidError(reason, raw_output=raw) from exc
            if changes:
                reason = "WAV changed the lens state unexpectedly"
                self._invalidate_lens(reason)
                raise SessionInvalidError(reason, details={"changes": changes}, raw_output=raw)
        if session.output_is_truncated(raw):
            if self._task is not None:
                self._task.output_truncated = True
            raise ComputationError("WAV output filled the text buffer.", raw_output=raw)
        try:
            parsed = parse_nominal_wavefront(raw, len(lens.fields))
        except ValueError as exc:
            raise ComputationError(str(exc), raw_output=raw) from exc
        if parsed["position"] != settings.zoom_position:
            raise ComputationError("WAV output zoom does not match the requested lens.", raw_output=raw)
        for field, (x_angle, y_angle) in zip(lens.fields, parsed["field_coordinates_deg"]):
            if (field.x_angle is None or field.y_angle is None
                    or abs(field.x_angle - x_angle) > 0.0051
                    or abs(field.y_angle - y_angle) > 0.0051):
                raise ComputationError(
                    f"WAV field {field.number} coordinates do not match the open lens.",
                    raw_output=raw,
                )
        if len(parsed["wavelengths_nm"]) != len(lens.wavelengths) or any(
            wave.micrometers is None
            or abs(shown - wave.micrometers * 1000) > 0.11
            for shown, wave in zip(parsed["wavelengths_nm"], lens.wavelengths)
        ):
            raise ComputationError("WAV wavelengths do not match the open lens.", raw_output=raw)
        references = [wave for wave in lens.wavelengths if wave.is_reference]
        if len(references) != 1 or references[0].micrometers is None:
            raise ComputationError("The open lens has no unambiguous reference wavelength.", raw_output=raw)
        result = WavefrontResult(
            source=self.source, zoom_position=settings.zoom_position,
            wavelength_numbers=settings.wavelength_numbers,
            reference_wavelength_number=references[0].number,
            reference_wavelength_nm=references[0].micrometers * 1000,
            fields=[WavefrontField(field_number=field.number, rms_waves=rms,
                                   strehl=strehl, rays_traced=rays)
                    for field, (rms, strehl), rays in zip(lens.fields, parsed["values"], parsed["rays"])],
            weighted_rms_waves=parsed["weighted"][0],
            weighted_strehl=parsed["weighted"][1], raw_output=raw,
            warnings=["The NOM listing does not print its polychromatic RMS equivalent wavelength; "
                      "the lens reference wavelength is reported separately and is not substituted for it.",
                      "CODE V's displayed Strehl is an approximation and may be zero for large RMS."],
        )
        return result, raw

    # ------------------------------------------------------- spot diagram

    def _trace_spot_grids(
        self, lens: LensData, settings: AnalysisSettings, grids: list[int]
    ) -> tuple[list[list[tuple[float, float]]], list[str]]:
        """Trace pupil grids with aimed launch points and return image coordinates.

        The rays are launched from the first tangent plane with RAYTRA, which
        needs the launch point of each ray. The paraxial entrance pupil is a
        poor guess for a large field (the real pupil is shifted and distorted,
        and the grid was stopped by the aperture: all 207 rays at 23.5 degrees),
        so a few rays aimed by CODE V (RAYRSI) calibrate a map from relative
        pupil coordinates to launch points for each wavelength, and the grids
        are traced through that map. The relative pupil is scaled by the
        vignetting factors of the field, because that is the pupil the native
        SPO option traces and RAYRSI itself ignores them.

        Aperture checking is requested so that rays blocked by an aperture or
        obscuration are reported with a negative status: they are excluded from
        the grid, which is what the SPO option does as well.

        Returns one list of image points per requested grid and the warnings
        about rays that could not be traced.
        """
        session = self._require_session()
        zoom = settings.zoom_position
        field_number = settings.field_numbers[0]
        field = next(item for item in lens.fields if item.number == field_number)
        if self._field_kind() not in {None, "angle"}:
            raise UnsupportedError(
                "The first release builds spot diagrams for fields given as angles; "
                f"this lens defines fields as {self._field_kind()}.",
                hint="Use the native SPO listing for other field types.",
            )
        direction_x = math.tan(math.radians(field.x_angle or 0.0))
        direction_y = math.tan(math.radians(field.y_angle or 0.0))
        factors = tuple(
            getattr(field, name) or 0.0 for name in ("vux", "vlx", "vuy", "vly")
        )

        diameter = session.evaluate_number(f"(EPD Z{zoom})")
        if diameter <= 0:
            raise ComputationError(
                "The entrance pupil diameter is not positive, so no pupil grid can be built.",
                details={"epd": diameter},
            )
        pupil_radius = diameter / 2.0

        results: list[list[tuple[float, float]]] = [[] for _ in grids]
        # Only the first (plotted) grid is reported: the rays the others lose
        # at the edge of the pupil only thin out the statistics cross-check.
        launched = 0
        blocked = 0
        worst_residual = 0.0
        for wavelength in settings.wavelength_numbers:
            pupil_map = self._aim_pupil_map(
                session, zoom, wavelength, field_number, direction_x, direction_y, factors
            )
            worst_residual = max(worst_residual, pupil_map.max_residual)
            for index, grid in enumerate(grids):
                for u, v in aiming.unit_pupil_grid(grid):
                    x, y = pupil_map.launch(*aiming.vignetted_pupil(u, v, *factors))
                    status, values = session.raytra(
                        zoom, wavelength, 1, [x, y, direction_x, direction_y]
                    )
                    if index == 0:
                        launched += 1
                    if status == 0.0 and len(values) >= 2:
                        results[index].append((values[0], values[1]))
                    elif index == 0:
                        blocked += 1
        warnings: list[str] = []
        if not results[0]:
            raise ComputationError(
                "No ray of the plot grid reached the image surface.",
                details={"rays_traced": launched},
            )
        if blocked:
            warnings.append(
                f"{blocked} of {launched} plot grid rays were blocked by an aperture or "
                "failed to trace and are not plotted; the native SPO statistics are unaffected."
            )
        if worst_residual > PUPIL_MAP_TOLERANCE * pupil_radius:
            warnings.append(
                "The pupil aiming map fitted to the aimed calibration rays deviates by "
                f"up to {worst_residual:.3g} (launch plane units) from those rays, more "
                f"than {PUPIL_MAP_TOLERANCE * 100:.1f}% of the pupil radius; the plotted "
                "grid may be distorted near the pupil edge."
            )
        return results, warnings

    def _aim_pupil_map(
        self,
        session: Any,
        zoom: int,
        wavelength: int,
        field_number: int,
        direction_x: float,
        direction_y: float,
        factors: tuple[float, float, float, float],
    ) -> "aiming.PupilMap":
        """Fit the relative pupil to launch point map from RAYRSI rays.

        RAYRSI leaves the ray in CODE V's database: X, Y and Z at surface 1 are
        where the ray met the first surface, so the launch point on the tangent
        plane is that point moved back along the object space direction by the
        surface sag (Z). Each ray is read before the next one is traced.
        """
        samples: list[tuple[float, float, float, float]] = []
        for u, v in aiming.calibration_nodes():
            su, sv = aiming.vignetted_pupil(u, v, *factors)
            if session.rayrsi(zoom, wavelength, field_number, [su, sv, 0.0, 0.0]) != 0.0:
                continue
            x = session.evaluate_number("(X S1)")
            y = session.evaluate_number("(Y S1)")
            z = session.evaluate_number("(Z S1)")
            samples.append((su, sv, x - z * direction_x, y - z * direction_y))
        try:
            return aiming.fit_pupil_map(samples)
        except ValueError as exc:
            raise ComputationError(
                f"The pupil of wavelength {wavelength} could not be aimed: {exc}",
                details={"wavelength": wavelength, "aimed_rays": len(samples)},
                hint="Use the native SPO plot for this field.",
            ) from exc

    @staticmethod
    def _spot_statistics(points: list[tuple[float, float]]) -> tuple[float, float]:
        """Return (rms radius about the centroid, largest radius)."""
        count = len(points)
        centroid_x = sum(point[0] for point in points) / count
        centroid_y = sum(point[1] for point in points) / count
        radii = [
            math.hypot(point[0] - centroid_x, point[1] - centroid_y) for point in points
        ]
        rms = math.sqrt(sum(radius * radius for radius in radii) / count)
        return rms, max(radii)

    def _start_spot(self, settings: AnalysisSettings) -> None:
        """Submit the SPO option as an asynchronous command."""
        session = self._require_session()
        session.async_command("spo; go")
        self._pending_spot = {"settings": settings, "started_at": time.monotonic()}
        self.log("SPO submitted as an asynchronous command")

    def _advance_spot(self) -> bool:
        """Poll the running SPO option; True once the task reached a result."""
        session = self._require_session()
        pending = self._pending_spot
        if pending is None:
            return True
        status = session.wait(POLL_WAIT_SECONDS)
        executing = session.is_executing_command()
        elapsed = time.monotonic() - pending["started_at"]
        if status != 0 or executing:
            # As with native plots, CODE V 10.2 can report Wait completion
            # before SPO has actually stopped. Do not consume its output yet.
            if self._task is not None:
                self._task.progress = f"SPO is still running ({elapsed:.0f} seconds)."
            return False
        # The output has to be fetched before any call that could reset it.
        listing_text = session.get_command_output()
        truncated = session.output_is_truncated(listing_text)
        if self._task is not None:
            self._task.raw_output = listing_text
            if truncated:
                self._task.output_truncated = True
                self._task.warnings.append(
                    "The SPO output filled the text buffer and may be cut off; the parsed "
                    "values carry that caveat."
                )
        result = self._finish_spot(
            self._require_lens(), pending["settings"], listing_text, truncated
        )
        self._analysis_payload["spot_diagram"] = result
        self._pending_spot = None
        return True

    def _finish_spot(
        self,
        lens: LensData,
        settings: AnalysisSettings,
        listing_text: str,
        truncated: bool,
    ) -> SpotDiagramResult:
        """Parse the native statistics, trace the plot grid and build the result."""
        field_number = settings.field_numbers[0]
        spot = parse_spot_listing(listing_text)
        warnings: list[str] = [
            "The RMS and 100% values are computed by the native SPO option and printed "
            "as diameters; they are reported here as radii, which is why size_is_radius "
            "is true."
        ]
        if truncated:
            warnings.append(
                "The SPO output filled the text buffer, so the annotations may be "
                "incomplete; only the values that were present are reported."
            )
        if not spot.fields:
            raise ComputationError(
                "The SPO output did not contain spot size annotations, so the native "
                "statistics could not be read.",
                details={"field": field_number, "output_truncated": truncated},
                raw_output=listing_text,
                hint="Check the SPO output settings in CODE V.",
            )
        index = min(field_number - 1, len(spot.fields) - 1)
        if field_number - 1 >= len(spot.fields):
            warnings.append(
                f"The SPO listing only annotated {len(spot.fields)} field(s); the values "
                f"of field {index + 1} are reported for field {field_number}."
            )
        statistic = spot.fields[index]
        if statistic.rms_diameter is None:
            raise ComputationError(
                "The SPO listing did not contain an RMS spot size.",
                raw_output=listing_text,
            )
        rms_radius = statistic.rms_diameter / 2.0
        max_radius = (
            statistic.hundred_diameter / 2.0
            if statistic.hundred_diameter is not None
            else None
        )

        # Plot coordinates: a pupil grid traced with the documented RAYTRA call.
        plot_grid = settings.ray_grid or DEFAULT_PLOT_GRID
        (plot_points, statistics_points), grid_warnings = self._trace_spot_grids(
            lens, settings, [plot_grid, DEFAULT_STATISTICS_GRID]
        )
        warnings.extend(grid_warnings)
        _, cross_check_max = self._spot_statistics(statistics_points)
        cross_check_rms, _ = self._spot_statistics(statistics_points)

        if max_radius:
            difference = abs(cross_check_max - max_radius) / max(max_radius, 1e-12)
            if difference > SPOT_MAX_TOLERANCE:
                warnings.append(
                    "The largest radius of the traced plot grid "
                    f"({cross_check_max:.6g}) differs from the native 100% spot radius "
                    f"({max_radius:.6g}) by {difference * 100:.1f}%; the native value is "
                    "reported."
                )
        difference = abs(cross_check_rms - rms_radius) / max(rms_radius, 1e-12)
        if difference > SPOT_RMS_TOLERANCE:
            warnings.append(
                f"The RMS radius of the traced grid ({cross_check_rms:.6g}) differs from "
                f"the native RMS spot radius ({rms_radius:.6g}) by {difference * 100:.1f}%; "
                "the native value is reported."
            )

        airy_radius, airy_note = self._airy_radius(lens, settings)
        warnings.append(airy_note)
        image = self._plot_spot(plot_points, max_radius, airy_radius, lens, settings)
        result = SpotDiagramResult(
            source=self.source,
            units=lens.units,
            zoom_position=settings.zoom_position,
            field_number=field_number,
            wavelength_numbers=settings.wavelength_numbers,
            centroid_x=statistic.centroid_x,
            centroid_y=statistic.centroid_y,
            rms_radius=rms_radius,
            max_radius=max_radius,
            size_is_radius=True,
            plot_sample_count=len(plot_points),
            statistics_sample_count=statistic.rays,
            image=image,
            raw_output=listing_text,
            warnings=warnings,
        )
        return result

    def _airy_radius(
        self, lens: LensData, settings: AnalysisSettings
    ) -> tuple[float | None, str]:
        """Airy disk radius 1.22 x wavelength x F/# for the picture, and a note.

        The value is calculated by the service from the reference wavelength
        and the infinite conjugate F/# (FNO); it is not a CODE V result. CODE V
        draws its own circle from the real ray F/# in the native spot plot.
        """
        drawn = ("The green circle is the Airy disk radius, 1.22 x reference wavelength x F/# "
                 "(service calculated, {radius:.6g} {unit}); the grey circle is the native "
                 "100% spot radius.")
        reference = [wave for wave in lens.wavelengths if wave.is_reference]
        object_surface = next((s for s in lens.surfaces if s.role.value == "object"), None)
        if object_surface is None or not object_surface.thickness_is_infinite:
            return None, "The Airy disk is not drawn: the object is at a finite distance."
        if len(reference) != 1 or reference[0].micrometers is None:
            return None, "The Airy disk is not drawn: the reference wavelength is unavailable."
        try:
            f_number = self._require_session().evaluate_number(f"(FNO Z{settings.zoom_position})")
        except CodeVError:
            return None, "The Airy disk is not drawn: the F/# could not be read."
        if not math.isfinite(f_number) or f_number <= 0:
            return None, "The Airy disk is not drawn: the F/# is not positive."
        unit = lens.units.value
        radius = 1.22 * reference[0].micrometers / MICROMETERS_PER_UNIT[unit] * f_number
        return radius, drawn.format(radius=radius, unit=unit)

    def _plot_spot(
        self,
        points: list[tuple[float, float]],
        reference_radius: float | None,
        airy_radius: float | None,
        lens: LensData,
        settings: AnalysisSettings,
    ) -> ImagePayload | None:
        # The spot is drawn about the centroid of the traced grid: an off axis
        # field lands far from the axis, and absolute image coordinates would
        # squeeze the spot into a dot.
        centroid_x = sum(point[0] for point in points) / len(points)
        centroid_y = sum(point[1] for point in points) / len(points)
        try:
            canvas = plotting.scatter_plot(
                [(x - centroid_x, y - centroid_y) for x, y in points],
                reference_radius=reference_radius,
                airy_radius=airy_radius,
                title=f"spot {lens.units.value} field {settings.field_numbers[0]} about centroid",
            )
            return self._write_image(canvas, "spot")
        except OSError as exc:  # noqa: BLE001 - the numbers matter more than the picture
            self.log(f"could not write the spot diagram image: {exc}")
            return None

    def _write_image(self, canvas: "plotting.Canvas", suffix: str) -> ImagePayload:
        import base64

        self.result_directory.mkdir(parents=True, exist_ok=True)
        name = f"{(self._task.task_id if self._task else 'analysis')}-{suffix}.png"
        path = self.result_directory / name
        data = canvas.to_png()
        path.write_bytes(data)
        return ImagePayload(
            path=str(path),
            media_type="image/png",
            base64_data=base64.b64encode(data).decode("ascii"),
            width=canvas.width,
            height=canvas.height,
        )

    # --------------------------------------------------------- native plots

    def _native_plot_stem(self, task: TaskInfo, plot_type: NativePlotType) -> str:
        """An unused file stem that fits CODE V's filespec limit.

        The stem is shortened to the room the result directory leaves, because
        CODE V truncates a longer filespec and then the conversion can no longer
        find the plot file. The random part comes first so shortening can never
        remove the only part that makes the name new, and analysis task ids
        restart at 1 whenever the service starts, so they are never relied on.
        """
        room = (
            MAX_PLOT_FILESPEC
            - len(str(self.result_directory))
            - 1
            - len(PLOT_FILE_SUFFIX)
        )
        if room < MIN_PLOT_STEM:
            raise ComputationError(
                "The result directory leaves no usable room for a CODE V plot file "
                "name.",
                details={
                    "result_directory": str(self.result_directory),
                    "available_characters": room,
                    "minimum_characters": MIN_PLOT_STEM,
                    "filespec_limit": MAX_PLOT_FILESPEC,
                },
                hint="Run the service from a shorter working directory.",
            )
        for _ in range(8):
            stem = f"{uuid.uuid4().hex[:8]}-{plot_type.value}-{task.task_id}"[:room]
            stem = stem.rstrip("-")
            # No name with this prefix may exist: the plot file, its PNG and any
            # numbered variant CODE V might add all have to be new files.
            if stem and not list(self.result_directory.glob(f"{stem}*")):
                return stem
        raise ComputationError(
            "No unused native plot file name could be found in the result directory.",
            details={"result_directory": str(self.result_directory)},
        )

    def _release_native_graphics(self, session: Any, plot_type: NativePlotType) -> None:
        """Return vector graphics to the display and close the plot file.

        CODE V closes the current neutral plot file when the graphics
        destination changes, so this is what makes the .PLT file complete. A
        destination that cannot be reset leaves every later plot unreliable, so
        the session is marked invalid rather than used on.
        """
        try:
            session.command("gra t", error_kind=ComputationError)
        except CodeVError as exc:
            reason = (
                "the graphics output could not be returned to the display after "
                f"native plot {plot_type.value}: {exc.message}"
            )
            self._invalidate_lens(reason)
            raise SessionInvalidError(
                reason, details={"plot_type": plot_type.value}
            ) from exc

    def _abandon_native_plot(self, pending: dict[str, Any], reason: str) -> None:
        """Recover the graphics state after a native plot failed while polling.

        The option may still be running and its plot file is still open, so the
        calculation is stopped first and the graphics destination is put back
        afterwards. A stop or a reset that cannot be confirmed marks the session
        invalid: a plot file left open would swallow every later plot, and an
        option that is still running can no longer be supervised. This method
        never raises, because the task still has to reach a final state.
        """
        if pending.get("graphics_released"):
            # The graphics destination was already put back, or the session was
            # already invalidated; there is nothing left to recover.
            return
        pending["graphics_released"] = True
        plot_type: NativePlotType = pending["plot_type"]
        try:
            session = self._require_session()
        except CodeVError as exc:
            self.log(f"native plot {plot_type.value}: no session left to recover ({exc.message})")
            return
        try:
            session.stop_command()
            stopped = self._wait_for_stop(session)
        except CodeVError as exc:
            stopped = False
            self.log(f"native plot {plot_type.value}: the stop was refused ({exc.message})")
        if not stopped:
            self._invalidate_lens(
                f"native plot {plot_type.value} failed while polling ({reason}) and CODE V "
                "did not confirm that the drawing stopped"
            )
            return
        try:
            self._release_native_graphics(session, plot_type)
        except CodeVError as exc:
            # _release_native_graphics invalidates the session before it raises;
            # swallowing it here lets the task still be failed and reported.
            self.log(f"native plot {plot_type.value}: the reset failed ({exc.message})")

    def _start_native_plot(self, task: TaskInfo, settings: AnalysisSettings) -> None:
        """Point graphics at a fresh plot file and start the drawing option."""
        session = self._require_session()
        plot_type = settings.plot_type
        if plot_type is None:  # pragma: no cover - checked in _analysis_settings
            raise ParameterError("A native plot needs a plot type.")
        self.result_directory.mkdir(parents=True, exist_ok=True)
        stem = self._native_plot_stem(task, plot_type)
        plot_file = self.result_directory / f"{stem}{PLOT_FILE_SUFFIX}"
        # Vector graphics go to the file only, and the extender is spelled out:
        # the stem was already shortened so that the whole path stays inside the
        # filespec limit CODE V truncates at, otherwise the plot file it writes
        # could no longer be found by the conversion command.
        session.command(
            f"gra {command_filespec(plot_file)}",
            error_kind=ComputationError,
        )
        # Recorded before the option starts, so an engine loss between here and
        # the first poll still leaves the graphics state to be recovered.
        self._pending_native_plot = {
            "plot_type": plot_type,
            "plot_file": plot_file,
            "stem": stem,
            "zoom_position": settings.zoom_position,
            "started_at": time.monotonic(),
            #: Set once the graphics destination has been put back, so a later
            #: failure never releases it twice.
            "graphics_released": False,
        }
        try:
            session.async_command(NATIVE_PLOT_COMMANDS[plot_type])
        except CodeVError:
            # Nothing will ever write to the plot file GRA just opened, so the
            # graphics destination is put back before the failure surfaces.
            self._pending_native_plot = None
            self._release_native_graphics(session, plot_type)
            raise
        self.log(f"native plot {plot_type.value} submitted as an asynchronous command")

    def _advance_native_plot(self) -> bool:
        """Poll the drawing option; True once its plot has been exported."""
        session = self._require_session()
        pending = self._pending_native_plot
        if pending is None:
            return True
        status = session.wait(POLL_WAIT_SECONDS)
        executing = session.is_executing_command()
        elapsed = time.monotonic() - pending["started_at"]
        if status != 0 or executing:
            # Wait() reporting completion is not enough on this machine: the
            # option has been seen still executing afterwards, and reading the
            # output or touching the command line then destroys the result.
            if self._task is not None:
                self._task.progress = (
                    f"CODE V is still drawing the {pending['plot_type'].value} plot "
                    f"({elapsed:.0f} seconds)."
                )
            return False
        # The output has to be fetched before any call that could reset it.
        output = session.get_command_output()
        truncated = session.output_is_truncated(output)
        if self._task is not None:
            self._task.raw_output = output
            if truncated:
                self._task.output_truncated = True
                self._task.warnings.append(
                    "The plot command output filled the text buffer and is "
                    "incomplete, so the drawing is not treated as a finished result; "
                    "the files that were already written are kept for diagnosis."
                )
        result = self._finish_native_plot(pending, output, truncated)
        self._analysis_payload["native_plot"] = result
        self._pending_native_plot = None
        return True

    def _finish_native_plot(
        self, pending: dict[str, Any], output: str, truncated: bool
    ) -> NativePlotResult:
        """Close the plot file, verify it, convert it and read the PNG back."""
        session = self._require_session()
        plot_type: NativePlotType = pending["plot_type"]
        plot_file: Path = pending["plot_file"]
        warnings: list[str] = [
            "CODE V drew this plot itself with the command "
            f"{NATIVE_PLOT_COMMANDS[plot_type]!r}; the service only chose the output "
            "file and verified the result."
        ]

        # The graphics destination is put back before the output is judged: the
        # plot file is open either way, and leaving it open would send every
        # later plot into it. A release that cannot be confirmed invalidates the
        # session, which is a more serious outcome than the failed plot itself.
        self._release_native_graphics(session, plot_type)
        pending["graphics_released"] = True

        if not output.strip():
            raise ComputationError(
                "The native plot command produced no output at all.",
                details={"plot_type": plot_type.value},
                hint="The option may not have run; check the session and try again.",
            )
        errors = [line.strip() for line in output.splitlines() if ERROR_LINE.match(line)]
        if errors:
            raise ComputationError(
                "CODE V reported an error while drawing the native plot.",
                details={"plot_type": plot_type.value, "errors": errors},
                raw_output=output,
            )
        if truncated:
            # A clipped command output cannot prove that the drawing finished:
            # the missing part could hold an error or the rest of the option
            # listing. The files that were already written are kept for
            # diagnosis, but the plot is not reported as a complete result.
            raise ComputationError(
                "The plot command output filled the text buffer, so the drawing "
                "cannot be confirmed as complete.",
                details={
                    "plot_type": plot_type.value,
                    "output_characters": len(output),
                    "plot_file": str(plot_file),
                },
                raw_output=output,
                hint=(
                    "The exported files are kept in the result directory; raise the "
                    "session text buffer size or use a smaller lens."
                ),
            )

        try:
            written = self._locate_native_plot(plot_file, pending["stem"])
            if written is None:
                raise ComputationError(
                    "CODE V drew the plot but wrote no neutral plot file.",
                    details={"plot_type": plot_type.value, "expected": str(plot_file)},
                    raw_output=output,
                    hint=(
                        "Check that the option writes vector graphics and that the "
                        "service working directory is writable."
                    ),
                )
            plot_file = self._canonical_plot_file(written)
            plot_bytes = plot_file.stat().st_size
        except OSError as exc:
            # A file system failure has to reach the client as a structured
            # error, and the task has to end instead of being polled again.
            raise ComputationError(
                "The neutral plot file could not be read from the result directory.",
                details={
                    "plot_type": plot_type.value,
                    "result_directory": str(self.result_directory),
                    "error": str(exc),
                },
                raw_output=output,
            ) from exc
        if plot_bytes <= 0:
            raise ComputationError(
                "The neutral plot file CODE V wrote is empty.",
                details={"plot_type": plot_type.value, "plot_file": str(plot_file)},
                raw_output=output,
            )

        image = self._export_native_png(session, plot_file, plot_type)
        return NativePlotResult(
            source=self.source,
            plot_type=plot_type,
            zoom_position=pending["zoom_position"],
            image=image,
            plot_file_path=str(plot_file),
            plot_file_bytes=plot_bytes,
            raw_output=output,
            warnings=warnings,
        )

    def _locate_native_plot(self, requested: Path, stem: str) -> Path | None:
        """Find the neutral plot file CODE V actually wrote.

        The manual describes .PLT as the default extender of a GRA filespec,
        but on this machine CODE V wrote the name it was given, shortened to fit
        its filespec limit, so the name can lose the extender. The bare stem is
        accepted as well, and a single file in the result directory that starts
        with the stem covers any remaining spelling difference. An existing but
        empty file is returned, so an empty plot is reported as empty rather
        than as absent.
        """
        for candidate in (requested, requested.with_suffix("")):
            if candidate.exists():
                return candidate
        matches = sorted(
            path for path in self.result_directory.glob(f"{stem}*") if path.is_file()
        )
        if len(matches) == 1:
            return matches[0]
        return None

    def _canonical_plot_file(self, written: Path) -> Path:
        """Give a plot file the .PLT extender CODE V did not write.

        A filespec CODE V had to shorten can come back without the extender, and
        the conversion command resolves that name with .PLT, so such a file
        could never be converted. The plotting option has been closed at this
        point, so the file is renamed once, inside the service result directory,
        and an existing file is never overwritten.
        """
        if written.suffix.upper() == PLOT_FILE_SUFFIX:
            return written
        target = written.with_name(written.name + PLOT_FILE_SUFFIX)
        if target.exists():
            raise ComputationError(
                "The neutral plot file cannot be given its .PLT name because that "
                "name already exists.",
                details={"plot_file": str(written), "target": str(target)},
                hint=(
                    "The service never overwrites a plot file, and converting "
                    "through an older file would report the wrong picture."
                ),
            )
        try:
            written.rename(target)
        except OSError as exc:
            raise ComputationError(
                "The neutral plot file could not be given its .PLT name.",
                details={
                    "plot_file": str(written),
                    "target": str(target),
                    "error": str(exc),
                },
            ) from exc
        return target

    def _export_native_png(
        self, session: Any, plot_file: Path, plot_type: NativePlotType
    ) -> ImagePayload:
        """Convert the neutral plot file to PNG and read the result back."""
        import base64

        try:
            # The directory scan is part of the guarded work too: an access
            # error there would otherwise escape the backend and leave the task
            # pending instead of ending it.
            before = {path.name.lower() for path in self.result_directory.glob("*.png")}
            session.command(
                f"gcv png {command_filespec(plot_file)}", error_kind=ComputationError
            )
            png_path = self._locate_native_png(plot_file, before, plot_type)
            data = png_path.read_bytes()
        except OSError as exc:
            raise ComputationError(
                "The converted PNG could not be read back from the result directory.",
                details={
                    "plot_type": plot_type.value,
                    "result_directory": str(self.result_directory),
                    "error": str(exc),
                },
                hint="The converted files are kept in the result directory.",
            ) from exc
        width, height = self._validate_png(data, png_path, plot_type)
        return ImagePayload(
            path=str(png_path),
            media_type="image/png",
            base64_data=base64.b64encode(data).decode("ascii"),
            width=width,
            height=height,
        )

    def _locate_native_png(
        self, plot_file: Path, before: set[str], plot_type: NativePlotType
    ) -> Path:
        """Find the PNG the conversion wrote.

        Only a file that appeared during this conversion counts: an older PNG
        with the expected name must never be reported as this plot's picture, so
        the expected name is preferred among the new files and exactly one new
        PNG is accepted as a fallback.
        """
        fresh = {
            path.name.lower(): path
            for path in self.result_directory.glob("*.png")
            if path.name.lower() not in before and path.stat().st_size > 0
        }
        for suffix in (".PNG", ".png"):
            expected = plot_file.with_suffix(suffix).name.lower()
            if expected in fresh:
                return fresh[expected]
        if len(fresh) == 1:
            return next(iter(fresh.values()))
        raise ComputationError(
            "CODE V reported no error but no PNG file appeared for the native plot.",
            details={
                "plot_type": plot_type.value,
                "plot_file": str(plot_file),
                "expected": str(plot_file.with_suffix(".png")),
                "candidates": [str(path) for path in sorted(fresh.values())],
            },
        )

    @staticmethod
    def _validate_png(data: bytes, path: Path, plot_type: NativePlotType) -> tuple[int, int]:
        """Prove that the converted file is a complete, decodable PNG.

        A partly written picture can still start with a valid signature and a
        plausible header, so the header alone proves nothing. The whole chunk
        list is walked instead: every chunk checksum is verified, the header has
        to be a 13 byte IHDR, at least one IDAT has to follow, the file has to
        end with an empty IEND and the pixel data has to inflate as one complete
        zlib stream. The inflated bytes are streamed and thrown away, because a
        real plot inflates to well over a hundred megabytes.
        """

        def broken(message: str, **details: Any) -> ComputationError:
            return ComputationError(
                message,
                details={"plot_type": plot_type.value, "path": str(path), **details},
            )

        def chunk_name(tag: bytes) -> str:
            return tag.decode("ascii", "replace")

        if not data.startswith(PNG_SIGNATURE):
            raise broken("The converted plot is not a PNG file.", bytes=len(data))

        offset = len(PNG_SIGNATURE)
        size: tuple[int, int] | None = None
        inflater = zlib.decompressobj()
        seen_idat = False
        seen_iend = False
        while offset < len(data):
            if offset + 8 > len(data):
                raise broken("The PNG ends inside a chunk header.", offset=offset)
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            tag = data[offset + 4 : offset + 8]
            end = offset + 12 + length
            if end > len(data):
                raise broken(
                    "The PNG ends inside a chunk.",
                    chunk=chunk_name(tag),
                    offset=offset,
                )
            payload = data[offset + 8 : offset + 8 + length]
            stored = struct.unpack(">I", data[offset + 8 + length : end])[0]
            if zlib.crc32(tag + payload) & 0xFFFFFFFF != stored:
                raise broken(
                    "A PNG chunk failed its checksum.", chunk=chunk_name(tag)
                )
            if tag == b"IHDR":
                if length != 13:
                    raise broken("The PNG header chunk is not 13 bytes.", length=length)
                width, height = struct.unpack(">II", payload[:8])
                if width <= 0 or height <= 0:
                    raise broken(
                        "The converted plot has an empty image size.",
                        width=width,
                        height=height,
                    )
                size = (width, height)
            elif tag == b"IDAT":
                if inflater.eof:
                    raise broken(
                        "The PNG carries pixel data after the end of its image stream."
                    )
                seen_idat = True
                remaining = payload
                while remaining:
                    try:
                        inflater.decompress(remaining, PNG_DECOMPRESS_STEP)
                    except zlib.error as exc:
                        # Structurally valid chunks with a broken compressed
                        # stream: the failure has to stay a structured error,
                        # or it would escape the backend and leave the task
                        # pending instead of ending it.
                        raise broken(
                            "The PNG pixel data could not be decompressed.",
                            error=str(exc),
                        ) from exc
                    tail = inflater.unconsumed_tail
                    if tail == remaining:
                        raise broken("The PNG pixel data could not be decompressed.")
                    remaining = tail
                if inflater.unused_data:
                    # The image stream ended inside this chunk but the chunk
                    # keeps going: the picture would carry trailing picture
                    # data, so it is not one complete stream.
                    raise broken(
                        "The PNG carries pixel data after the end of its image stream."
                    )
            elif tag == b"IEND":
                if length != 0:
                    raise broken("The PNG end chunk is not empty.", length=length)
                seen_iend = True
                offset = end
                break
            offset = end

        if size is None:
            raise broken("The PNG has no header chunk.")
        if not seen_idat:
            raise broken("The PNG has no pixel data.")
        if not seen_iend:
            raise broken("The PNG is incomplete: it has no end chunk.", bytes=len(data))
        if offset != len(data):
            raise broken(
                "The PNG has trailing data after its end chunk.",
                trailing=len(data) - offset,
            )
        if not inflater.eof:
            raise broken("The PNG pixel data is an incomplete zlib stream.")
        if inflater.unused_data:
            raise broken(
                "The PNG carries pixel data after the end of its image stream."
            )
        return size

    # ---------------------------------------------------------------- MTF

    def _analyse_mtf(
        self, lens: LensData, settings: AnalysisSettings
    ) -> tuple[MtfResult, str]:
        session = self._require_session()
        zoom = settings.zoom_position
        frequencies = settings.frequencies or []
        nrd = settings.ray_grid or 0

        try:
            afocal = session.evaluate_number(f"(AFC Z{zoom})")
        except CodeVError:
            afocal = 0.0
        if afocal:
            raise UnsupportedError(
                "The first release supports the diffraction MTF of focal systems only; "
                "this system is afocal.",
                details={"afc": afocal},
            )

        raw_lines = [
            "MTF_1FLD: field, azimuth, frequency, modulation, phase, analytic limit, "
            "actual limit, illumination, rays"
        ]
        curves: list[MtfCurve] = []
        series: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]] = []
        colours = [plotting.BLUE, plotting.RED, plotting.GREEN, plotting.ORANGE]
        for position, field_number in enumerate(settings.field_numbers):
            tangential: list[float] = []
            sagittal: list[float] = []
            analytic_limit: list[float] = []
            for frequency in frequencies:
                modulation_t, values_t = session.mtf_1fld(
                    zoom, field_number, frequency, 0.0, nrd, MTF_TYPE_DIF, MTF_TYPE_SINE
                )
                if modulation_t < 0:
                    raise ComputationError(
                        "MTF_1FLD reported a failed calculation.",
                        details={
                            "field": field_number,
                            "frequency": frequency,
                            "azimuth": 0.0,
                        },
                        hint="Check the field, wavelength and ray grid settings.",
                    )
                modulation_s, values_s = session.mtf_1fld(
                    zoom, field_number, frequency, 90.0, nrd, MTF_TYPE_DIF, MTF_TYPE_SINE
                )
                if modulation_s < 0:
                    raise ComputationError(
                        "MTF_1FLD reported a failed calculation.",
                        details={
                            "field": field_number,
                            "frequency": frequency,
                            "azimuth": 90.0,
                        },
                    )
                tangential.append(modulation_t)
                sagittal.append(modulation_s)
                if len(values_t) > 2:
                    analytic_limit.append(values_t[2])
                raw_lines.append(
                    f"  F{field_number} 0deg {frequency:g} {modulation_t:.6g} "
                    + " ".join(f"{value:.6g}" for value in values_t)
                )
                raw_lines.append(
                    f"  F{field_number} 90deg {frequency:g} {modulation_s:.6g} "
                    + " ".join(f"{value:.6g}" for value in values_s)
                )
            curves.append(
                MtfCurve(
                    field_number=field_number,
                    wavelength_numbers=settings.wavelength_numbers,
                    tangential=tangential,
                    sagittal=sagittal,
                    analytic_limit=analytic_limit,
                )
            )
            colour = colours[position % len(colours)]
            series.append((f"T{field_number}", list(zip(frequencies, tangential)), colour))
            series.append((f"S{field_number}", list(zip(frequencies, sagittal)), colour))

        image = None
        try:
            canvas = plotting.line_plot(
                series,
                title=f"MTF diffraction {lens.title[:20] if lens.title else ''}".strip(),
                y_label="MTF",
            )
            image = self._write_image(canvas, "mtf")
        except OSError as exc:  # noqa: BLE001
            self.log(f"could not write the MTF image: {exc}")

        result = MtfResult(
            source=self.source,
            zoom_position=zoom,
            frequencies=frequencies,
            frequency_unit="cycles/mm",
            azimuth=0.0,
            mtf_type=MtfType.DIFFRACTION,
            curves=curves,
            image=image,
            raw_output="\n".join(raw_lines),
            warnings=[
                "Tangential is azimuth 0 degrees and sagittal is azimuth 90 degrees.",
                "Diffraction MTF with the sine wave response; the reference wavelength "
                "and the stored weights are used and the lens is not refocused.",
            ],
        )
        return result, "\n".join(raw_lines)

    # --------------------------------------------------------------- session

    def close_session(self) -> StatusInfo:
        cleanup_confirmed = True
        cleanup_remaining: list[int] = []
        if self._session is not None:
            stopped = self._session.stop()
            cleanup_confirmed = stopped is not False
            cleanup_remaining = list(getattr(self._session, "cleanup_remaining", []))
        self._session = None
        self._session_valid = False
        self._reload_required = False
        self._checkpoint_load_attempted = True
        self._lens = None
        self._lens_open = False
        self._listing = None
        self._task = None
        self._pending_spot = None
        self._pending_native_plot = None
        self._closed = True
        self._lens_state = LensState.INVALID
        status = self.get_status()
        status.details["cleanup_confirmed"] = cleanup_confirmed
        status.details["cleanup_remaining"] = cleanup_remaining
        if not cleanup_confirmed:
            status.warnings.append("Owned CODE V process release could not be confirmed.")
        return status
