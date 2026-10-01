"""Read-only list of catalog glasses near a reference nd/vd (D8).

The reference is a catalog glass (``SK16_SCHOTT``) or an explicit (nd, vd).
Candidates come only from the catalogs the caller names; nothing is preset and
nothing is substituted in a lens. Every index comes from the native ``GLI``
listing, which prints five decimals at fixed wavelengths; nd is the 587.6 nm
column and vd = (nd - 1) / (nF - nC) from the 486.1 and 656.3 nm columns, so vd
is a service-calculated value with the precision noted in the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .com_session import ComSession
from .glass import CATALOGS, HEADER, ROOT, catalog_identity, installed_catalog_file
from .record import execution_step, write_record
from .safety import validate_glass_name

WAVELENGTH_HEADER = re.compile(r"^\s*GLASS CODES\s+(.+)$", re.MULTILINE)
ROW = re.compile(r"^\s*(\d{6})\s+([A-Z0-9.+-]+)\s")
VALUE = re.compile(r"\d+\.\d{5}")
LINES = {"d": 587.6, "F": 486.1, "C": 656.3}
#: Half a unit in the fifth printed decimal of GLI.
INDEX_HALF_STEP = 5e-6


def _columns(text: str) -> list[tuple[float, int]]:
    """Header wavelengths and the column where each printed value ends."""
    columns = None
    for match in WAVELENGTH_HEADER.finditer(text):
        line_start = text.rfind("\n", 0, match.start(1)) + 1
        header = [(float(item.group()), match.start(1) - line_start + item.end())
                  for item in re.finditer(r"\d+\.\d", match.group(1))]
        if columns is None:
            columns = header
        elif header != columns:
            raise ValueError("GLI pages use different wavelength columns")
    if not columns:
        raise ValueError("GLI listing has no wavelength header")
    return columns


def catalog_table(text: str, catalog: str) -> list[dict]:
    """Glass rows with nd, nF, nC read by column position from one catalog listing."""
    headings = {match.upper() for match in HEADER.findall(text)}
    if headings != {catalog.upper()}:
        raise ValueError(f"GLI did not confirm the requested catalog {catalog}")
    if not text.rstrip().endswith("Command End:"):
        raise ValueError(f"GLI listing is incomplete for {catalog}")
    columns = _columns(text)
    wanted = {}
    for key, wavelength in LINES.items():
        matches = [end for value, end in columns if abs(value - wavelength) < 0.05]
        if len(matches) != 1:
            raise ValueError(f"GLI has no {wavelength} nm column")
        wanted[key] = matches[0]
    rows, seen = [], set()
    for line in text.upper().splitlines():
        match = ROW.match(line)
        if not match:
            continue
        values = {}
        for item in VALUE.finditer(line):
            for key, end in wanted.items():
                if abs(item.end() - end) <= 1:
                    values[key] = item.group()
        code, name = match.groups()
        if (name, code) in seen:
            continue
        seen.add((name, code))
        row = {"catalog": catalog.upper(), "name": name, "code": code,
               "printed": {key: values.get(key) for key in LINES}}
        if len(values) == 3:
            nd, nf, nc = (float(values[key]) for key in ("d", "F", "C"))
            row.update(nd=nd, vd=(nd - 1) / (nf - nc) if nf > nc else None)
        rows.append(row)
    if not rows:
        raise ValueError(f"GLI listing has no glass rows for {catalog}")
    return rows


def vd_uncertainty(nd: float, vd: float) -> float:
    """Worst-case vd spread from half a printed unit on nd, nF and nC."""
    nf_nc = (nd - 1) / vd
    return vd * (INDEX_HALF_STEP / (nd - 1) + 2 * INDEX_HALF_STEP / nf_nc)


def nearest(reference: dict, rows: list[dict], *, nd_scale: float, vd_scale: float, limit: int) -> list[dict]:
    scored = []
    for row in rows:
        if row.get("nd") is None or row.get("vd") is None:
            continue
        dn, dv = row["nd"] - reference["nd"], row["vd"] - reference["vd"]
        scored.append({**row, "delta_nd": dn, "delta_vd": dv,
                       "distance": math.hypot(dn / nd_scale, dv / vd_scale),
                       "vd_uncertainty": vd_uncertainty(row["nd"], row["vd"])})
    scored.sort(key=lambda item: (item["distance"], item["name"]))
    return scored[:limit]


def query_near(*, glass: str | None = None, nd: float | None = None, vd: float | None = None,
               catalogs: list[str], limit: int = 8, nd_scale: float = 0.01, vd_scale: float = 1.0,
               run_root: Path | None = None,
               session_factory: Callable[..., ComSession] = ComSession) -> dict:
    if (glass is None) == (nd is None or vd is None) or (glass is not None and (nd is not None or vd is not None)):
        raise ValueError("Give either --glass NAME_CATALOG, or both --nd and --vd")
    if not catalogs or len(set(catalogs)) != len(catalogs):
        raise ValueError("Name one or more distinct target catalogs; none is preset")
    targets = [validate_glass_name(item).upper() for item in catalogs]
    if any(item not in CATALOGS for item in targets):
        raise ValueError("Target catalogs must be CODE V 10.2 pre-stored catalogs: " + ", ".join(CATALOGS))
    if not 1 <= limit <= 50 or not (nd_scale > 0 and vd_scale > 0):
        raise ValueError("limit is 1..50 and the distance scales must be positive")
    reference: dict
    if glass is not None:
        name, _, catalog = validate_glass_name(glass).upper().rpartition("_")
        if not name or catalog not in CATALOGS:
            raise ValueError("--glass must be NAME_CATALOG with a pre-stored catalog, e.g. SK16_SCHOTT")
        reference = {"glass": f"{name}_{catalog}", "name": name, "catalog": catalog}
    else:
        if not (math.isfinite(nd) and math.isfinite(vd) and 1 < nd < 3 and 0 < vd < 150):
            raise ValueError("nd must be in (1, 3) and vd in (0, 150)")
        reference = {"nd": nd, "vd": vd, "source": "caller"}
    catalog_path = installed_catalog_file()
    identity = catalog_identity(catalog_path)
    run = (run_root or ROOT / ".codev-run").resolve() / (
        "glass-near-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    if not str(run).isascii():
        raise ValueError("CODE V 10.2 working directory must have an ASCII path")
    run.mkdir(parents=True, exist_ok=False)
    session = session_factory(starting_directory=run / "work")
    listings = {}
    try:
        version = session.start()

        def listing(catalog: str) -> list[dict]:
            if catalog not in listings:
                text = session.command_raw(f"gld;gli {catalog}")
                if session.output_is_truncated(text):
                    raise ValueError(f"GLI output truncated for {catalog}")
                path = run / f"gli-{catalog}.txt"
                path.write_text(text, encoding="utf-8")
                listings[catalog] = {"rows": catalog_table(text, catalog), "raw_path": str(path),
                                     "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
            return listings[catalog]["rows"]

        if "catalog" in reference:
            matches = [row for row in listing(reference["catalog"]) if row["name"] == reference["name"]]
            if len(matches) != 1 or matches[0].get("nd") is None or matches[0].get("vd") is None:
                raise ValueError(f"{reference['glass']} is not a single complete row in GLI {reference['catalog']}")
            reference.update({key: matches[0][key] for key in ("code", "nd", "vd", "printed")},
                             source="GLI", vd_uncertainty=vd_uncertainty(matches[0]["nd"], matches[0]["vd"]))
        results = {catalog: nearest(reference, listing(catalog), nd_scale=nd_scale, vd_scale=vd_scale,
                                    limit=limit) for catalog in targets}
    finally:
        calls = session.call_count
        if session.stop() is False:
            raise RuntimeError("glass session cleanup unconfirmed")
    if catalog_identity(catalog_path)["sha256"] != identity["sha256"]:
        raise ValueError("CODE V glass catalog changed during the query")
    result = {
        "status": "listed", "codev_version": version, "catalog_file": identity, "reference": reference,
        "target_catalogs": targets, "candidates": results, "run_directory": str(run), "com_calls": calls,
        "listings": {key: {k: v for k, v in value.items() if k != "rows"} for key, value in listings.items()},
        "definitions": {
            "nd": "GLI 587.6 nm column (five printed decimals)",
            "vd": "(nd - 1) / (n486.1 - n656.3) from the GLI columns; service calculated",
            "distance": f"sqrt((delta_nd / {nd_scale:g})^2 + (delta_vd / {vd_scale:g})^2); a ranking "
                        "convention of this service, not optical equivalence",
            "vd_uncertainty": "worst case from half a unit in the fifth decimal of nd, nF and nC",
        },
        "note": "Read-only listing. Nothing is substituted; replace a glass with update_lens and re-optimize.",
    }
    (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_record(run / "execution-record.json", [execution_step(
        action="glass_candidates", tool="codev_mcp.glass_near", status="succeeded", source="codev",
        inputs=[{"role": "glass_catalog", "path": identity["path"], "sha256": identity["sha256"]}],
        parameters={"reference": reference, "catalogs": targets, "nd_scale": nd_scale, "vd_scale": vd_scale},
        native_commands=[f"GLD;GLI {catalog}" for catalog in listings],
        command_note="Read-only catalog listing; nothing was substituted",
        results={"candidates": results, "definitions": result["definitions"]}, outputs=[],
        bundle=str(run))])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读列出指定目录中 nd/νd 相近的玻璃（不替换）")
    parser.add_argument("--glass", help="参考玻璃 NAME_CATALOG，例如 SK16_SCHOTT")
    parser.add_argument("--nd", type=float)
    parser.add_argument("--vd", type=float)
    parser.add_argument("--catalog", action="append", required=True, help="目标目录，可重复；无默认值")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--nd-scale", type=float, default=0.01)
    parser.add_argument("--vd-scale", type=float, default=1.0)
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = query_near(glass=args.glass, nd=args.nd, vd=args.vd, catalogs=args.catalog,
                            limit=args.limit, nd_scale=args.nd_scale, vd_scale=args.vd_scale,
                            run_root=args.run_root)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
