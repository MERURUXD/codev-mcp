"""Structured cleanup evidence for CLI orchestration (not an MCP contract)."""
from __future__ import annotations

from typing import Any


class CleanupError(RuntimeError):
    """A failed isolated operation, with the outcome of its final cleanup check."""

    def __init__(self, message: str, *, remaining: list[int], interrupted: bool = False):
        super().__init__(message)
        self.cleanup_info = {"cleanup_remaining": remaining,
                             "cleanup_confirmed": not remaining, "interrupted": interrupted}


def cleanup_info(exc: BaseException) -> dict[str, Any]:
    return getattr(exc, "cleanup_info", {})


def cleanup_in_doubt(value: Any) -> bool:
    """Inspect structured evidence, including each disposable diagnostic session.

    A final owned-process check supersedes an earlier close error *on the same
    operation*. It cannot clear uncertainty in a different nested operation.
    Human-readable messages never decide whether scheduling may continue.
    """
    if isinstance(value, list):
        return any(cleanup_in_doubt(item) for item in value)
    if not isinstance(value, dict):
        return False
    if "cleanup_remaining" in value:
        doubt = value["cleanup_remaining"] != []
    elif "cleanup_confirmed" in value:
        doubt = value["cleanup_confirmed"] is not True
    else:
        doubt = bool(value.get("cleanup_unconfirmed") or value.get("cleanup_error"))
    return bool(doubt or value.get("cleanup_in_doubt") or
                any(cleanup_in_doubt(item) for item in value.values() if isinstance(item, (dict, list))))
