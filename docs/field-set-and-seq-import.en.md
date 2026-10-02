# Field Sets, Image-Height Normalization and `.seq` Import

[中文](field-set-and-seq-import.md)

This page covers replacing the whole field set with `update_lens`, the `PIM` image solve of `create_lens`, and whitelisted `.seq` import. There are still eleven public tools; all input passes typed validation, and no arbitrary command, macro or `IN` is executed.

## 1. Replacing the field set: `field_set` in `update_lens`

```json
{"field_set": {"fields": [
  {"y_angle": 0}, {"y_angle": 5}, {"y_angle": 10}, {"y_angle": 14},
  {"y_angle": 18, "weight": 0.5, "vuy": 0.5}
]}}
```

- A request carries either `edits` or `field_set`, never both in one batch (model validation refuses it directly). `field_set` is a transaction of its own: restore point → change → read back → compare item by item with the whole-lens snapshot from before the change (anything outside the fields moving is a failure) → checkpoint; a failure rolls everything back and verifies.
- Each field: `y_angle` (required), `x_angle` (default 0), and optional `weight` and vignetting factors `vux/vlx/vuy/vly` (-0.99 to 0.99). 1 to 10 fields, angles limited to ±89°, weights 0 to 1e6.
- **Omitted weights and vignetting factors keep the values of the field with the same number; new fields get weight 1 and vignetting 0; when the set is shorter than before, the extra fields are dropped.** This is CODE V's native behavior: the number of `XAN`/`YAN` values decides the number of fields, weights and vignetting are kept by field index, and shortening and then lengthening again does not "bring back" old values. Note that "same number" is not "same angle": changing 0/10/14 to 0/5/10/14/18 makes field 2 keep the vignetting factors of the old 10° field; give them explicitly if they should change.
- The commands the service sends are fixed: `XAN` and `YAN` (the number of values = the new number of fields, setting the count and the angles), then per-field commands only for weights and factors that differ from what CODE V kept (`WTF Fn`, `VUY Fn` and so on, the same as single edits). The last entry in the result's `warnings` lists the commands actually sent.
- Scope: single-zoom lenses with angle-defined fields (`XAN/YAN`); multi-zoom lenses and fields defined by object or image height return `field_set.applied=false` with the reason, and no command is sent. Any open single-zoom lens can use it, not only lenses created by the service. The image solve (`PIM`), apertures and other data are unchanged; the checkpoint format is still 6.
- Result `UpdateResult.field_set`: `applied`, `previous_fields`, the read-back `fields`, `rejected_reason`; on refusal `rolled_back=true`.
- The edit file of the standalone CLI `python -m codev_mcp.edit` can be written as `{"schema_version":1,"kind":"lens_edits","name":"...","field_set":{...}}` (either it or `edits`), and the execution record gives the equivalent commands.

## 2. Field angles normalized by image height (no CODE V connection)

When a task says "normalized fields 0.5/0.7/0.85", there are two readings: CODE V's relative field is by **image height** (proportional to tan of the angle without distortion), while taking angles linearly differs by almost half a degree (with a 23.5° half field, about 0.5° at 0.5).

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.fieldset --max-angle 23.5 --relative 0 0.5 0.7 0.85 1 --output fields.json
```

- Default `--mode height`: angle = atan(relative field × tan of the maximum angle); `--mode angle` is linear. The output JSON gives the convention, per-field angles, the linear reading for comparison, the relative image height computed back from the result, and a `field_set` usable directly as an `update_lens` request (`--output` writes a new file and refuses to overwrite; `--weights` can add weights).
- It only computes; it does not change CODE V's field type and adds no MCP tool.

## 3. `image_solve: "pim"` in `create_lens`

When creating a lens, this adds a CODE V paraxial image solve to the thickness of the last ordinary surface (sending `pim yes`). The thickness in the request is only a starting value and is derived by CODE V after solving; from then on, edits to that thickness are refused as a "solve-controlled parameter", and changing a radius and so on moves the image distance with the focus and lists it in `warnings`. The snapshot is checked against `SOLVES` and the variable control columns, and creation fails if the solve did not take effect. A lens with a solve is not a "simple spherical model", so `edit_lens_structure` refuses it (the same as existing lenses with solves).

## 4. `.seq` import: `python -m codev_mcp.seq_import`

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.seq_import --seq lens.seq --output lens.len --reference-lens ref.len
.\.venv\Scripts\python.exe -m codev_mcp.seq_import --seq lens.seq --dry-run   # parse only and list the commands that would be sent
```

The sequence file is never executed. The service parses it line by line against a whitelist into typed requests, and then builds the commands itself: `create_lens` (including `PIM`) → when needed, `field_set` with `update_lens` (weights, vignetting) → when needed, wavelength reference/weight edits → `save_lens_as` → reopen and recheck in a separate session.

**Accepted commands**: `RDM`, `LEN "version string"`, `EPD/FNO/NA/NAO`, `DIM M|C|I`, `WL` (strictly descending), `REF`, `WTW` (non-negative integers), `XAN/YAN/WTF/VUX/VLX/VUY/VLY`, `SO` (infinite object distance), `S radius thickness [glass_catalog]` (radius 0 is flat), a following `STO`, `PIM` (only on the last ordinary surface), `SI 0 0`, `GO`; `!` comments, blank lines, `&` line continuation at the end of a line, and several commands on one line separated by `;` (a `;` inside quotes is text).

**Listed as "not imported" rather than refused**: `TITLE`, `INI`, `UID`, `DOR` (an unknown system item from CODE V 2025), the version string of `LEN`, `DER` (derivative increments left by AUT), and the variable markers `CCY/THC/GLC` (AUT variables are opened only by a typed AUT specification). The list is written to `not_imported` in `manifest.json`.

**The whole file is refused, with each line listed**: every other command (`IN`, apertures such as `CIR`, aspheres, other solves/pickups, zoom, `AUT`, ...), finite object distance, non-zero image surface thickness or radius, a glass without a catalog suffix, inconsistent field counts, missing `STO`/`SI`/`SO`, and so on. Any refusal writes no output and does not connect to CODE V. The file size is limited to 1 MB and 5000 lines, and NUL is not accepted.

**Verification**: read-back values are compared item by item with the sequence (units, aperture, wavelengths, weights, reference wavelength, field angles/weights/vignetting, each surface's radius/thickness/glass, stop, `PIM` solve), and the solve and specification sections of `LIS` are compared once more; when CODE V re-solves the `PIM` thickness and it differs from the sequence's starting value by more than 1e-6, a warning is given (not a failure: CODE V would re-solve it the same way when reading this sequence). The service sends 12 significant digits while the sequence has 16; the read-back deviation is recorded in `max_relative_deviation`. With `--reference-lens`, the import result is compared item by item with that `.len`, and any difference is a failure. The execution record lists the equivalent commands the service actually sent and notes that the sequence text itself was never sent to CODE V.

Limits: apertures, decenters, aspheres, coatings, multi-zoom and variable markers are not imported; `INI`/title in the sequence are not carried into the new lens. When the `.seq` was exported by CODE V's `WRL` (`python -m codev_mcp.export_seq`), lenses with obscurations or explicit clear apertures (such as cooke1) are refused.
