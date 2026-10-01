"""Validation helpers that keep user input from becoming CODE V commands.

CODE V command mode is line oriented: a semicolon or a newline in a file path or
a glass name would start a second command. Phase C probe 3 confirmed this by
sending a glass name containing "; del all" and watching CODE V execute the
second command. Everything that reaches the command line therefore goes through
these helpers first.
"""

from __future__ import annotations

import re
from pathlib import Path

from .errors import ParameterError

#: Characters CODE V treats as command structure rather than as data.
FORBIDDEN_IN_FILESPEC = (";", "\n", "\r", '"', "'", "*", "?", "|", ">", "<")
FORBIDDEN_IN_GLASS = (";", "\n", "\r", '"', "'", " ", "\t", ",", "=", "$", "%", "&", "|")

GLASS_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")
LENS_SUFFIX = ".len"

#: CODE V writes 0.1E+19 for an infinite radius and about 9.9E+11 for an
#: infinite thickness (measured in Phase C, see local validation records).
INFINITE_RADIUS_THRESHOLD = 1.0e17
INFINITE_THICKNESS_THRESHOLD = 1.0e10


def validate_filespec(raw: str, *, must_exist: bool = False, must_not_exist: bool = False) -> Path:
    """Validate a lens file path and return it as a resolved Path.

    Relative paths are rejected so the service never depends on CODE V current
    directory, and the suffix is normalised to .len because CODE V appends it.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ParameterError("A file path is required.")
    if any(character in raw for character in FORBIDDEN_IN_FILESPEC):
        raise ParameterError(
            "The file path contains characters that CODE V would treat as command structure.",
            details={"path": raw, "forbidden": list(FORBIDDEN_IN_FILESPEC)},
        )
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        raise ParameterError(
            "An absolute path is required.", details={"path": raw}
        )
    if candidate.suffix == "":
        candidate = candidate.with_suffix(LENS_SUFFIX)
    elif candidate.suffix.lower() != LENS_SUFFIX:
        raise ParameterError(
            "Only .len lens files are supported.", details={"path": raw}
        )
    if must_exist and not candidate.exists():
        from .errors import NotFoundError

        raise NotFoundError(f"Lens file not found: {candidate}", details={"path": str(candidate)})
    if must_not_exist and candidate.exists():
        raise ParameterError(
            "The target file already exists.",
            details={"path": str(candidate)},
            hint="Choose a new file name; this service never overwrites a lens file.",
        )
    return candidate


def command_filespec(path: Path) -> str:
    """Render a path for a CODE V command, quoted so spaces survive."""
    text = str(path)
    if any(character in text for character in FORBIDDEN_IN_FILESPEC):
        raise ParameterError("Unsafe file path reached the command builder.", details={"path": text})
    return f'"{text}"' if " " in text else text


def validate_glass_name(value: object) -> str:
    """Reject anything that is not a plain glass name."""
    if not isinstance(value, str):
        raise ParameterError("A glass name must be a string.", details={"value": repr(value)})
    if any(character in value for character in FORBIDDEN_IN_GLASS) or not GLASS_PATTERN.match(value):
        raise ParameterError(
            "The glass name must be 1-32 characters of letters, digits, dot, plus, minus "
            "or underscore.",
            details={"value": value},
        )
    return value


def check_number(value: object, *, field_name: str, minimum=None, maximum=None) -> float:
    """Validate a numeric edit value and return it as a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ParameterError(f"{field_name} needs a number.", details={"value": repr(value)})
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ParameterError(
            f"{field_name} needs a number.", details={"value": repr(value)}
        ) from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ParameterError(f"{field_name} must be finite.", details={"value": repr(value)})
    if minimum is not None and number < minimum:
        raise ParameterError(
            f"{field_name} must be at least {minimum}.", details={"value": number}
        )
    if maximum is not None and number > maximum:
        raise ParameterError(
            f"{field_name} must be at most {maximum}.", details={"value": number}
        )
    return number


def looks_infinite(value: str) -> bool:
    """True when CODE V used its large sentinel value for an infinite quantity."""
    text = value.strip().upper()
    return text.startswith("0.1") and "E+19" in text


def format_float(value: float) -> str:
    """Render a number with enough digits and no exponent surprises."""
    text = f"{value:.12g}"
    return text
