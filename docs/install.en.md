# Installation and Troubleshooting

[中文](install.md)

This document covers how to install, configure and run the CODE V MCP service on the local machine, and the order in which to troubleshoot problems.

## 1. Requirements

| Item | Requirement |
| --- | --- |
| Operating system | Windows, local stdio connection only |
| CODE V | 10.2, with `CodeV.Command.102` registered and a working license |
| Python | 3.10 or later, 64-bit recommended |
| Dependencies | Pinned install in `requirements.lock.txt`; declared dependencies in `pyproject.toml` |

64-bit Python can drive the 32-bit COM server; no 32-bit worker process is needed.

## 2. Installation

Run the following in the root of the downloaded or cloned repository, with `python` pointing to Python 3.10+:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m codev_mcp --backend simulated
```

The lock file records the release candidate dependencies (rpds-py and websockets use compatible branches on Python 3.10), and the editable install lets you run from the current source. If you do not install this package, you still need to install the dependencies first and then set `$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'`.

When installing the declared dependencies directly, the project is constrained to MCP 1.x and Pydantic 2.x; the server API changed in MCP 2.x and the current implementation has not been migrated.

For the real backend use `--backend com`. First confirm that CODE V is installed, the COM server is registered and a license is available.

## 3. Running

```text
codev-mcp [--backend {simulated,com}] [--working-directory DIR] [--python EXE] [--timeout SECONDS]
```

| Option | Description |
| --- | --- |
| `--backend` | Default `simulated`; `com` drives the real CODE V; `simulated` is for automated tests and its results are always marked simulated |
| `--working-directory` | The service's own working directory; CODE V sessions run in it, and restore points and result images are written there too (default `<repository>/.codev-run` for source/editable installs; for other installs, give a short writable path explicitly) |
| `--python` | Interpreter for the worker process, by default the same as the current process |
| `--timeout` | Timeout in seconds for a single tool call; a timeout terminates the stuck worker process |

Matching environment variables: `CODEV_MCP_BACKEND`, `CODEV_MCP_WORKDIR`, `CODEV_MCP_PYTHON`, `CODEV_MCP_TIMEOUT`.

stdout is used only for the MCP protocol; logs go to stderr.

## 4. MCP client configuration example

After installing in the project root, generate the absolute paths the configuration needs with PowerShell, so it does not depend on the maintainer's machine or on `PYTHONPATH`:

```powershell
$pythonPath = (Resolve-Path .venv/Scripts/python.exe).Path
$workPath = Join-Path (Get-Location).Path '.codev-run'
$config = @{mcpServers = @{codev = @{command = $pythonPath; args = @('-m', 'codev_mcp', '--backend', 'simulated', '--working-directory', $workPath)}}}
$config | ConvertTo-Json -Depth 6
```

Add the generated JSON to the client configuration. For the real backend change `simulated` to `com`; this needs Windows, the CODE V 10.2 COM registration and a working license. A working directory serves only one service instance at a time; when several clients connect at once, give each configuration a different `--working-directory` (see 6.11).
You supply real lenses yourself as absolute `.len` paths to `open_lens`; no vendor samples are needed. With a wheel install outside the source directory, the MCP service can use the same configuration, and the working directory must be writable. The engine working directory of the standalone command-line tools (`compare`, `batch`, `scan`, `scale`, `edit`, `seq_import`, `export_seq`, `glass`, `glass_near`, and `schemes`, which calls `edit`) is derived from the package install location, and `--output-dir` does not change it; with a wheel install it lands in `.codev-run` under the Python environment's `Lib`, which may not be writable. To use these command-line tools, do an editable install in the repository root as in section 2.

The service's `serverInfo.version` comes from the MCP SDK; the service version is in `get_status.service_version`.

