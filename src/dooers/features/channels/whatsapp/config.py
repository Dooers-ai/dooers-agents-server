"""Defaults for the built-in Dooers WhatsApp tools client."""

from __future__ import annotations

# WhatsApp tools service base is intentionally baked in (includes path prefix).
_TOOLS_BASE_URL = "https://services.dooers.ai/whatsapp"


def tools_base_url() -> str:
    """Base URL of dooers-tools-whatsapp (path included)."""
    return _TOOLS_BASE_URL.rstrip("/")
