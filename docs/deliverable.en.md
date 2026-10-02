# Deliverables

[中文](deliverable.md)

The standalone CLI `python -m codev_mcp.deliver` organizes evaluation packages and AUT runs that have already finished into report material, laid out with the directories and file names reports commonly use, and generates a material index. It only copies, checks hashes and lays out: **it recomputes no numbers, sends no commands to CODE V and does not write its inputs**, and the target directory must not exist (an existing one is refused).

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.deliver --comparison <work dir>\compare-bundle --aut <work dir>\aut-run --seq <work dir>\final.seq --output-dir <work dir>\deliverables --title "Double Gauss objective"
```

- `--comparison`: a run package from `codev_mcp.compare`, either an initial/final comparison or a single-lens evaluation (a single lens always uses the `final-` prefix).
- `--aut`: a run directory from `codev_mcp.aut`; can be repeated (from the second one on, file names carry `-2`, `-3`).
- `--setup`: the run directory of a typed edit (`codev_mcp.edit`), scaling (`codev_mcp.scale`) or `.seq` import (`codev_mcp.seq_import`) made before optimization; can be repeated. The equivalent commands from its execution record are written as `macro/setup-*.seq`, to be used before the optimization macro (for example the `GLA` commands of a glass swap).
- `--seq`: `.seq`/`.len` files to include as well; can be repeated.
- Give at least one of these; any combination works.

## Directory layout

| Location | Contents |
| --- | --- |
| `figures/init-*.png`, `figures/final-*.png` | Paired initial/final plots: CODE V native plots (layout, spot, mtf, ray_aberration, field_aberration, with the `.PLT` of the same name copied too) and the spot diagram and MTF redrawn by the service from numbers; file names follow report conventions (`init-`/`final-` + analysis name) |
| `lis/init-lis.txt`, `lis/final-lis.txt` | Each lens's native `LIS` text (the read-only output `get_lens` read during evaluation) |
| `wav/`, `first-order/`, `raw/` | CODE V native text output: raw WAV, first-order data, and raw spot and MTF text |
| `macro/setup-*.seq` | Equivalent commands of the edits/scaling/imports given with `--setup` (for example the `GLA` lines of a glass swap) |
| `macro/optimize-verbatim.seq` | The optimization command block the service actually sent (including the zero-cycle check, output redirection, control code restoration and intermediate saves), verbatim |
| `macro/optimize-equivalent.seq` | **Equivalent hand-written macro**: each stage keeps only field/weight changes, `FRZ` and opening variables, error function settings, variable bounds, constraints and one `GO` (plus `SET VIG` when the stage has it); the file header lists what was left out |
| `runs/` | Each stage's raw AUT output (the `OUT` redirected file) and `aut-summary.md` (stages, end reason, `ERR. F.` initial → final, constraint and variable bound judgements) |
| `evaluation/` | Per-requirement specification judgement `evaluation.json`, the comparison report, and the copy of the design specification used in the evaluation |
| `lens/` | Files given with `--seq` |
| `index.md`, `manifest.json` | Material index (description, size and first 12 characters of the SHA-256 of each file) and machine-readable manifest (with source paths, full hashes and items that could not be produced) |

## Trimmed macro and verbatim command block

The trimmed macro is cut from the **recorded command block** at fixed position markers, not regenerated: each recorded stage is "changes and opening variables → zero-cycle check (`aut; err cdv; mxc 0; vli y; go`) → AUT settings, variable bounds, constraints → `out <file>` → `go` → `out t` → control code restoration → `sav <file>`". When a marker cannot be found or the order does not match, that stage is written as a comment in the trimmed macro and listed under "Notes" in the index; the verbatim command block is not affected.

The differences (also written in the index): the trimmed macro leaves out the zero-cycle variable list check, `tim` and `vli y`, output redirection, control code restoration and candidate saving. So after running the hand-written macro, the variable markers stay in the lens, no candidate file is written and the variable list is not checked; the optimization commands themselves are the same as those the service sent.

## When the initial lens cannot be fully traced

A wide-field starting lens without vignetting may give no result at all for some analyses (for example when every grid ray is blocked by the stop). The initial plots then include only the parts actually produced; each missing item is listed under "Items that could not be produced" in the index with the reason from the run record, `unavailable` in `manifest.json` lists the same, and the notes state how many items are missing. The evaluation package itself keeps running the other analyses when one fails (see the [comparison workflow](comparison-workflow.en.md)), so the full set of material for the final lens can still be assembled in this case.

## Limits

It only organizes existing run packages and does not judge whether a design is good; images and `.PLT` files are not checked for content (only the hashes after copying are checked); AUT records must come from this service's `codev_mcp.aut`.
