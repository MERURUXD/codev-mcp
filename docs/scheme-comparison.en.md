# Scheme Comparison

[中文](scheme-comparison.md)

The standalone CLI `python -m codev_mcp.schemes` runs several design schemes in a batch starting from the **same baseline lens** (glass swaps, changed vignetting or field set, different constraint sets). Each scheme is "typed changes → staged AUT → candidate", and at the end the results are summarized with a list of which quantities can be compared directly and which cannot. It only recommends and **never accepts any candidate**; adopting a candidate still requires running `python -m codev_mcp.aut accept` separately. It adds no MCP tool and sends no new CODE V commands: changes are made by [`codev_mcp.edit`](field-set-and-seq-import.en.md) (one `update_lens` transaction), optimization by [`codev_mcp.aut`](controlled-aut.en.md) (including field ramps), and the optional specification judgement by [`codev_mcp.compare`](comparison-workflow.en.md).

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json --output-dir <work dir>\new-run [--design-spec spec.json]
.\.venv\Scripts\python.exe -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json --design-spec spec.json --max-analyses-per-session 8 --jobs 2 --output-dir <work dir>\new-fast-run
.\.venv\Scripts\python.exe -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json --screen-cycles 2 --finalists 1 --output-dir <work dir>\new-screened-run
```

## Scheme file (schema 1)

```json
{"schema_version": 1, "kind": "scheme_set", "name": "...",
 "schemes": [
   {"name": "schott", "reason": "baseline"},
   {"name": "cdgm", "edits": [{"target": "surface", "surface": 4, "parameter": "glass", "value": "HF1_CDGM"}]},
   {"name": "vignetted", "field_set": {"fields": [{"y_angle": 0}, {"y_angle": 10, "vuy": 0.3}]}},
   {"name": "loose", "aut_spec": "aut-loose.json"}
 ]}
