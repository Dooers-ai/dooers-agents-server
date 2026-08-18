"""Decode inline channel media (``data_base64``) before pipeline persist."""

from __future__ import annotations

import base64
from typing import Any

_MEDIA_TYPES = frozenset({"audio", "image", "document"})


def strip_data_uri_base64(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "," in s and s[:40].lower().startswith("data:"):
        s = s.split(",", 1)[1].strip()
    return s


def pop_inline_media_bytes(data: dict[str, Any]) -> bytes | None:
    """Return bytes from ``data_base64`` (or ``data`` if already bytes). Does not mutate ``data``."""
    raw = data.get("data_base64")
    if isinstance(raw, str) and raw.strip():
        try:
            return base64.b64decode(strip_data_uri_base64(raw), validate=False)
        except Exception:
            return None
    raw_data = data.get("data")
    if isinstance(raw_data, (bytes, bytearray)) and raw_data:
        return bytes(raw_data)
    return None


def default_filename_for_media(part_type: str, filename: Any, mime_type: Any) -> str:
    if isinstance(filename, str) and filename.strip():
        return filename.strip()
    if part_type == "image":
        mime = (mime_type or "").split(";")[0].strip().lower()
        if mime == "image/webp":
            return "sticker.webp"
        return "image"
    if part_type == "audio":
        return "audio"
    return "file"


def prepare_inline_media_part(data: dict[str, Any]) -> dict[str, Any]:
    """If the part carries inline bytes and no ``ref_id``, expose ``_inline_bytes`` for ingest.

    Callers persist via upload store / chat artifacts, then set ``ref_id`` and drop base64.
    """
    part_type = data.get("type")
    if part_type not in _MEDIA_TYPES or data.get("ref_id"):
        return data
    blob = pop_inline_media_bytes(data)
    if not blob:
        return data
    out = dict(data)
    out["_inline_bytes"] = blob
    out.pop("data_base64", None)
    if not out.get("filename"):
        out["filename"] = default_filename_for_media(str(part_type), out.get("filename"), out.get("mime_type"))
    if not out.get("mime_type"):
        out["mime_type"] = {
            "image": "image/jpeg",
            "audio": "application/octet-stream",
            "document": "application/octet-stream",
        }.get(str(part_type), "application/octet-stream")
    return out
