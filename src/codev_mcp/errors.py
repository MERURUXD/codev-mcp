"""Structured error types shared by the service, the worker and the backends.

The plan requires that errors distinguish parameter mistakes, a backend that is
not ready, failed computations and a session that has become unusable. Every
failure that crosses the worker boundary is one of these kinds, so callers can
branch on it instead of parsing prose.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class ErrorKind(str, Enum):
    PARAMETER = "parameter"
    NOT_READY = "not_ready"
    COMPUTATION = "computation_failed"
    SESSION_INVALID = "session_invalid"
    UNSUPPORTED = "unsupported"
    NOT_FOUND = "not_found"
    INTERNAL = "internal"


class ErrorInfo(BaseModel):
    """Machine readable failure description."""

    kind: ErrorKind
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    raw_output: str | None = Field(
        default=None, description="Verbatim backend output, when one is available."
    )
    hint: str | None = Field(default=None, description="Suggested next step for the caller.")


class CodeVError(Exception):
    """Base class for failures that are reported to the client as ErrorInfo."""

    kind: ErrorKind = ErrorKind.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        raw_output: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.raw_output = raw_output
        self.hint = hint

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            kind=self.kind,
            message=self.message,
            details=self.details,
            raw_output=self.raw_output,
            hint=self.hint,
        )


class ParameterError(CodeVError):
    kind = ErrorKind.PARAMETER


class NotReadyError(CodeVError):
    kind = ErrorKind.NOT_READY


class ComputationError(CodeVError):
    kind = ErrorKind.COMPUTATION


class SessionInvalidError(CodeVError):
    kind = ErrorKind.SESSION_INVALID


class UnsupportedError(CodeVError):
    kind = ErrorKind.UNSUPPORTED


class NotFoundError(CodeVError):
    kind = ErrorKind.NOT_FOUND


class InternalError(CodeVError):
    kind = ErrorKind.INTERNAL


def parse_tool_error_message(text: str) -> ErrorInfo | None:
    """Recover ErrorInfo from the message of a failed MCP tool call.

    MCP carries only text for a failed call, so this service puts a JSON object
    with the structured fields in the message. The MCP server may prefix it with
    its own wording, so parsing starts at the first opening brace.
    """
    start = text.find("{")
    if start == -1:
        return None
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    try:
        return ErrorInfo.model_validate(payload)
    except ValidationError:
        return None
