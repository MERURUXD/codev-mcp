"""Thin wrapper around the CODE V COM object.

The calling conventions here are the ones verified in Phase A:
activate the versioned ProgID, set the command timeout
in milliseconds, configure the working directory before StartCodeV, and treat
StopCodeV as potentially slow.

CODE V reports input problems as text in the command output rather than by
failing the COM call, so every command is inspected for an "Error:" line.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .errors import ComputationError, InternalError, NotReadyError, ParameterError, SessionInvalidError

PROG_ID = "CodeV.Command.102"
CLSID = "{E5900CEA-4A26-11DE-857D-001D09312B62}"
TLB_GUID = "{E5900CE8-4A26-11DE-857D-001D09312B62}"

#: Verified in Phase A: the value is milliseconds even though the manual says
#: seconds, so 120 aborted StartCodeV after half a second.
DEFAULT_COMMAND_TIMEOUT_MS = 600000
DEFAULT_TEXT_BUFFER_SIZE = 2000000

#: Starting the session is bounded separately: a stale recovery file used to
#: make StartCodeV wait forever, and a bounded failure is far better than a
#: wedged worker process.
STARTUP_TIMEOUT_MS = 120000

#: Recovery files CODE V writes into its working directory.
RECOVERY_FILE_PATTERNS = ("codev.rec", "codev*.rec")

#: A windowless engine that crashed stays in the process table with no threads
#: while an application error dialog waits for a click. Verified on real
#: machines: those sessions never answer a command again, and killing the
#: engine also dismisses the dialog, so the watchdog treats a threadless engine
#: as dead instead of waiting for the startup timeout.
WATCHDOG_POLL_SECONDS = 0.5

#: Processes the CODE V automation infrastructure uses. A windowless session
#: starts codevm.exe (the engine) and cvcommand.exe (the COM server); the engine
#: can die while the COM server stays alive, and calls then block forever.
CODEV_PROCESS_NAMES = ("codevm.exe", "cvcommand.exe", "cvcomsvr.exe")

#: File that records which processes this service started, so a later run can
#: clean up a session that was killed instead of stopped.
SESSION_FILE_NAME = "codev-mcp-session.json"


def list_codev_processes() -> dict[int, str]:
    """Return {pid: name} for every running CODE V process."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is available in the runtime
        return {}
    found: dict[int, str] = {}
    try:
        for process in psutil.process_iter(["pid", "name"]):
            name = str(process.info.get("name") or "").lower()
            if name in CODEV_PROCESS_NAMES:
                found[int(process.info["pid"])] = name
    except Exception:  # noqa: BLE001 - diagnostics only
        return {}
    return found


def terminate_processes(pids: list[int]) -> list[int]:
    """Force kill the given pids. Returns the pids that are still alive."""
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return list(pids)
    remaining: list[int] = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.kill()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            if pid in list_codev_processes():
                remaining.append(pid)
    return remaining


def shared_server_pids(own_pids) -> set[int]:
    """The ``cvcomsvr.exe`` among ``own_pids`` that another session still needs.

    ``cvcomsvr.exe`` is one COM server that every session started while it runs shares, and it exits by
    itself when the last of them stops. The session that happened to start it records it as its own, but
    stopping or killing it takes the engines of the sessions that still run down with it (F7). It counts
    as shared while any engine or command server that is not in ``own_pids`` is alive.
    """
    own = {int(pid) for pid in own_pids}
    live = list_codev_processes()
    others_alive = any(pid not in own and name in ("codevm.exe", "cvcommand.exe") for pid, name in live.items())
    return {pid for pid in own if live.get(pid) == "cvcomsvr.exe"} if others_alive else set()


def engine_is_alive(pid: int) -> bool:
    """Whether one recorded engine process can still run commands.

    psutil reports a process as running while it sits on a fatal application
    error dialog, but such a process has already unwound its threads. Checking
    the thread count turns a two minute startup stall into a sub second
    decision, and killing that process also dismisses the dialog.
    """
    psutil = _psutil()
    if psutil is None:  # pragma: no cover - psutil is available in the runtime
        return pid in list_codev_processes()
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        return process.num_threads() > 0
    except Exception as exc:  # noqa: BLE001 - a gone process is a dead engine
        # Access denied says nothing about the engine; calling it dead would let a watchdog kill a
        # healthy process or drop a working session.
        denied = getattr(psutil, "AccessDenied", None)
        return isinstance(denied, type) and isinstance(exc, denied)


def _psutil() -> Any:
    """The psutil module, or None when it is not installed.

    Resolved through the module namespace so tests can substitute a fake.
    """
    try:
        import psutil  # noqa: PLC0415 - imported lazily like the other callers
    except ImportError:
        return None
    return psutil


