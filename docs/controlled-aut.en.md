# Controlled AUT Candidates and Explicit Acceptance

[中文](controlled-aut.md)

The entry point is the standalone command line `python -m codev_mcp.aut`. It builds its own committed lens checkpoint inside a new run directory and does not take over a lens opened through MCP or the user's CODE V GUI. `prepare` only produces a candidate; after reviewing `result.json`, the caller must run `accept` separately to publish that candidate as revision 1 of the run directory. There is no automatic acceptance option.

Requests use a typed **AUT specification**: several stages run in order, each with its own variables, whitelisted constraints, general thickness constraints, error function parameters and typed weight changes. The single-variable options still work and are converted internally into a one-stage specification.

## Usage

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m codev_mcp.aut prepare --lens <work dir>\start.len --output-dir <work dir>\new-aut-run --spec docs\design\aut-spec-dbgauss-stages.json
.\.venv\Scripts\python.exe -m codev_mcp.aut accept <work dir>\new-aut-run\result.json
```

Single-variable usage (1 to 8 cycles, 5 to 300 seconds) is unchanged: `--surface 1 --parameter radius --lower 25 --upper 80 --target 0 --cycles 1 --wall-seconds 90`, mutually exclusive with `--spec`. Example specifications: [`aut-spec-dbgauss.json`](design/aut-spec-dbgauss.json) (one stage with 12 variables, EFL/OAL/IMD and MNT/MNE) and [`aut-spec-dbgauss-stages.json`](design/aut-spec-dbgauss-stages.json) (monochromatic → add spectral lines → polish). All numbers are demonstration values: these examples follow the surface order of the 11-surface double Gauss in the simulated backend's dbgauss model, while AUT runs only on the real backend, so replace it with your own lens with the same surface order and adjust surface numbers and values to the actual lens.

## Specification (schema 1)

```json
{"schema_version": 1, "kind": "aut_spec", "name": "...", "wall_seconds": 600,
 "stages": [{"name": "mono",
   "lens_changes": [{"target": "wavelength", "wavelength": 1, "parameter": "weight", "value": 0}],
   "variables": [{"surface": 1, "parameter": "radius"},
                 {"surface": 2, "parameter": "thickness", "lower": 0.1, "upper": 5}],
   "constraints": [{"operand": "EFL", "relation": "=", "value": 100, "tolerance": 0.001},
                   {"operand": "DIY", "field": 3, "relation": "<", "value": 0.005}],
   "general_constraints": {"MNT": 2, "MNE": 1},
   "error_function": {"MXC": 30, "MNC": 1, "TAR": 0, "IMP": 0.05},
   "set_vignetting": false}]}