## 5. Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe examples/simulated_quickstart.py --output .codev-run/demo
```

The simulated workflow checks the installation and the MCP connection; it does not evaluate real optical performance.

## 6. Troubleshooting

Work out which layer the problem is in, in this order.

### 6.1 The client cannot connect, or the tool list is empty

1. Run `python -m codev_mcp --backend simulated` by hand and confirm the process starts and writes nothing to stdout (stdout is taken by the protocol).
2. Check that the client configuration uses the interpreter where this project is installed, and that the absolute paths exist.
3. The `warnings` of `get_status` say whether the worker process failed to start, and why.

### 6.2 A tool returns not_ready

This means the background worker process is unavailable. The `warnings` of `get_status` and the error's `details.stderr` hold the last lines of the worker's stderr. Common causes: missing dependencies, a wrong interpreter path, or the worker process terminated after a call timed out (restarting the service is enough in that case).

### 6.3 Session start is very slow or never returns

- Start-up has a 120-second limit; past it an error is returned instead of waiting forever.
- The first start can take longer because of license initialization; if it exceeds the start-up limit, read the returned diagnostics.
- If it does not return for a long time, check whether a CODE V dialog has popped up (see 6.4).

### 6.4 An "Application Error" dialog appears and every call hangs

This is a crash of the CODE V engine itself (for example `0x40000015` or `c0000005`), and it happens **at random**: on the same machine with the same commands, one run succeeds and the next one crashes. After the crash the engine process stays in the process table with no threads, sitting on the "Application Error" dialog waiting for a click; the COM server is still alive, so later calls raise no error and simply block forever.

The service has the following safeguards against this; it cannot guarantee that every crash is recoverable:

1. **Start-up watchdog**: while `StartCodeV` runs, a thread watches newly appearing engine processes; as soon as it finds an engine with no threads it terminates it, the dialog disappears with it (you do not need to click "OK"), and `StartCodeV` returns right away.
2. **Start-up retries**: session start is attempted up to 4 times, each time cleaning up only processes confirmed to belong to this service before retrying; persistent failure returns diagnostics.
3. **Self-healing at run time**: when the engine dies while running, the next call recognizes it immediately (instead of waiting for the full timeout), discards the dead session, builds a new one, and restores the lens from the latest successful checkpoint and verifies its state; if recovery cannot be confirmed, lens operations stop.

If manual intervention is needed, do this in order:

1. Close the dialog.
2. Check PIDs and session ownership against `codev-mcp-session.json` in the working directory, and handle only leftover processes confirmed to have been created by this service; do not end processes in bulk by process name. If ownership cannot be confirmed, keep the logs first.
3. Restart the MCP service.

Other existing safeguards: if there is no engine process after start-up, a structured error is returned immediately; before each call the recorded engine process is checked for being alive (and for still having threads); before start-up, leftover `codev*.rec` files in the working directory are removed (a leftover recovery file makes `StartCodeV` block forever); a client call timeout terminates the stuck worker process.

How to tell that self-healing happened: `details.session_restarts` in `get_status` is the number of session rebuilds; anything above 0 means the engine died midway and was rebuilt automatically. After a rebuild, the analysis that was running is marked `failed` and must be resubmitted; existing result files are kept, and historical results are marked with `history_only`. The lens is restored from the checkpoint and verified; see 6.5 for details.

### 6.5 Engine exits midway and successful checkpoints

The service's promise: before `update_lens` reports success, the change has been written to an independent checkpoint and has passed "save → reload → compare item by item". So after the engine exits midway, the service restores from the latest successful revision rather than going back to the original lens file.

Checkpoint location and how to identify them:

```text
<working directory>/checkpoints/<backend instance UUID>/<lens UUID>/
    revision-000000.len / .json     # one per successful commit, never overwritten
    current.json                    # the only file allowed to be replaced: the "successful revision pointer"
    restore-points/<rp-NNNN>.len    # restore point before each batch
    transactions/<tx-NNNNNN>.json   # transaction records (state, failure reason)
