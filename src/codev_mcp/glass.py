"""Read-only CODE V 10.2 glass catalog lookup outside the MCP tool surface."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from .com_session import CLSID, ComSession
from .errors import ParameterError
from .safety import validate_glass_name

CATALOGS = ("CDGM", "CHANCE", "CHINA", "CORNFR", "CORNING", "HIKARI", "HOYA",
            "KODAK", "NSG", "OHARA", "PILKINGTON", "SCHOTT", "SPECIAL", "SUMITA")
ROOT = Path(__file__).resolve().parents[2]
HEADER = re.compile(r"^\s*([A-Z][A-Z0-9]+)\s+GLASSES ON DISC\b", re.MULTILINE)
SUMMARY = re.compile(r"^\s*(\d{6})\s+([A-Z0-9.+-]+)\s+", re.MULTILINE)
DETAIL = re.compile(r"^\s*([A-Z0-9.+-]+)\s+-\s+(\d{6})\s+([A-Z0-9]+)\b", re.MULTILINE)
WVL = re.compile(r"\bWVL\(1\)\s*=\s*(\d+(?:\.\d+)?)\s+NM\b")
INDEX = re.compile(r"^\d+\.\d{5,8}$")


class GlassCandidate(BaseModel):
    catalog: str
    name: str
    code: str


class GlassIndex(BaseModel):
    requested_wavelength_nm: float
    printed_wavelength_nm: float | None = None
    available: bool = False
    refractive_index: float | None = None
    precision_note: str = "CODE V REL listing prints approximately six decimal places."


class GlassQueryResult(BaseModel):
    status: str
    query_name: str
    query_catalog: str | None
    codev_version: str | None = None
    catalog_file: dict = Field(default_factory=dict)
    candidates: list[GlassCandidate] = Field(default_factory=list)
    suggestions: list[GlassCandidate] = Field(default_factory=list)
    index: GlassIndex | None = None
    detail_sha256: str | None = None
    detail_raw_path: str | None = None
    message: str | None = None
    run_directory: str | None = None
    com_calls: int | None = None


def registered_install_root() -> Path:
    """Locate the versioned automation server through its 32-bit registration."""
    import winreg

    key = rf"CLSID\{CLSID}\LocalServer32"
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key, 0,
                       winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as handle:
        executable = winreg.QueryValueEx(handle, None)[0].strip().strip('"')
    path = Path(executable)
    if path.name.lower() != "cvcommand.exe":
        raise ValueError("Versioned CODE V registration does not point to cvcommand.exe")
    return path.parent.resolve()


def installed_catalog_file(install_root: Path | None = None) -> Path:
    registered = registered_install_root()
    if (install_root is not None
            and os.path.normcase(str(install_root.resolve())) != os.path.normcase(str(registered))):
        raise ValueError("--install-root differs from the registered CODE V COM installation")
    return registered / "glass" / "glass.cat"


def catalog_identity(path: Path) -> dict:
    content = path.read_bytes()
    return {"path": str(path), "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "format_header": content[:32].split(b"\0", 1)[0].decode("ascii", errors="replace")}


def catalog_rows(text: str, catalog: str) -> list[GlassCandidate]:
    """Accept only a complete listing for the requested catalog."""
    headings = {match.upper() for match in HEADER.findall(text)}
    if headings != {catalog.upper()}:
        raise ValueError(f"GLI did not confirm the requested catalog {catalog}")
    if not text.rstrip().endswith("Command End:"):
        raise ValueError(f"GLI listing is incomplete for {catalog}")
    rows = []
    seen: set[tuple[str, str]] = set()
    for code, name in SUMMARY.findall(text.upper()):
        key = (name, code)
        if key not in seen:
            seen.add(key)
            rows.append(GlassCandidate(catalog=catalog.upper(), name=name, code=code))
    if not rows:
        raise ValueError(f"GLI listing has no verifiable glass rows for {catalog}")
    return rows


def detail_matches(text: str, candidate: GlassCandidate) -> bool:
    return any(name == candidate.name and code == candidate.code and catalog == candidate.catalog
               for name, code, catalog in DETAIL.findall(text.upper()))


def relative_index(text: str, candidate: GlassCandidate, wavelength_nm: float) -> GlassIndex:
    value = GlassIndex(requested_wavelength_nm=wavelength_nm)
    header = WVL.search(text.upper())
    if header is None:
        return value
    printed = float(header.group(1))
    value.printed_wavelength_nm = printed
    if abs(printed - wavelength_nm) > 0.0051:
        return value
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 7 or parts[0].upper() != candidate.name or parts[1] != candidate.code:
            continue
        indices = [float(token) for token in parts[2:] if INDEX.fullmatch(token)]
        if len(indices) < 4 or not all(math.isfinite(n) and 0 < n < 5 for n in indices[:4]):
            continue
        value.available = True
        value.refractive_index = indices[0]
        break
    return value


def _wavelength(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or not 100 <= value <= 30000:
        raise ValueError("wavelength_nm must be finite and between 100 and 30000")
    return value


def query_glass(name: str, *, catalog: str | None = None, wavelength_nm: float | None = None,
                install_root: Path | None = None, run_root: Path | None = None,
                session_factory: Callable[..., ComSession] = ComSession) -> GlassQueryResult:
    name = validate_glass_name(name).upper()
    catalog = validate_glass_name(catalog).upper() if catalog is not None else None
    wavelength_nm = _wavelength(wavelength_nm)
    result = GlassQueryResult(status="catalog_unavailable", query_name=name, query_catalog=catalog)
    catalog_path = installed_catalog_file(install_root)
    try:
        result.catalog_file = catalog_identity(catalog_path)
    except OSError as exc:
        result.message = f"CODE V glass catalog is unavailable: {type(exc).__name__}: {exc}"
        return result
    if catalog is not None and catalog not in CATALOGS:
        result.message = "Catalog name is not a CODE V 10.2 pre-stored catalog."
        return result
    run_root = (run_root or ROOT / ".codev-run").resolve()
    run = run_root / ("glass-query-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                      + "-" + uuid.uuid4().hex[:8])
    if not str(run).isascii():
        raise ValueError("CODE V 10.2 working directory must have an ASCII path")
    run.mkdir(parents=True, exist_ok=False)
    result.run_directory = str(run)
    session = session_factory(starting_directory=run / "work")
    primary_error = None
    try:
        result.codev_version = session.start()
        available: list[GlassCandidate] = []
        for requested in ((catalog,) if catalog is not None else CATALOGS):
            output = session.command_raw(f"gld;gli {requested}")
            if session.output_is_truncated(output):
                raise ValueError(f"GLI output truncated for {requested}")
            rows = catalog_rows(output, requested)
            available.extend(rows)
        exact = [row for row in available if row.name == name]
        result.candidates = exact
        if not exact:
            close_names = set(difflib.get_close_matches(name, sorted({r.name for r in available}),
                                                         n=8, cutoff=0.6))
            result.suggestions = [r for r in available if r.name in close_names][:16]
            result.status = "not_found"
            result.message = "No exact glass name was found; suggestions are not substitutions."
            return result
        if len(exact) != 1:
            result.status = "ambiguous"
            result.message = ("Name occurs in multiple catalogs; specify --catalog."
                              if catalog is None else
                              "Multiple entries occur in this catalog; this query cannot disambiguate them.")
            return result
        candidate = exact[0]
        detail = session.command_raw(f"gld;gpr {candidate.catalog} {candidate.name}")
        if session.output_is_truncated(detail) or not detail_matches(detail, candidate):
            raise ValueError("GPR did not confirm the exact catalog, glass name and code")
        detail_path = run / "detail.txt"
        detail_path.write_text(detail, encoding="utf-8")
        result.detail_raw_path = str(detail_path)
        result.detail_sha256 = hashlib.sha256(detail.encode("utf-8")).hexdigest()
        if wavelength_nm is not None:
            rel = session.command_raw(f"gld;rel {candidate.catalog} {candidate.name} "
                                      f"{wavelength_nm:.9g} 486.133 587.562 656.273")
            if session.output_is_truncated(rel) or not re.search(
                    rf"PARTIAL DISPERSION DATA FOR\s+{candidate.catalog}\b", rel.upper()):
                raise ValueError("REL output truncated or catalog mismatch")
            (run / "relative.txt").write_text(rel, encoding="utf-8")
            result.index = relative_index(rel, candidate, wavelength_nm)
        result.status = "found"
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        result.com_calls = session.call_count
        cleaned = session.stop()
        if cleaned is False:
            raise RuntimeError(f"glass session cleanup unconfirmed; primary error: "
                               f"{type(primary_error).__name__}: {primary_error}"
                               if primary_error else "glass session cleanup unconfirmed")
        if catalog_identity(catalog_path)["sha256"] != result.catalog_file["sha256"]:
            raise ValueError("CODE V glass catalog changed during the query")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读查询本机 CODE V 10.2 玻璃目录")
    parser.add_argument("--name", required=True, help="精确玻璃名；不自动替换为相似名称")
    parser.add_argument("--catalog", help="厂商目录；重名时必须指定")
    parser.add_argument("--wavelength-nm", type=float, help="核对该波长的原生 REL 折射率")
    parser.add_argument("--install-root", type=Path, help="CODE V 安装根目录；默认读版本化 COM 注册")
    parser.add_argument("--run-root", type=Path, help="独立查询证据目录的父目录")
    args = parser.parse_args(argv)
    try:
        result = query_glass(args.name, catalog=args.catalog, wavelength_nm=args.wavelength_nm,
                             install_root=args.install_root, run_root=args.run_root)
    except (OSError, ValueError, ParameterError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "found" else 2


if __name__ == "__main__":
    raise SystemExit(main())