```

- `wall_seconds`: parent-process wall-clock limit for the whole candidate generation, 5 to 1800, default 600. Each stage's `TIM` takes its value in minutes as a secondary limit.
- `variables`: `radius` or `thickness` of existing ordinary surfaces, with optional bounds (`RDY/THI Sn > a < b`). Starting values may lie on or outside a bound; CODE V can push the variable inside, and `start_bounds` in the stage record notes "on bound/outside bound" item by item. A surface that starts flat (infinite radius) can be a `radius` variable; AUT changes the curvature, and a flat start allows no `lower`/`upper` (when the curvature passes zero the radius changes sign, so radius bounds have no definite meaning). Variable records carry `before_infinite`/`after_infinite`, in which case `before`/`after` are `null`, and read-back comparison allows the infinity flag of a radius variable to change. Parameters controlled by a solve or pickup, infinite thicknesses and the object/image surfaces cannot be variables; image defocus (`THI SI`) is not open.
- `constraints` (whitelist): `EFL`, `EFY`, `OAL` (optional `surfaces: [i, j]`, default S1..I-1), `IMD` (image distance including defocus; a specification's "back focal distance" maps here), `DIY` (`field`, a **fraction** of the field height), `CT`/`ET` (`surface`). `relation` is `=`, `<` or `>`; a `>` and a `<` on the same quantity are merged into one two-sided command. `tolerance` is optional and defaults to 1e-6 relative to the target (at least 1e-6 absolute).
- `general_constraints`: `MXT`, `MNT`, `MNE`, `MNA`, `MAE`, set only; CODE V does not print their values.
- `error_function`: `MXC` (1 to 500, required), `MNC`, `TAR`, `IMP`, `DEL`, `WTA`; the error function is fixed to the `ERR CDV` default transverse aberration.
- `lens_changes`: typed field data and wavelength weight changes before the stage starts, counted as lens changes in the candidate difference: field `weight` (`WTF Fn`, ≥0), `y_angle`/`x_angle` (`YAN`/`XAN Fn`, ±89°), vignetting factors `vux/vlx/vuy/vly` (-0.99 to 0.99), and wavelength `weight` (`WTW Wn`, integer 0 to 1000).
  - **Field weight example**: raise a field's weight temporarily in one stage and restore it in a later stage: in the first stage `{"target":"field","field":3,"parameter":"weight","value":2}`, and in the last stage write `"value":1` again (see [`aut-spec-dbgauss-ramp.json`](design/aut-spec-dbgauss-ramp.json)).
  - **Vignetting is not an AUT variable**: to change vignetting step by step, use typed changes such as `vuy` in `lens_changes`, or an explicit `set_vignetting` after a stage. `FAP` rewrites vignetting and lets the pupil scale change; this service does not open it.
- `set_vignetting`: runs `SET VIG` after the stage's AUT. It is refused when the lens has no explicit clear apertures: CODE V would assume the stop aperture and replace the design vignetting.

### Field ramp `field_ramp`

A wide-field lens is best not designed in one jump: start from a smaller field and increase the field angle step by step, each step continuing from the previous step's candidate. `field_ramp` in the specification is expanded into ordinary stages when it is read:

```json
{"field_ramp": {"name": "grow",
  "steps": [{"fields": [{"field": 2, "y_angle": 6}, {"field": 3, "y_angle": 9, "weight": 2}]},
            {"fields": [{"field": 2, "y_angle": 10}, {"field": 3, "y_angle": 14, "weight": 1}]}],
  "stage": {"variables": [...], "constraints": [...], "error_function": {"MXC": 15}}}}
