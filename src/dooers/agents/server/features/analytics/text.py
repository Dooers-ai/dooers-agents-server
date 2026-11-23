"""Text copied from an event onto a feedback row."""

from __future__ import annotations

from typing import Any


def message_text(event: Any | None) -> str | None:
    content = getattr(event, "content", None)
    if not content:
        return None
    parts = [
        part.text
        for part in content
        if getattr(part, "type", None) == "text" and isinstance(getattr(part, "text", None), str) and part.text
    ]
    return "\n".join(parts) if parts else None
