"""Test bootstrap: make the src layout importable without installing the package.

Scratch directories are created inside the repository with plain mkdir calls.
Python tempfile.mkdtemp is avoided on purpose: on Windows it creates the folder
with a restrictive DACL that the sandboxed test process cannot write into.
"""

from __future__ import annotations

import itertools
import os
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if os.environ.get("CODEV_MCP_TEST_INSTALLED") != "1" and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
elif os.environ.get("CODEV_MCP_TEST_INSTALLED") == "1":
    import codev_mcp
    if Path(codev_mcp.__file__).resolve().is_relative_to(SRC):
        raise RuntimeError("Installed-package tests unexpectedly imported the source tree")

WORKSPACE = SRC.parent
TMP_ROOT = (Path(os.environ["CODEV_MCP_TEST_TMPROOT"]).resolve()
            if os.environ.get("CODEV_MCP_TEST_TMPROOT") else WORKSPACE / ".codev-run" / "tests")
_counter = itertools.count(1)


class WorkspaceTempDir:
    """Small stand-in for tempfile.TemporaryDirectory rooted in the workspace."""

    def __init__(self, prefix: str = "case") -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = TMP_ROOT / f"{prefix}-{next(_counter)}"
        self.path.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return str(self.path)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "WorkspaceTempDir":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.cleanup()
        return False


def workspace_temp_directory(prefix: str = "case") -> WorkspaceTempDir:
    return WorkspaceTempDir(prefix)