def _identity(pid: int) -> dict[str, Any] | None:
    """Return a process identity; absence and unreadable identity differ."""
    psutil = _psutil()
    if psutil is None:
        raise RuntimeError("psutil is unavailable")
    try:
        process = psutil.Process(pid)
        return {"name": process.name().lower(), "created_at": process.create_time()}
    except psutil.NoSuchProcess:
        return None


def _matching_owned(pid: int, recorded: dict[str, Any]) -> bool | None:
    """True only for the same process; None means identity cannot be checked."""
    try:
        current = _identity(pid)
    except Exception:  # access denied is not evidence of release
        return None
    if current is None:
        return False
    if recorded.get("created_at") is None:
        # Without a start time only a different program name proves the recorded process is gone
        # (its pid was reused); the same name can still be ours and stays undecided.
        name = recorded.get("name")
        return False if name and current.get("name") != str(name).lower() else None
    if current != recorded:
        return False
    return True

#: Named mutex that serialises session starts of every codev-mcp process in this logon session. A mutex
#: goes away with its holder, so a killed worker cannot leave it locked.
STARTUP_MUTEX_NAME = "Local\\codev-mcp-session-start"
#: Longest wait for another session's start to finish (a first start can take about two minutes).
STARTUP_LOCK_WAIT_SECONDS = 600

#: CODE V starts its windowless engine unreliably on this machine: the engine
#: sometimes dies behind an application error dialog before StartCodeV returns.
#: The failure is transient, so the session is retried a few times, each time
#: after the crashed processes have been cleaned up.
START_ATTEMPTS = 4
START_RETRY_DELAY_SECONDS = 3.0
#: After every attempt failed, a new start is refused for this long instead of for good.
START_RETRY_COOLDOWN_SECONDS = 60.0
#: Allowance for abandoning one failed attempt: StopCodeV, the shutdown check
#: and the kills that follow it.
START_ATTEMPT_STOP_SECONDS = 60


def session_start_budget_seconds(attempts: int = START_ATTEMPTS) -> float:
    """Longest a call that has to start the session may legitimately take for the start alone.

    One wait for another session's start, then every attempt with its bounded StartCodeV, its
    abandonment and the pause before the next one. A caller that gives up sooner kills a worker
    whose CODE V processes then hold a licence until the next start cleans them up.
    """
    attempts = max(1, int(attempts))
    return (STARTUP_LOCK_WAIT_SECONDS
            + attempts * (STARTUP_TIMEOUT_MS / 1000 + START_ATTEMPT_STOP_SECONDS)
            + (attempts - 1) * START_RETRY_DELAY_SECONDS)


class _StartupLock:
    """Hold the startup mutex for the with-block; without pywin32 it does nothing."""

    def __init__(self, note: Any = None) -> None:
        self._note = note
        self._handle: Any = None
        self._win32event: Any = None
        self._held = False

    def __enter__(self) -> "_StartupLock":
        try:
            import win32event  # noqa: PLC0415 - Windows only, like the rest of the COM layer
        except ImportError:
            return self
        self._win32event = win32event
        self._handle = win32event.CreateMutex(None, False, STARTUP_MUTEX_NAME)
        begun = time.perf_counter()
        result = win32event.WaitForSingleObject(self._handle, int(STARTUP_LOCK_WAIT_SECONDS * 1000))
        if result == win32event.WAIT_TIMEOUT:
            self._handle.Close()
            self._handle = None
            raise ComputationError(
                "Another CODE V session was still starting after "
                f"{STARTUP_LOCK_WAIT_SECONDS} seconds.",
                details={"mutex": STARTUP_MUTEX_NAME},
                hint="Check for a stuck CODE V start (dialog or licence prompt), then try again.")
        # WAIT_ABANDONED means the previous holder died inside its start; the lock is ours either way.
        self._held = True
        waited = time.perf_counter() - begun
        if self._note is not None and waited >= 1.0:
            self._note(f"waited {waited:.1f} s for another session to finish starting")
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._handle is None:
            return
        try:
            if self._held:
                self._win32event.ReleaseMutex(self._handle)
        finally:
            self._handle.Close()
            self._handle = None
            self._held = False


#: Lock file that marks a working directory as held by one live session. The lock belongs to the open
#: handle, so the operating system drops it when the holding process exits, however it exits.
DIRECTORY_LOCK_FILE_NAME = "codev-mcp-session.lock"
#: The locked byte lies past the holder note, so a refused start can still read who holds the directory.
DIRECTORY_LOCK_OFFSET = 1 << 20


class WorkingDirectoryInUseError(NotReadyError):
    """Another live session holds the working directory; retrying in the same directory cannot help."""


