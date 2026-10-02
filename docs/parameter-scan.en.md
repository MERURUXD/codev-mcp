# Fixed-Grid Parameter Scan

[中文](parameter-scan.md)

`python -m codev_mcp.scan` uses the public MCP tools to scan one existing surface radius or thickness of a single-zoom lens. Each sample opens its own copy of the same baseline snapshot in its own service session, edits it in a transaction and reads it back, then runs a first-order analysis at the current focus. It does not run CODE V AUT, does not pick or publish a best lens and does not modify the input file.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m codev_mcp.scan --lens D:\lenses\singlet.len --surface 1 --parameter radius --values 60,61
```

`--backend` defaults to `com`; `simulated` only checks the orchestration and its results have no optical meaning. `--timeout` defaults to 300 seconds; `--output-dir` defaults to `.codev-run/scans`. `--values` must be 2 to 16 distinct finite numbers; one scan changes only one parameter of one surface, and multi-zoom, infinite baseline parameters and the object or image surface are not supported. Refused solve/pickup parameters are handled by the existing `update_lens` transaction safeguards; a single failed sample is marked on its own, and later samples still start from the original baseline.

Each run creates a new directory. `manifest.json` records the input hash, the grid, and each sample's status, read-back value, task ID and lens revision; `metrics.csv` and `scan.md` list the requested/read-back parameter, EFL, F/#, image distance and overall length. Each sample keeps its request, transaction result, analysis snapshot, lens read-back, MCP protocol log and cleanup record. Any failure, unconfirmed cleanup, or a changed or unverifiable input or snapshot hash fails the run with exit code 1; success returns 0. Metrics carry the units and precision notes of the first-order analysis, and detailed values and raw output are in each sample's `snapshot.json`.

The state check covers only the optical fields exposed by the public `LensData`. Solve coupling after a change is checked by the existing transaction read-back and checkpoint mechanism; before and after the analysis, the public optical state and the lens revision must not change. The scan table is not sorted by any metric and draws no conclusion about image quality.

The scan keeps the main error, cleanup errors and input check errors separately; an interrupt or unconfirmed cleanup stops the remaining samples, while an ordinary sample computation failure can continue once cleanup is confirmed. `interrupted`, `cleanup_unconfirmed` and `error_sources` in `manifest.json` record the corresponding reasons.

Use `--config` to enable a fixed-focus image quality scan: first-order, per-field spot, numeric MTF and WAV are selectable, and the output is a condition-bound metric table, curves and per-requirement evaluation. Without a configuration only first-order analysis runs by default. Real CODE V accepts only fixed-focus lenses confirmed to have no solve/pickup relations; when scanning an air gap, the medium after the target surface must be air and the next surface must not be the image surface. Explicitly selected samples are saved separately and recomputed after reopening in a separate session, and are marked deliverable only after that passes.