```

- The only authority is `current.json` (it holds the format version, lens id, revision number, file size and SHA-256, and the source file path and hash). Do **not** guess the "latest revision" from file modification times or names.
- `get_status.details` gives `lens_state` (`empty`/`ready`/`updating`/`recovering`/`invalid`), `lens_id`, `committed_revision`, `checkpoint_path`, `recovery_count`, `last_recovery`.
- To open a committed revision explicitly: read `lens_file` from `current.json` and call `open_lens` on that file (it gets its own record as a new working copy and does not change the historical revision).
- When recovery cannot be confirmed (missing file, hash mismatch, inconsistent read-back), `lens_state` becomes `invalid` and every operation that relies on a trusted lens (read, edit, analyze, save as) is refused until the service is restarted; the service does not fall back to an older revision or the original lens file.
- After the worker process or the whole service exits, checkpoints, transaction records, logs and result files are kept, but the work **does not resume automatically**: after restarting the service you must call `open_lens` again.

### 6.6 A long session fails midway

A CODE V session that runs for a long time can exit midway, and call overhead varies with the analysis settings. Work in segments and re-establish the session when needed; see [long-session stability](capabilities.en.md#3-limits-and-caveats).

A successful `open_lens` has already created the revision 0 checkpoint, and each later successful `update_lens` advances the revision; as long as the current checkpoint is valid, the next call discards the dead session and automatically restores the lens from the latest successful revision (see 6.5). If no checkpoint has been committed yet, there is no revision to restore (the service does not fall back to the original lens file); restart the service and call `open_lens` again.

### 6.7 The session is reported invalid (session_invalid)

This happens in two cases: a rollback failed and could not be confirmed, or `StopCommand` was not confirmed. Do not keep relying on the current lens state; when recovery cannot be confirmed, read, edit, analyze and save as are all refused. Use `get_status` to see the diagnostics, then restart the service and reopen the lens.

### 6.8 A parameter error is reported but the parameters look fine

Decide from the error `details`:

- Parameters controlled by a solve or pickup are refused (for example an image distance controlled by `PIM`).
- Edits on a multi-zoom lens must give `zoom_position`; if the parameter itself is not zoomed, the service changes the shared value and says so in `warnings`.
- Aperture edits are only supported on single-zoom lenses. The system aperture uses `target="aperture"`, `parameter="value"` and keeps the existing EPD/FNO/NA/NAO type; a surface aperture uses `parameter="clear_aperture_radius"` and only changes an existing, single, centered, circular explicit clear aperture radius. The old names `semi_aperture` and `clear_aperture`, automatic apertures, obscurations and compound definitions are all refused.
- Glass names may contain only letters, digits, dots, plus, minus and underscore; paths must be absolute and end in `.len`.

When the solve probe itself fails (the engine exits midway, for example), the edit is still attempted and `warnings` in the result say "the solve probe could not be read"; read-back verification then acts as the safety net.

### 6.9 A file conflict is reported

Save as refuses to overwrite the source file and existing target files; choose a new file name.

### 6.10 Leftover processes and license seats

Each time a session starts, the service records the CODE V processes created by that session in `codev-mcp-session.json` in the working directory; the next start first cleans up these processes confirmed to belong to the service. A normal shutdown (`close_session` or client disconnect) stops the session automatically. A CODE V GUI the user started is never touched or ended by the service.

When several sessions run at once (`schemes --jobs`): session start is queued by the machine-wide mutex `Local\codev-mcp-session-start`, waiting at most 600 seconds; `cvcomsvr.exe` is the COM server shared by all concurrently running sessions, and although the session that started first records it, stopping and cleanup never end it while another session still uses it.

### 6.11 The working directory is reported in use by another session

The error is `not_ready`, `details.reason` is `working_directory_in_use`, and `details.holder` gives the PID and start time of the process holding the directory. Start-up cleanup removes recovery files in the working directory and ends the processes recorded in `codev-mcp-session.json`, which is only valid for an old session that has already exited; so a session holds the working directory exclusively from start to stop (lock file `codev-mcp-session.lock`), and another service instance starting in the same directory is refused straight away, without cleanup and without retrying within that call. The refused service has not started a session and does not become invalid; once the directory is released, calling again starts it, with no service restart needed.

- When several MCP clients (for example two conversations open at once) each start a service, configure a different `--working-directory` or `CODEV_MCP_WORKDIR` for each; the default directory of a source install, `<repository>/.codev-run`, can serve only one service.
- If the process for `details.holder.pid` no longer exists, the lock was released with it; just call again, and the new session cleans up the processes it left according to the record.
- If the holder is still running, first call `close_session` in its client or close that client, then call again.
- It is normal for the lock file to stay in the directory; do not delete it by hand. Whether the directory is in use depends on whether the holding process is alive, not on whether the file exists.