class _DirectoryLock:
    """Exclusive hold on a working directory for the lifetime of one session; without msvcrt it does nothing.

    The recovery files and the process record in a working directory describe the session that uses it.
    A start clears both as leftovers of a dead session, so a second session on a directory that a live one
    still uses would stop that session's engine. Holding this lock from before that cleanup until the
    session stops makes the second start fail instead.
    """

    def __init__(self, directory: Path) -> None:
        self.path = directory / DIRECTORY_LOCK_FILE_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            import msvcrt  # noqa: PLC0415 - Windows only, like the rest of the COM layer
        except ImportError:
            return
        import os  # noqa: PLC0415

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(fd, DIRECTORY_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise WorkingDirectoryInUseError(
                "Another codev-mcp session is using this working directory.",
                details={"reason": "working_directory_in_use", "working_directory": str(self.path.parent),
                         "holder": self._read_holder(), "error": str(exc)},
                hint=("Give each MCP client or service instance its own working directory "
                      "(--working-directory or CODEV_MCP_WORKDIR), or close the other session first."),
            ) from exc
        self._fd = fd
        note = json.dumps({"service": "codev-mcp", "pid": os.getpid(),
                           "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}).encode("utf-8")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, note)
        except OSError:  # the note is diagnostics only; the lock itself is held
            pass

    def _read_holder(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is None:
            return
        import msvcrt  # noqa: PLC0415
        import os  # noqa: PLC0415

        fd, self._fd = self._fd, None
        try:
            os.lseek(fd, DIRECTORY_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # closing the handle drops the lock as well
            pass
        finally:
            os.close(fd)


ERROR_LINE = re.compile(r"^\s*Error:")
NUMBER = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?$")


class ComSession:
    """Owns the COM automation object for one windowless CODE V session."""

    def __init__(
        self,
        *,
        prog_id: str = PROG_ID,
        clsid: str = CLSID,
        starting_directory: str | Path | None = None,
        command_timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
        text_buffer_size: int = DEFAULT_TEXT_BUFFER_SIZE,
        log: Any = None,
        trace: Any = None,
        require_engine: bool = True,
    ) -> None:
        self.prog_id = prog_id
        self.clsid = clsid
        self.starting_directory = Path(starting_directory) if starting_directory else None
        self.command_timeout_ms = command_timeout_ms
        self.text_buffer_size = text_buffer_size
        self.log = log
        #: Optional callback invoked before every COM call, for diagnostics.
        self.trace = trace
        #: Evidence from Phase C and D: a healthy windowless session always
        #: starts an engine process right after StartCodeV. When it is missing
        #: the engine crashed or never started, and every following COM call
        #: blocks forever instead of failing, so the session is refused.
        self.require_engine = require_engine
        self._object: Any = None
        self._pythoncom: Any = None
        self._version: str | None = None
        self.removed_recovery_files: list[str] = []
        self.owned_processes: dict[int, str] = {}
        self.process_identities: dict[int, dict[str, Any]] = {}
        self.cleanup_confirmed: bool | None = None
        self.cleanup_remaining: list[int] = []
        #: Recorded processes left running on purpose because other sessions still use them (cvcomsvr.exe).
        self.shared_processes: list[int] = []
        self.cleaned_processes: list[int] = []
        self.engine_pids: set[int] = set()
        #: Held from before the leftover cleanup until stop, so no second session clears this one's files.
        self._directory_lock: _DirectoryLock | None = None
        #: Whether the process record in the working directory was written by this session; only then may
        #: stop remove it.
        self._recorded = False
        #: Number of COM calls this session has served. The engine is a 32 bit
        #: process that was observed to exit after a few hundred analysis calls,
        #: so the count is reported to the client.
        self.call_count = 0
        #: Wall time for the most recent successful StartCodeV, including COM activation.
        self.startup_seconds: float | None = None
        self.closed = False
        #: Set by the watchdog when the engine stopped being usable, so a
        #: blocked COM call is reported instead of waiting for its timeout.
        self.engine_dead = False
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    # -------------------------------------------------------------- lifecycle

    def _note(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def clear_recovery_files(self) -> list[str]:
        """Remove recovery files left behind by a session that did not stop cleanly.

        Verified in Phase C: with a leftover codev.rec in the working directory,
        StartCodeV blocks waiting for a recovery prompt that a windowless session
        can never answer, so the service would hang forever. start() holds the
        working directory lock first, so any recovery file in it is stale.
        """
        removed: list[str] = []
        if self.starting_directory is None:
            return removed
        for pattern in RECOVERY_FILE_PATTERNS:
            for path in sorted(self.starting_directory.glob(pattern)):
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                except OSError as exc:
                    raise ComputationError(
                        "A leftover CODE V recovery file could not be removed; starting a "
                        "session would block on it.",
                        details={"path": str(path), "error": str(exc)},
                        hint=(
                            "Make sure no other CODE V session is using this working "
                            "directory, then try again."
                        ),
                    ) from exc
                removed.append(path.name)
        if removed:
            self._note("removed recovery files: " + ", ".join(removed))
        self.removed_recovery_files = removed
        return removed

    @property
    def session_file(self) -> Path | None:
        if self.starting_directory is None:
            return None
        return self.starting_directory / SESSION_FILE_NAME

    def _record_session(self) -> None:
        path = self.session_file
        if path is None or not self.owned_processes:
            return
        try:
            path.write_text(
                json.dumps(
                    {
                        "service": "codev-mcp",
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "version": self._version,
                        "processes": {str(pid): self.process_identities.get(pid, {"name": name})
                                      for pid, name in self.owned_processes.items()},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._recorded = True
        except OSError as exc:  # noqa: PERF203 - diagnostics only
            self._note(f"could not record the session file: {exc}")

    def _hold_directory(self) -> None:
        if self._directory_lock is not None or self.starting_directory is None:
            return
        lock = _DirectoryLock(self.starting_directory)
        lock.acquire()
        self._directory_lock = lock

    def _release_directory(self) -> None:
        lock, self._directory_lock = self._directory_lock, None
        if lock is not None:
            lock.release()

    def _forget_session(self) -> None:
        path = self.session_file
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def cleanup_recorded_session(self) -> list[int]:
        """Kill the processes of a previous session that was never stopped.

        Only processes that this service recorded are touched, so a CODE V GUI
        session the user started is never affected.
        """
        path = self.session_file
        if path is None or not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._note(f"unreadable session file {path}: {exc}")
            raise SessionInvalidError("Recorded CODE V process ownership is unreadable.",
                                      details={"path": str(path), "error": str(exc)}) from exc
        try:
            recorded = {int(pid): value if isinstance(value, dict) else {"name": value}
                        for pid, value in (data.get("processes") or {}).items()}
        except (AttributeError, TypeError, ValueError) as exc:
            # Valid JSON of the wrong shape is as unreadable as broken JSON: the ownership it should
            # prove cannot be checked, so the record stays and the start is refused the same way.
            self._note(f"malformed session file {path}: {exc}")
            raise SessionInvalidError("Recorded CODE V process ownership is unreadable.",
                                      details={"path": str(path), "error": str(exc)}) from exc
        shared = shared_server_pids(recorded)
        targets = [pid for pid, identity in recorded.items()
                   if pid not in shared and _matching_owned(pid, identity) is True]
        if shared:
            self._note(f"leaving the shared CODE V COM server {sorted(shared)} to the sessions that still use it")
        if targets:
            self._note(f"terminating leftover CODE V processes {targets}")
            terminate_processes(targets)
        remaining = [pid for pid, identity in recorded.items()
                     if pid not in shared and _matching_owned(pid, identity) is not False]
        if remaining:
            self._note(f"leftover ownership could not be released or verified: {remaining}")
            raise SessionInvalidError(
                "Previous CODE V session cleanup was not confirmed.",
                details={"remaining_pids": remaining, "session_file": str(path)},
                hint=(f"If no codev-mcp service is using CODE V processes {remaining}, end them "
                      f"(Task Manager or taskkill /PID <pid>) and delete {path}, then try again."))
        self.cleaned_processes = targets
        self._forget_session()
        return targets

    def release_leftovers(self) -> list[int]:
        """Clean up a recorded dead session without starting a new one.

        The directory lock is held only for the cleanup, so a live session in the same directory makes
        this fail (WorkingDirectoryInUseError) instead of losing its engine.
        """
        path = self.session_file
        if path is None or not path.exists():
            return []
        self._hold_directory()
        try:
            return self.cleanup_recorded_session()
        finally:
            self._release_directory()

    def start(self) -> str:
        """Create the object and start an invisible CODE V session."""
        startup_started = time.perf_counter()
        import pythoncom
        import win32com.client
        import win32com.client.dynamic
        import win32com.client.gencache

        self._pythoncom = pythoncom
        pythoncom.CoInitialize()
        # Dynamic dispatch is enough and keeps win32com from writing generated
        # modules into the interpreter site-packages.
        win32com.client.gencache.is_readonly = True

        if self.starting_directory is not None:
            self.starting_directory.mkdir(parents=True, exist_ok=True)
            # The recovery files and the process record are only leftovers when no live session holds the
            # directory; another service on the same directory would otherwise lose its engine here.
            self._hold_directory()
            # A dead session's engine can outlive its worker and keep codev.rec open, so its recorded
            # processes go first; only then can the recovery file be removed.
            self.cleanup_recorded_session()
            self.clear_recovery_files()

        # The record of which processes belong to this session is "what appeared while it started", so two
        # sessions must never start at the same time: each would claim the other's processes and a cleanup
        # would then stop the sibling's engine (F7).
        with _StartupLock(self._note):
            return self._start_engine(pythoncom, win32com, startup_started)

    def _start_engine(self, pythoncom: Any, win32com: Any, startup_started: float) -> str:
        """Activate the object and start the engine; runs under the machine-wide startup lock."""
        last_error: Exception | None = None
        before = list_codev_processes()
        for spec in (self.prog_id, self.clsid):
            try:
                dispatch = pythoncom.CoCreateInstance(
                    spec, None, pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch
                )
            except Exception as exc:  # noqa: BLE001 - try the next identifier
                last_error = exc
                continue
            self._object = win32com.client.dynamic.Dispatch(dispatch)
            self._note(f"activated {spec}")
            # Record what activation started before StartCodeV: if the session
            # hangs while starting, the next run still knows what to clean up.
            self.owned_processes = {
                pid: name for pid, name in list_codev_processes().items() if pid not in before
            }
            self._capture_identities()
            self._record_session()
            break
        if self._object is None:
            raise ComputationError(
                f"Could not activate {self.prog_id}.",
                details={
                    "prog_id": self.prog_id,
                    "clsid": self.clsid,
                    "last_error": repr(last_error),
                },
                hint="Check that CODE V 10.2 is installed and the COM class is registered.",
            )

        if self.starting_directory is not None:
            self._object.SetStartingDirectory(str(self.starting_directory))
        self._object.SetCommandTimeout(min(self.command_timeout_ms, STARTUP_TIMEOUT_MS))
        self._object.SetMaxTextBufferSize(self.text_buffer_size)
        watchdog = self._start_startup_watchdog(before)
        try:
            self._object.StartCodeV()
        finally:
            self._stop_startup_watchdog(watchdog)
        # A startup that failed behind an application error dialog leaves a
        # threadless engine behind; killing it dismisses the dialog and frees
        # the licence seat, so the next attempt starts from a clean machine.
        self._discard_dead_engine()
        self._object.SetCommandTimeout(self.command_timeout_ms)
        self._version = str(self._object.GetCodeVVersion())
        after = list_codev_processes()
        self.owned_processes = {
            pid: name for pid, name in after.items() if pid not in before
        }
        self._capture_identities()
        self.engine_pids = {
            pid for pid, name in self.owned_processes.items() if name == "codevm.exe"
        }
        self._note(f"session processes: {self.owned_processes}")
        self._record_session()
        self._require_engine_started()
        self._start_watchdog()
        self.startup_seconds = time.perf_counter() - startup_started
        return self._version

    # ------------------------------------------------------------- watchdog

    def _start_startup_watchdog(
        self, before: dict[int, str]
    ) -> tuple[threading.Thread, threading.Event]:
        """Watch for a crashed engine while StartCodeV is still running.

        StartCodeV can block for its whole timeout when the engine dies behind a
        modal application error dialog. The watchdog kills the threadless engine
        as soon as it appears, which makes StartCodeV return and dismisses the
        dialog without anybody clicking it.

        The watchdog has its own stop event that is never cleared: a scan that
        outlasts the join in _stop_startup_watchdog still sees the stop when it
        returns, and the event is checked again right before a kill, so a
        watchdog that stopped late never kills an engine that started later.
        """
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(WATCHDOG_POLL_SECONDS):
                for pid, name in list_codev_processes().items():
                    if name != "codevm.exe" or pid in before:
                        continue
                    if not engine_is_alive(pid) and not stop.is_set():
                        self._note(f"engine {pid} crashed during startup; terminating it")
                        terminate_processes([pid])
                        return

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        return thread, stop

    def _stop_startup_watchdog(
        self, watchdog: tuple[threading.Thread, threading.Event] | None
    ) -> None:
        if watchdog is None:
            return
        thread, stop = watchdog
        stop.set()
        thread.join(timeout=2 * WATCHDOG_POLL_SECONDS)
        if thread.is_alive():
            self._note("the startup watchdog is still finishing a process scan; it stops after it")

    def _discard_dead_engine(self) -> bool:
        """Kill a threadless engine recorded for this session."""
        dead = [pid for pid in self._candidate_engine_pids()
                if not engine_is_alive(pid) and _matching_owned(
                    pid, self.process_identities.get(pid, {"name": "codevm.exe"})) is True]
        if not dead:
            return False
        self._note(f"terminating crashed engine processes {dead}")
        terminate_processes(dead)
        return True

    def _candidate_engine_pids(self) -> list[int]:
        engine_pids = set(self.engine_pids)
        for pid, name in self.owned_processes.items():
            if name == "codevm.exe":
                engine_pids.add(pid)
        return sorted(engine_pids)

    def _capture_identities(self) -> None:
        for pid, name in self.owned_processes.items():
            try:
                identity = _identity(pid)
            except Exception:
                identity = None
            if identity is not None and identity["name"] == name:
                self.process_identities[pid] = identity

    def _start_watchdog(self) -> None:
        """Keep watching the engine so a later crash is reported at once.

        After the engine dies, every COM call blocks instead of failing, so the
        watchdog marks the session dead; the next call then raises
        session_invalid instead of hanging until the client timeout.
        """
        if self._watchdog is not None or not self.engine_pids:
            return
        engine_pids = set(self.engine_pids)

        def watch() -> None:
            while not self._watchdog_stop.wait(WATCHDOG_POLL_SECONDS):
                if not any(engine_is_alive(pid) for pid in engine_pids):
                    self.engine_dead = True
                    self._note(
                        f"the CODE V engine {sorted(engine_pids)} is no longer usable; "
                        "marking the session dead"
                    )
                    return

        self._watchdog = threading.Thread(target=watch, daemon=True)
        self._watchdog.start()

    def stop(self) -> bool:
        """Close COM and report whether owned process release was confirmed."""
        try:
            return self._stop()
        finally:
            # A stop that fails part way must still give the directory back: the process lives on, so the lock
            # would otherwise refuse every later session in it, including this service's own rebuild.
            self._release_directory()

    def _stop(self) -> bool:
        if self.closed:
            # A second stop (the worker's shutdown and then its exit) has nothing left to release, and
            # a second CoUninitialize would unbalance the CoInitialize of start.
            return bool(self.cleanup_confirmed)
        self._watchdog_stop.set()
        self._watchdog = None
        # After the engine died every call on the COM server blocks (see _check_engine), StopCodeV
        # included, so a dead session is not asked to stop: its own processes are killed first and
        # the COM object is let go only afterwards, when nothing is left to block on.
        engine_lost = self._engine_lost()
        if self._object is not None and engine_lost:
            self._note("the engine is gone; skipping StopCodeV and stopping the session's processes")
        elif self._object is not None:
            try:
                self._object.StopCodeV()
                self._note("StopCodeV returned")
            except Exception as exc:  # noqa: BLE001
                self._note(f"StopCodeV failed: {type(exc).__name__}: {exc}")
            self._object = None
        # A session whose engine already crashed never answers StopCodeV with a
        # clean exit, so its processes are removed here rather than waited out.
        self._discard_dead_engine()
        leftovers = self._verify_shutdown(wait_seconds=0 if engine_lost else 20)
        if leftovers:
            self._note(f"terminating CODE V processes left behind by StopCodeV: {leftovers}")
            safe = [pid for pid in leftovers if _matching_owned(
                pid, self.process_identities.get(pid, {"name": self.owned_processes[pid]})) is True]
            if safe:
                terminate_processes(safe)
        self._object = None
        shared = shared_server_pids(self.owned_processes)
        self.shared_processes = sorted(shared)
        if shared:
            self._note(f"leaving the shared CODE V COM server {sorted(shared)} to the sessions that still use it")
        self.cleanup_remaining = [pid for pid in self.owned_processes if pid not in shared and _matching_owned(
            pid, self.process_identities.get(pid, {"name": self.owned_processes[pid]})) is not False]
        self.cleanup_confirmed = not self.cleanup_remaining
        if not self.cleanup_confirmed:
            self._note(f"CODE V process release unconfirmed: {self.cleanup_remaining}")
        elif self._recorded:
            # A record this session did not write belongs to another live session (a refused start) or
            # is a leftover whose cleanup failed; either way it stays for the session that can confirm it.
            self._forget_session()
        self.closed = True
        if self._pythoncom is not None:
            try:
                self._pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
        return self.cleanup_confirmed

    def _engine_lost(self) -> bool:
        """Whether the recorded engine is known to be gone; without a recorded engine nobody can tell."""
        if self.engine_dead:
            return True
        return bool(self.engine_pids) and not any(engine_is_alive(pid) for pid in self.engine_pids)

    def _verify_shutdown(self, wait_seconds: float = 20) -> list[int]:
        """Return the recorded processes that are still alive after StopCodeV."""
        if not self.owned_processes:
            return []
        alive = list_codev_processes()
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            shared = shared_server_pids(self.owned_processes)
            remaining = [pid for pid in self.owned_processes if pid in alive and pid not in shared]
            if not remaining:
                return []
            time.sleep(2)
            alive = list_codev_processes()
        shared = shared_server_pids(self.owned_processes)
        return [pid for pid in self.owned_processes if pid in alive and pid not in shared]

    @property
    def started(self) -> bool:
        return self._object is not None

    @property
    def version(self) -> str | None:
        return self._version

    # ----------------------------------------------------------------- access

    def _require_object(self) -> Any:
        if self._object is None:
            raise SessionInvalidError(
                "The CODE V session is not running.",
                hint="Restart the service, or call get_status to see the last error.",
            )
        return self._object

    def _raw(self, method: str, *args) -> str:
        obj = self._require_object()
        self._check_engine()
        self.call_count += 1
        if self.trace is not None:
            label = f"{method}({args[0]!r})" if args else f"{method}()"
            self.trace(label)
        result = self._invoke(obj, method, *args)
        return "" if result is None else str(result)

    def _invoke(self, obj: Any, method: str, *args) -> Any:
        """Call one COM method, turning a COM failure into a session error.

        Every call goes through here, including the array based analysis calls,
        so a dead engine is always reported as session_invalid instead of an
        internal error.
        """
        try:
            return getattr(obj, method)(*args)
        except Exception as exc:  # noqa: BLE001 - the COM call itself failed
            raise SessionInvalidError(
                f"The COM call {method} failed: {type(exc).__name__}: {exc}",
                details={"method": method},
                hint="The session may be unusable; reopen the service.",
            ) from exc

    def _check_engine(self) -> None:
        """Fail fast when the engine process is gone.

        Phase C evidence: when codevm.exe exits while the COM server keeps
        running, every following call blocks forever instead of failing. The
        service watches the engine it recorded, so a dead session is reported
        instead of hanging the tool call.
        """
        if self.engine_dead:
            raise SessionInvalidError(
                "The CODE V engine stopped responding; the session can no longer be used.",
                details={"engine_pids": sorted(self.engine_pids)},
                hint="Restart the service to open a new session.",
            )
        if not self.engine_pids:
            return
        if not any(engine_is_alive(pid) for pid in self.engine_pids):
            self.engine_dead = True
            raise SessionInvalidError(
                "The CODE V engine process has exited; the session can no longer be used.",
                details={"engine_pids": sorted(self.engine_pids)},
                hint="Restart the service to open a new session.",
            )

    def _require_engine_started(self) -> None:
        """Refuse a session whose engine process never appeared.

        Observed in Phase C and D: StartCodeV can return successfully while the
        engine has crashed behind an application error dialog. Nothing then
        answers, so a later command blocks forever. Reporting it here turns that
        hang into a diagnosable error.
        """
        if not self.require_engine or self.engine_pids:
            return
        raise ComputationError(
            "CODE V started but no engine process appeared, so the session cannot "
            "execute commands.",
            details={"processes": {str(pid): name for pid, name in self.owned_processes.items()}},
            hint=(
                "Close any CODE V error dialog, make sure no other CODE V session is "
                "blocking the engine, then restart the service."
            ),
        )

    def command(self, text: str, *, error_kind: type[Exception] = ParameterError) -> str:
        """Run a synchronous CODE V command and return its output."""
        if "\n" in text or "\r" in text:
            raise ParameterError("A command may not contain newlines.", details={"command": text})
        started = time.perf_counter()
        output = self._raw("Command", text)
        elapsed = time.perf_counter() - started
        self._note(f"command {text!r} -> {elapsed:.3f}s")
        errors = [line.strip() for line in output.splitlines() if ERROR_LINE.match(line)]
        if errors:
            raise error_kind(
                errors[0],
                details={"command": text, "errors": errors},
                raw_output=output,
            )
        return output

    def command_raw(self, text: str) -> str:
        """Run a command without turning an Error line into an exception."""
        return self._raw("Command", text)

    def evaluate(self, item: str) -> str:
        """Evaluate one Macro-PLUS database item and return the raw string."""
        started = time.perf_counter()
        value = self._raw("EvaluateExpression", item).strip()
        elapsed = time.perf_counter() - started
        if elapsed > 1.0:
            self._note(f"EvaluateExpression {item!r} took {elapsed:.2f}s")
        return value

    def evaluate_quietly(self, item: str) -> str:
        """Evaluate without the slow call note; used in tight read loops."""
        return self._raw("EvaluateExpression", item).strip()

    def evaluate_number(self, item: str) -> float:
        """Evaluate a database item that must return a number.

        A misspelled item makes CODE V echo the previous result instead of
        failing, so a value that does not parse is reported as an internal error
        rather than being used as a number.
        """
        text = self.evaluate(item)
        if not NUMBER.match(text):
            raise InternalError(
                f"{item} did not return a number.",
                details={"item": item, "value": text},
                hint=(
                    "The database item may not exist; CODE V echoes the previous "
                    "value instead of reporting an error."
                ),
            )
        return float(text)

    def evaluate_optional(self, item: str) -> str:
        """Evaluate an item that may legitimately return an empty string."""
        return self.evaluate(item)

    def get_version(self) -> str:
        version = self._version
        if version is None:
            version = self._raw("GetCodeVVersion")
            self._version = version
        return version

    def get_surface_count(self) -> int:
        return int(self._raw("GetSurfaceCount"))

    def get_dimension(self) -> int:
        return int(self._raw("GetDimension"))

    def get_stop_surface(self) -> int:
        return int(self._raw("GetStopSurface"))

    def get_zoom_count(self) -> int:
        return int(self._raw("GetZoomCount"))

    def get_field_count(self) -> int:
        return int(self._raw("GetFieldCount"))

    def get_wavelength_count(self) -> int:
        return int(self._raw("GetWavelengthCount"))

    def get_max_aperture(self, surface: int, zoom: int = 1) -> float:
        return float(self._raw("GetMaxAperture", surface, zoom))

    # -------------------------------------------------------- async commands

    def async_command(self, text: str) -> None:
        """Start a command without waiting for it to finish.

        Only one asynchronous command may run at a time and its result is read
        with get_command_output. Nothing that could reset that buffer may run in
        between, so the caller must not issue other calls while a task runs.
        """
        if "\n" in text or "\r" in text:
            raise ParameterError("A command may not contain newlines.", details={"command": text})
        self._note(f"async command {text!r}")
        self._raw("AsyncCommand", text)

    def is_executing_command(self) -> bool:
        """True while an asynchronous command is still running."""
        return bool(int(float(self._raw("IsExecutingCommand"))))

    def wait(self, seconds: int) -> int:
        """Wait for the running command: 0 means completed, 1 means timed out."""
        status = self._raw("Wait", int(seconds))
        try:
            return int(float(status))
        except (TypeError, ValueError) as exc:
            raise InternalError(
                "Wait did not return a status.", details={"value": status}
            ) from exc

    def get_command_output(self) -> str:
        """Output of the last completed command, limited by the text buffer size."""
        return self._raw("GetCommandOutput")

    def stop_command(self) -> None:
        """Abort the running calculation."""
        self._raw("StopCommand")
        self._note("StopCommand sent")

    def output_is_truncated(self, text: str) -> bool:
        """True when captured output filled the text buffer and may be cut off."""
        return len(text) >= max(self.text_buffer_size - 1, 1)

    # ---------------------------------------------------------- array methods

    @staticmethod
    def _normalise_array_result(raw: Any) -> tuple[float, list[float]]:
        """Split a COM call result into (return value, filled array values).

        pywin32 returns (retval, out_array) for a function that takes a VARIANT
        by-reference array, as verified for BufferToArray in Phase A.
        """
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            try:
                return float(raw), []
            except (TypeError, ValueError):
                return float("nan"), []
        retval, payload = raw
        values: list[float] = []
        if isinstance(payload, (tuple, list)):
            for item in payload:
                if isinstance(item, (tuple, list)):
                    values.extend(float(value) for value in item)
                else:
                    values.append(float(item))
        elif payload is not None:
            values = [float(payload)]
        return float(retval), values

    def mtf_1fld(
        self,
        zoom: int,
        field: int,
        frequency: float,
        azimuth: float,
        nrd: int,
        mtf_type: int = 0,
        mtf_wave: int = 0,
    ) -> tuple[float, list[float]]:
        """Call MTF_1FLD and return (modulation, six data values).

        The caller supplies the frequency grid; the six values are modulation,
        phase in degrees, analytic diffraction limit, actual diffraction limit,
        illumination and rays traced.
        """
        obj = self._require_object()
        self._check_engine()
        self.call_count += 1
        values = [0.0] * 6
        started = time.perf_counter()
        raw = self._invoke(
            obj,
            "MTF_1FLD",
            int(zoom), int(field), float(frequency), float(azimuth), int(nrd),
            values, int(mtf_type), int(mtf_wave),
        )
        elapsed = time.perf_counter() - started
        if elapsed > 1.0:
            self._note(f"MTF_1FLD(field {field}, {frequency}) took {elapsed:.2f}s")
        return self._normalise_array_result(raw)

    def rayrsi(
        self, zoom: int, wavelength: int, field: int, input_values: list[float]
    ) -> float:
        """Trace one ray aimed by CODE V at relative pupil coordinates.

        The return value is 0 on success and the failing surface otherwise.
        RAYRSI does not check apertures. The ray data are not returned: they
        stay in CODE V and are read with database items such as (X S1), which
        is why the caller has to read them before any other ray is traced.
        """
        obj = self._require_object()
        self._check_engine()
        self.call_count += 1
        inputs = [float(value) for value in input_values]
        if len(inputs) != 4:
            raise InternalError("RAYRSI needs exactly four input values.")
        raw = self._invoke(obj, "RAYRSI", int(zoom), int(wavelength), int(field), 0, inputs)
        status, _ = self._normalise_array_result(raw)
        return status

    def raytra(
        self, zoom: int, wavelength: int, aperture_check: int, input_values: list[float]
    ) -> tuple[float, list[float]]:
        """Trace one ray and return (status, eight output values).

        The input is four numbers: the coordinates on the first tangent plane
        and the direction tangents in object space. The output is the image
        surface coordinates X, Y, Z, the direction cosines L, M, N, the optical
        path length and the transmission.
        """
        obj = self._require_object()
        self._check_engine()
        self.call_count += 1
        inputs = [float(value) for value in input_values]
        if len(inputs) != 4:
            raise InternalError("RAYTRA needs exactly four input values.")
        outputs = [0.0] * 8
        raw = self._invoke(
            obj, "RAYTRA", int(zoom), int(wavelength), int(aperture_check), inputs, outputs
        )
        return self._normalise_array_result(raw)