```

- Each step produces a stage `<name>-<k>`: first that step's field changes are sent (`y_angle`, `x_angle`, `weight`, vignetting factors; at most 12 steps and 10 fields per step, with no repeated field in a step), then the template `stage`'s own `lens_changes`, and then it runs as an ordinary stage. It can be combined with `stages` (stages before the ramp, for example refocusing), and `field_ramp.then` holds ordinary stages that run after the ramp (for example a refinement with more variables); the total number of stages is limited to 20.
- Each step is a stage: it keeps its own command block, `ERR. F.` change, constraint judgement, candidate file `candidate-stage-<n>.len` and `ramp_step` in the record; if a step fails, the run stops and the candidates of earlier steps and `last_good_stage` are kept.
- **Intermediate steps are only chained candidates within the run and are not published**: the candidate of the last step of the whole run can still only be accepted explicitly with `accept`.
- In the result, `spec` is the expanded ordinary specification and `spec_input` keeps the original (with `field_ramp`); the specification file hash refers to the original bytes. Examples: [`aut-spec-dbgauss-ramp.json`](design/aut-spec-dbgauss-ramp.json), [`aut-spec-wideang-ramp.json`](design/aut-spec-wideang-ramp.json). A small-field starting lens can be generated from your own starting lens with a `field_set` file for `python -m codev_mcp.edit` (see [field sets](field-set-and-seq-import.en.md)).

The SHA-256 of the specification's original bytes is written to the result. No free-form commands, macros, `IN` or user strings reach CODE V.

## How it runs

The baseline, the candidate, two WAV diagnostics and two reopen verifications at acceptance each call COM serially in a disposable child process; baseline/diagnostics/verification each have an external 120-second deadline, and the candidate is bounded by `wall_seconds`.

The candidate child process starts from a copy of revision 0 and, stage by stage:

1. Sends the typed weight changes; after `FRZ S0..I`, opens the variables one by one (`CCY`/`THC Sn 0`).
2. Runs a zero-cycle `AUT; ERR CDV; MXC 0; VLI Y; GO`: the set of parameters in the `VARIABLE LIST` must match the request exactly (bending combination rows that CODE V generates automatically are accepted).
3. Sends error function parameters, variable bounds, constraints and general constraints; after `OUT <service file>` it runs `GO` asynchronously, and on completion first takes `GetCommandOutput`, then `OUT T`, and reads the redirected file. A failed `OUT T` is treated as AUT not having returned to the prompt: no further command is sent, and only processes recorded for this session are cleaned up.
4. Parses the file: it must contain a `Normal AUTO Completion` line and a per-cycle `ERR. F.`; specific constraints read target/value/diff (6/6/4 significant digits) from the last cycle that has a constraint table and are judged one by one as `satisfied`/`violated`/`unknown` (when the print precision straddles the tolerance); active general constraint names and `Frozen Thickness Violations` warnings are recorded.
5. Restores to their original values only the numeric control codes changed by `FRZ` and opening the variables (for example `THC Sn 0` in the original lens); solve codes such as `PIM` are left alone.
6. Reads back a snapshot: only this stage's variables, solve-controlled parameters (for example the `PIM` image distance) and this stage's weight changes (and the vignetting factors of `SET VIG`) may change; any other change fails the stage. General constraints are rechecked independently by the service against the center/edge thicknesses of the [specification judgement](design/design-spec.en.md#specification-judgement) (covering all elements; the edge definition differs from CODE V's and this is noted). The recheck uses the same default tolerance as specific constraints (1e-6 relative to the limit, at least 1e-6 absolute), because when CODE V pushes a thickness onto a bound the read-back value can differ by about 1e-15; the result lists the limits and tolerances.
7. Saves `candidate-stage-<n>.len`, reloads it and verifies the snapshot; the next stage starts from this file.

When a stage fails, later stages stop, the candidates already saved by earlier stages and `last_good_stage` are kept, the result is `failed`, and it cannot be accepted. After all stages succeed, first-order analyses before and after and an independent WAV diagnostic run.

## Results and acceptance

`result.json`: `state`, each stage's `native_commands` (the command block actually sent, for the design record to reproduce), the redirected output file and its hash, `completion`, first and last `ERR. F.`, variables before and after with in-bound judgement, `solve_coupled` (`PIM` image distance before and after), each constraint, the general constraint recheck, weight changes and stage candidates; overall `target_reached` (last stage `ERR. F.` ≤ `TAR`), `all_constraints_satisfied`, `final_constraints_satisfied`, `explicit_bounds_satisfied`, `relations` (the solves/pickups kept and the parameters they control), and first-order data and WAV before and after. **When constraints are not met, the candidate is still saved and can be accepted, but the result marks this clearly; it does not mean the requirements are met.**

`accept` rechecks the source lens hash, candidate hash, original revision and candidate snapshot; the allowed differences are re-derived from the specification (each stage's variables, solve-controlled parameters, typed weight changes), and any other difference is refused. It then copies the candidate as the single revision 1, verifies two reopens, and publishes `current.json` under a lock; the result gains `accepted_path`. Both prepare and accept are written to the [execution record](design/execution-record.en.md) (steps `aut` and `aut_accept`). Stale, modified or repeatedly accepted candidates, and faults before publishing, are all refused; a commit whose pointer has already been updated is not overturned by later diagnostic errors. `audit.jsonl` records preparation, failures and acceptance. The source lens is never overwritten.

## Limits

- Non-`PIM` solves and pickups are let through according to their control columns and listed item by item, and their relation to the requested variables must be fully verifiable; allowing relations to remain does not mean arbitrary solve structures are supported.
- `SET VIG` does not guarantee that the original design vignetting or pupil is kept; it runs only when explicitly selected and the aperture precondition holds. The candidate's vignetting and pupil changes need a separate check.
- Multi-zoom, glass variables, aspheres, image defocus, `IMC`/`TT`/ray constraints, user-defined error functions and `WTC` are not supported.
- On a candidate timeout or interrupt, the current session is discarded. Preparation, diagnostics and acceptance verification each have their own process deadline; a command timeout must not be taken as a confirmed cancellation.
- The checkpoint format is 6, including the native `CCY/THC/GLC` control codes and field vignetting factors; old formats 1 to 5 are not recovered automatically.
