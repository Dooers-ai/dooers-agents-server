"""Strip characters Postgres and other stores reject in text/jsonb."""

from __future__ import annotations

from typing import Any


def without_nul(value: Any) -> Any:
    """Remove U+0000 from strings nested in JSON-compatible values.

    Postgres ``text``/``jsonb`` reject ``\\u0000``. Tool output from a sandbox
    (binary files, ``strings`` on a binary, a corrupted log) can carry it and
    would otherwise abort the whole turn at ``create_event``.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [without_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: without_nul(item) for key, item in value.items()}
    return value
