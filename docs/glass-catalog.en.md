# Local Glass Catalog Queries

[中文](glass-catalog.md)

`python -m codev_mcp.glass` is a standalone read-only CLI and adds no MCP tool. It queries the catalogs installed with CODE V 10.2 on the local machine through the versioned `CodeV.Command.102`, using a windowless session created by the service, and does not read or modify user lenses.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m codev_mcp.glass --name BK7 --catalog SCHOTT --wavelength-nm 550
.\.venv\Scripts\python.exe -m codev_mcp.glass --name BK7
```

By default the install directory is located from the 32-bit `CodeV.Command.102` registration. When `--install-root` is given, that path must match the registered COM install directory; otherwise the query is refused before a session starts, so that the hash of a different `glass.cat` is never attached to the current engine's results. Of the catalog files, only the SHA-256, size, modification time and format identifier are read; they are not parsed, copied or distributed with the repository. The detailed native output of each query is written to its own `.codev-run/glass-query-*` directory, and the JSON returns that path and its hash.

The result `status` is `found`, `not_found`, `ambiguous` or `catalog_unavailable`. Without a catalog, the 14 installed catalogs listed in the manual are searched one by one; glasses with the same name are only listed as candidates, and you must pass `--catalog` to choose explicitly. `not_found` may list similar spellings but never substitutes automatically. An unsupported catalog name or a missing local `glass.cat` returns `catalog_unavailable`. If the `GLI` header, glass rows or end marker of any catalog cannot be verified, the whole query fails, and a match in another catalog cannot be used to report `found` or `not_found`. The CLI returns exit code 0 for `found`, 2 for these miss states, and 1 for parameter errors, COM errors or output that cannot be verified.

With `--wavelength-nm`, the CLI extracts the refractive index at the given wavelength from CODE V's native `GLD;REL` listing by catalog, name and six-digit code. `index.available=false` means this native output could not verify the value; it must not be taken as the material physically lacking one. Values have listing print precision only; the catalog's own valid wavelength range and precision notes are those in the local `detail.txt`, and this is not used to judge glass substitution or design feasibility. The query covers the installed catalogs only, not private user catalogs, melt data or vendor catalog updates.

## Nearby glass candidates

`python -m codev_mcp.glass_near` lists, read-only, glasses in **catalogs named by the caller** whose nd/νd are close to a reference glass. It presets no catalog and does not replace glass in a lens.

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.glass_near --glass SK16_SCHOTT --catalog CDGM --catalog HOYA --limit 5
.\.venv\Scripts\python.exe -m codev_mcp.glass_near --nd 1.62041 --vd 60.3 --catalog CDGM
```

- Write the reference glass as `name_catalog`, read from the `GLI` row of its catalog; or give `--nd` and `--vd` directly. `--catalog` can be repeated, must be an installed CODE V 10.2 catalog, and has no default.
- nd is the `GLI` 587.6 nm column; νd = (nd − 1)/(n486.1 − n656.3), computed from the printed values of the same row, is a value computed by the service. `GLI` prints only five decimals, so the result gives a worst-case interval for νd (about ±0.06 for SK16, for example), which can be compared with the six-digit glass code; the computed value may differ from the vendor's published νd within that interval.
- The sort distance is √((Δnd/0.01)² + (Δνd/1)²), and the scales can be changed with `--nd-scale` and `--vd-scale`. It is only this service's sorting convention and does not mean optical equivalence; the result lists Δnd and Δνd as well.
- Each catalog listing is parsed by column position; the header must contain only the requested catalog and end with `Command End:`, and rows missing 587.6/486.1/656.3 nm values are left out of the sorting. Native listings are kept in their own `.codev-run/glass-near-*` directory with hashes recorded, and catalog file hashes are checked before and after.
- The actual replacement is done with `update_lens` (or `python -m codev_mcp.edit`), followed by re-optimization with controlled AUT.
