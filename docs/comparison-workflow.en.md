# Initial/Final Lens Analysis Comparison

[中文](comparison-workflow.md)

`python -m codev_mcp.compare` is a command-line client of the public MCP tools and adds no public tool. It does not execute arbitrary CODE V commands, does not optimize, does not refocus and does not modify input files.

## Usage

In the repository root, set `PYTHONPATH=src` and use a Python that has this project's dependencies installed:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
python -m codev_mcp.compare --initial D:\lenses\initial.len --final D:\lenses\final.len
```

- `--backend com|simulated`: default `com`. The simulated backend picks a built-in sample by file name and **does not parse real `.len` content**; it only checks the workflow.
- `--output-dir`: default `.codev-run/comparisons` in the repository; each run creates a new subdirectory with a unique name.
- `--timeout`: seconds per analysis, default 300. Single calls inside the service use the same limit; protocol wrap-up and session release have their own waiting time.
- `--allow-field-weight-difference`: off by default. When turned on explicitly, the initial and final field weights may differ; the differences are listed, only per-field comparison is done and no weighted overall score is computed. Field coordinates, numbers, wavelengths and their weights must still match.
- `--analyses`: comma-separated subset of `first_order,spot_diagram,mtf,wavefront,native_plot`; all analyses run when omitted.
- `--fields`: comma-separated field numbers, used only for first-order, spot and numeric MTF. WAV and native plots must use the lens's own full field settings, so selecting them together with this option is refused before the engine starts.
- `--mtf-frequencies`: comma-separated, non-negative, unique, ascending frequencies in cycles/mm, default `0,10,...,100`; `--spot-grid` defaults to 7.
- `--zoom-positions`: comma-separated zoom positions. When given, only first-order, spot and numeric MTF run by default; explicitly including WAV or native plots is refused. Every result and table states the position; the other positions are also read and the public optical state checked before and after the analyses.
- `--max-analyses-per-session`: default 1, meaning one session per item; an explicit 2 to 8 reuses a session, bounded, for consecutive analyses of the same lens. All five analysis kinds on single-zoom lenses support it, including WAV, the five native plot types and full `--spec` evaluation; multi-zoom reuse is still refused before the engine starts. Preflight and different lenses never share a session. Performance and safety boundaries are in the [performance workflow](performance-workflow.en.md).

The default workflow still supports single-zoom lenses only; multi-zoom numeric orchestration is enabled only by explicitly using `--zoom-positions`. Units must match. The initial and final lenses each keep the focus, aperture and weights they were saved with; the lenses are not automatically "aligned" or modified. Other parameters such as aperture may differ and are kept in the lens snapshots and first-order data, so the comparison is not automatically a same-conditions judgement of image quality.

For example, with initial weights `1/1/1/1/1` and final weights `1/1/1.2/1/1.8`, you must explicitly use `--allow-field-weight-difference`; the default strict mode refuses this pair of lenses.

## Design specification evaluation package

The same entry point supports single-lens evaluation and an evaluation configuration driven by a [design specification](design/design-spec.en.md):

```powershell
python -m codev_mcp.compare --lens D:\lenses\design.len --spec docs\design\design-spec-dbgauss.json
python -m codev_mcp.compare --initial initial.len --final final.len --spec my-spec.json
```

- Use either `--lens` or `--initial/--final`. A single-lens package writes `report.md`, a two-lens package still writes `comparison.md`, and the other outputs have the same structure.
- `--spec` decides the analysis kinds, structured MTF frequencies and spot grid, and cannot be combined with `--analyses`, `--fields`, `--mtf-frequencies`, `--spot-grid` or `--zoom-positions`; specification evaluation is limited to single-zoom lenses and all fields. The specification is validated before the engine starts, its original bytes are copied into the package as `design-spec.json` with the SHA-256 recorded, and at the end the source file and copy are rechecked as unchanged. If the lens units do not match the specification, or a requirement refers to a field that does not exist, the run fails after preflight and before any analysis.
- Preflight writes the native `LIS` text that `get_lens` read, verbatim, to `<stage>/preflight/lis.txt`, and the manifest entry `inputs.<stage>.listing` records its path, size and hash. This is a read-only export and sends no extra command; on the real backend, missing text fails preflight, and the simulated backend records it as missing.
- `figures` in the manifest: the origin of each plot (CODE V native or redrawn by the service from numbers), the image hash, and the matching numeric snapshot, native text or `.PLT`, for the design record to cite. The native MTF plot still fixes `MFR 100; IFR 10`; structured MTF uses the specification frequencies, and the two are not claimed to share sampling.
- After all analyses finish (including failures), each lens is judged against the specification, written to `evaluation.json`, and the per-item judgements are listed at the end of the report. Run status and specification judgement are separate: if all analyses succeed and the judgement is fail, the exit code is still 0; if an analysis fails, the judgement is still produced and missing items are unknown.

Judgement rules, preset sources, and the distortion and edge thickness conventions are in the [design specification format](design/design-spec.en.md#specification-judgement).

## Analyses and traceability

For each lens, first-order data, a spot diagram per field, full-field diffraction MTF, and the five native plots layout, spot, mtf, ray_aberration and field_aberration run serially. The spot grid is 7; structured MTF frequencies are 0 to 100 cycles/mm in steps of 10, returning tangential/sagittal curves. All wavelengths take part.

By default each analysis starts its own MCP service and worker process and opens this run's input snapshot; with explicit bounded reuse, one session runs at most the given number of analyses. In both modes, the optical fields exposed by the public `LensData` are checked before and after each analysis with absolute tolerance `1e-9` and relative tolerance `1e-7`, consistent with the existing parameter read-back; paths, titles, raw listings and warnings are excluded. Exact fingerprints are kept too, and equality within tolerance does not require identical fingerprints. This check **cannot prove that no undisclosed internal state of CODE V changed**.

The engine working directory is a short ASCII path under `.codev-run/c-<unique id>/` in the repository, and the output directory can be set independently. The source files, the input snapshots in the output and the internal snapshots all have their SHA-256 rechecked at the end.

`manifest.json` has `schema_version=1` and contains:

- Run status, source, time, code commit and whether the working tree had changes.
- The original paths of both inputs, relative snapshot paths, hashes and the unchanged check at the end.
- For each item: the request, actual settings, backend lens ID/revision, task number, before/after state fingerprints, warnings and elapsed time.
- For each output: its relative path in the package, size and SHA-256; missing backend information is left empty.
- `performance.sessions`: per-session client initialization, engine start, lens open, release and COM call counts; per-analysis wall-clock time for computation, export and read-back. The simulated backend provides no real COM data.

The initial and final directories each keep the inputs, lens JSON before and after analysis, status, tasks, raw output, PNGs, real native `.PLT` files, the MCP protocol log, stderr and the session release record. MCP image content must match the local image byte for byte, and copied outputs can be rechecked against the manifest hashes. Simulated native plots have no `.PLT`, and none is faked.

`comparison.md` contains numeric tables and links to outputs. Spot statistics always use **radius**, with native statistics and plotting sample counts in separate columns. First-order data carries precision notes; native plots and plots the service redraws from numbers are labeled separately and are not claimed to share sampling. WAV uses the saved current focus, all fields and wavelengths, and fixed `NRD=20`; per field it lists native RMS (waves), Strehl, actual ray count and accepted pupil ratio (ray count divided by the ray count of field 1; rays blocked by the stop surface and default apertures are not trace failures and are not in the RMS). When the initial and final accepted pupil ratios differ by more than 3 percentage points, the report marks "pupil differs" and shows the composite RMS without ray-count weighting alongside. The lens reference wavelength and the multi-wavelength RMS equivalent wavelength, which the native output does not provide, are labeled separately. When field weights differ and are explicitly allowed, only per-field values are compared, not composite values. The workflow does not judge whether the diffraction limit is reached.

## Failures and reruns

When an analysis task itself fails (`computation_failed`, `unsupported`, `parameter`, or truncated output) and the session has closed normally with cleanup confirmed, that item is recorded as failed in the manifest's `failed_analyses` (stage, analysis name, error type, reason) and the remaining analyses run as usual; the overall status is failed with exit code 1, the report and design record list the failed items, and quantities missing from the specification judgement are unknown. Task mismatch, historical results, inconsistent images, a changed lens state, an invalid session (`session_invalid`/internal error), protocol or timeout faults, and a release that cannot be confirmed still stop the run at that point, keep the outputs already produced, and end with exit code 1. A successful run exits with 0.

An asynchronous analysis that runs out of time is first cancelled and its confirmation state saved; a broken connection or a synchronous call timeout is released by closing the client and the existing service lifecycle. An unconfirmed cancellation is never recorded as success. Only services started by this run are closed, and CODE V processes are never terminated by name.

A rerun always creates a new package, does not reuse earlier successful items and does not overwrite old packages. `elapsed_seconds` is the per-item orchestration wall-clock time; in reuse mode, start-up and release are counted only for their group, and stage times are in `performance`.
