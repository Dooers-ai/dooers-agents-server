"""Content-type validation for incoming user messages."""

from __future__ import annotations

import json
from typing import Any

from dooers.exceptions import UnsupportedContentTypeError
from dooers.protocol.models import AudioPart, ContentPart, DocumentPart, ImagePart, TextPart

# Types the SDK can surface to handlers today (excluding video — rejected at ingest).
HANDLER_SUPPORTED_CONTENT_TYPES = frozenset({"text", "audio", "image", "document"})


def content_part_public_type(part: ContentPart) -> str:
    if isinstance(part, TextPart):
        return "text"
    if isinstance(part, AudioPart):
        return "audio"
    if isinstance(part, ImagePart):
        return "image"
    if isinstance(part, DocumentPart):
        return "document"
    return type(part).__name__


def _normalize_kind_token(raw: str) -> str | None:
    t = raw.strip().lower()
    if not t:
        return None
    if t == "doc":
        t = "document"
    if t not in HANDLER_SUPPORTED_CONTENT_TYPES:
        return None
    return t


def normalize_allowed_content_types(raw: Any) -> frozenset[str] | None:
    """Build the pipeline allowlist from :class:`~dooers.config.AgentConfig`.

    Accepts ``None``, ``frozenset``/``set``, ``tuple``/``list`` of tokens, comma-separated ``str``, or JSON list string.

    Returns ``None`` when unset / empty → the SDK skips allowlist enforcement (creator may validate manually).
    """
    if raw is None:
        return None
    if isinstance(raw, (frozenset, set)):
        tokens = list(raw)
    elif isinstance(raw, (list, tuple)):
        tokens = [str(x) for x in raw]
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.startswith("[") and s.endswith("]"):
            try:
                loaded = json.loads(s)
                tokens = [str(x) for x in loaded] if isinstance(loaded, list) else []
            except (json.JSONDecodeError, TypeError):
                tokens = [p.strip() for p in raw.replace(",", " ").split()]
        else:
            tokens = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    else:
        return None

    out: set[str] = set()
    for tok in tokens:
        n = _normalize_kind_token(str(tok))
        if n:
            out.add(n)
    return frozenset(out) if out else None


def parse_allowed_content_types_setting(raw: Any) -> frozenset[str] | None:
    """Backward-compatible alias for ``normalize_allowed_content_types``."""
    return normalize_allowed_content_types(raw)


def format_allowed_content_policy_denial(
    *,
    parts: list[ContentPart],
    allowed: frozenset[str] | None,
    message_template: str | None = None,
) -> str | None:
    """Return assistant-facing text when ``allowed`` is set and ``parts`` include a disallowed kind; else ``None``."""
    if allowed is None:
        return None

    offenders: list[str] = []
    for p in parts:
        pt = content_part_public_type(p)
        if pt not in allowed:
            offenders.append(pt)
    if not offenders:
        return None

    off_h = ", ".join(sorted(set(offenders)))
    allowed_h = ", ".join(sorted(allowed))
    default_en = "This agent does not accept this attachment type in chat ({offenders}). Allowed: {allowed}."
    tmpl = (message_template or "").strip() or default_en
    return tmpl.format(offenders=off_h, allowed=allowed_h)


def enforce_allowed_content_types(
    *,
    parts: list[ContentPart],
    allowed: frozenset[str] | None,
) -> None:
    """Raise ``UnsupportedContentTypeError`` when an allowlist is active and violates (tests / strict callers)."""
    msg = format_allowed_content_policy_denial(parts=parts, allowed=allowed, message_template=None)
    if msg is not None:
        raise UnsupportedContentTypeError(msg)