```

- 1 to 8 schemes, with names of 1 to 24 letters, digits, dots, underscores or hyphens, no duplicates. Each scheme has at most one kind of change: `edits` (1 to 100 public `ParameterEdit` records) or `field_set` (public field set replacement); with neither, a copy of the baseline is optimized directly.
- `aut_spec` (a path relative to the scheme file) overrides the default `--aut-spec`, to compare different constraint sets; a specification can contain `field_ramp`. All specifications are validated before the run; a missing specification, a path that does not exist, an output directory that already exists or contains non-ASCII characters (required by CODE V 10.2) are all refused before any CODE V runs.
- By default schemes run serially in file order (one CODE V session at a time). `--jobs N` (1 to 8, the maximum number of schemes in a set) runs N schemes at once: each scheme does exactly what it would do serially, in its own worker process and CODE V session, and the summary still follows the order of the scheme file (see "Parallel runs" below). A failed scheme (change refused, AUT not completed, timeout) is recorded and the other schemes continue; if a scheme leaves a CODE V process that cannot be confirmed, no new scheme is started, schemes already running finish naturally, and the rest are marked "not run"; `Ctrl+C` keeps the completed part.

## Two-stage comparison (optional)

Giving both `--screen-cycles N --finalists K` first screens all schemes and then fully optimizes the finalists. Without them, every scheme still runs in full.

- `N` is an integer from 1 to 500: after expanding `field_ramp`, each stage's `MXC` takes the smaller of its original value and N, and `MNC` is limited accordingly; variables, constraints, the other error function settings, typed changes and the field ramp order are kept. Screening still performs AUT read-back, candidate save and reopen, and WAV diagnostics, but no `--design-spec` evaluation.
- `K` is an integer from 1 to 8 and means at most K finalists **per comparable error function group**, not a total across groups. Only among schemes whose screening completed with no cleanup doubts, variable bounds met and a finite final `ERR. F.`, finalists are chosen by ascending `ERR. F.` within the group; ties follow the scheme file order. Unmet specific constraints after a few cycles may improve with further optimization, so they do not eliminate a scheme directly. Groups with different settings keep candidates separately, and error functions are not compared across groups.
- The full stage reruns the scheme's typed changes and original AUT specification from the original baseline, each in its own session. The final specification evaluation and the recommendation use full-stage results only. The screening order may differ from the order after full optimization, and fewer full runs do not guarantee a shorter total time.
- When screening cleanup is in doubt, the run is interrupted or the baseline hash changes, the full stage does not start; with no valid finalist there is no recommendation. `--jobs` applies to each stage separately, and the full stage starts only after all screening has finished.

The two-stage directories are `screen/<scheme>/` and `full/<scheme>/`; parallel requests go into each stage's `_workers/`. `summary.json.screening` keeps the screening metrics, the sort basis, comparable groups and the finalist list; the summary table shows screening and full results side by side, schemes not selected are recorded as `screened_out`, and their screening candidates are not final candidates or recommendations. When the baseline has changed or cannot be verified, no acceptance suggestion is given.

## Parallel runs (`--jobs`)

`python -m codev_mcp.schemes ... --jobs 3` runs at most 3 schemes at once (limit 8, default 1).

- Each scheme is handed to its own worker process (`python -m codev_mcp.schemes _scheme <request file>`), which in turn calls `codev_mcp.edit` and `codev_mcp.aut` exactly as in a serial run, so each scheme's isolation, cleanup and interrupt behavior is unchanged. Requests, results and error output go into `_workers/` in the output directory.
- Session start-up is queued by a machine-wide mutex (one at a time), so each ownership record contains only its own processes; `cvcomsvr.exe` is shared by all concurrently running sessions and is not ended while another session still uses it.
- A worker process that exits without leaving a result is treated as "cleanup in doubt" and stops new schemes; after `Ctrl+C`, the run waits for worker processes to clean up themselves (180 seconds), and those still running are forcibly ended and marked in doubt. In a serial run, a scheme interrupted by `Ctrl+C` is recorded as `interrupted`, and the schemes after it do not run.
- Whether cleanup is in doubt is decided from `cleanup_confirmed`, `cleanup_remaining` and the structured state of each diagnostic/evaluation session. When the final ownership check confirms nothing is left over, a diagnostic timeout only fails that scheme and later schemes can continue; the word "cleanup" in an error message does not drive scheduling. Unconfirmed states of different sessions are still kept separately.
- Parallel sessions increase memory and license use, so choose `--jobs` according to the machine's resources. Replacing `edit_runner`/`aut_runner`/`evaluator` (for tests) works only with `--jobs 1`.

## Output

`--max-analyses-per-session 1..8` only controls the candidate evaluation of `--design-spec`, default 1. Both serial schemes and `--jobs` worker processes pass this option on. Each scheme still runs serially inside, and WAV/native plots keep the same state and release checks; when evaluation cleanup is unconfirmed, no new scheme is started. The result manifest and execution record keep the option; see the [performance workflow](performance-workflow.en.md).

The output directory contains `summary.md` (summary table, written in Chinese), `summary.json`, `execution-record.json`, and a subdirectory per scheme: `edits.json`, `start.len`, `edit/` (the run package of the change), `aut/` (the AUT run directory, with `result.json`, candidates and the execution record) and optionally `evaluation/`. The baseline lens hash must be the same before and after, otherwise the whole run is judged failed.

The summary table lists: status, number of stages, `ERR. F.` initial → final, WAV weighted RMS and Strehl (start → candidate), whether constraints and variable bounds are met, specification judgement (only with `--design-spec`, where each candidate gets a full evaluation), and the candidate's first-order data (in parentheses, the starting point after the scheme's changes and before optimization). All numbers are taken from each scheme's AUT result and evaluation package; only the accepted pupil ratio and the composite RMS without ray-count weighting are computed from the printed per-field values.

## Comparable and non-comparable

- **First-order data and specification judgement**: for the same specification, they can be shown side by side directly.
- **Error function (`ERR. F.`) and WAV weighted RMS/Strehl**: comparable only between schemes with "the same settings": the same field angles, field weights, vignetting factors, wavelengths and wavelength weights, reference wavelength and aperture (the error function additionally requires the same `DEL` and `WTA`). With different vignetting the error function cannot be compared directly: CODE V scales the optimization ray grid by the vignetting factors, so the pupil sampling is already different (WAV does not scale its grid; vignetting factors only affect which rays pass through the default apertures). When the condition is not met, the summary splits the schemes into groups with an explanation, and only schemes within a group are comparable.
- **WAV accepted pupil**: the WAV grid is fixed, the stop surface and default apertures block some rays, blocked rays are neither failures nor part of the RMS, and the composite RMS is weighted by each field's ray count. So the summary also lists the "WAV accepted pupil ratio" (each field's ray count divided by the ray count of field 1), CODE V's composite RMS and a composite RMS averaged directly by field weight; when any field's accepted pupil ratio differs from the first scheme of the same group by more than 3 percentage points, the scheme is put into a different pupil group, and the notes state that schemes with fewer rays have a smaller RMS and are only directly comparable within their group. The recommendation is still chosen by the original rule, but when the chosen scheme's pupil differs from the other comparable schemes, a "Note" is added below the recommendation.

## Recommendation

Within the **largest group** whose error functions are comparable, only schemes with AUT completed, all specific constraints met, variable bounds met and a specification judgement other than "fail" are considered, and the first by ascending WAV weighted RMS is taken (by `ERR. F.` when WAV is missing). With no qualifying scheme there is no recommendation, and the reason is written; there is also no recommendation when the largest group is not unique (for example two schemes with different vignetting, each in its own group), because picking a group by name has no basis. The recommendation comes with the acceptance command but does not run it; schemes outside the largest group are listed as "not compared" instead of being compared silently.

## Limits

It recommends one scheme by WAV weighted RMS only and makes no multi-objective trade-off; schemes share no AUT state. Evaluation time varies with sampling and the lens. A recommendation does not mean the candidate has been accepted or that the design meets every manufacturing requirement.
