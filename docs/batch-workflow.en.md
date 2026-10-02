# Batch Lens Reports

[中文](batch-workflow.md)

`python -m codev_mcp.batch` analyzes serially through the public MCP tools. The first `--lens` is the reference lens, and every other lens is compared with it one by one; each pair uses its own comparison package that is never overwritten, and inputs get stable identifiers generated from their absolute paths.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
python -m codev_mcp.batch --lens D:\lenses\base.len --lens D:\lenses\candidate-a.len --lens D:\lenses\candidate-b.len --analyses first_order,spot_diagram,mtf --fields 1 --mtf-frequencies 0,20,40 --spot-grid 5
```

`--backend` defaults to `com`; `--output-dir` defaults to `.codev-run/batches`; `--timeout`, `--allow-field-weight-difference` and the analysis configuration options are the same as in the [two-lens workflow](comparison-workflow.en.md). The simulated backend only picks built-in samples by file name and does not read the optical data of input `.len` files.

Each run creates its own directory. `batch.json` stores the input hashes, the status of each pair, the paths of the detailed packages, and the numeric differences for the same request and the same actual settings; `batch.md` shows these differences and links to each pair's `comparison.md`. Metrics record `unit`; MTF additionally records `frequency` and `frequency_unit=cycles/mm`. Lengths and spot radii use the lens units of the result, WAV RMS uses `waves`, and F-number, Strehl and MTF modulation use `1` (shown as "dimensionless" in the tables). The first reference lens is recomputed independently in every pair.

When two lenses are incompatible or an item fails, that pair is marked failed and left out of the numeric summary, and the following lenses keep running; any failed pair or changed input gives the batch exit code 1. When the final input recheck finds a missing or unreadable file, the manifest records `unchanged=false` and the error, and a failure report is still produced. A user interrupt stops the remaining lenses, saves `status=interrupted`, and the command exits with 130; the session cleanup state of the current pair is given by its own comparison package. There is no ranking or overall score across different settings.

Multi-zoom numeric analysis can select positions explicitly with `--zoom-positions 1,2`, limited to first-order, spot and numeric MTF. WAV and CODE V native plots still support single-zoom lenses only.
