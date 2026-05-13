"""Defaults for the built-in Dooers WhatsApp tools client."""

from __future__ import annotations

import os

# Production default (path prefix included). Override with ``DOOERS_WHATSAPP_TOOLS_BASE`` for local/ngrok tools.
_DEFAULT_TOOLS_BASE_URL = "https://services.dooers.ai/whatsapp"


def tools_base_url() -> str:
    """Base URL of dooers-tools-whatsapp (path included).

    Reads ``DOOERS_WHATSAPP_TOOLS_BASE`` when set (e.g. ``http://127.0.0.1:8810`` or an ngrok URL);
    otherwise uses the production tools host so workers in cloud keep working without extra env.
    """
    raw = (os.environ.get("DOOERS_WHATSAPP_TOOLS_BASE") or "").strip()
    if raw:
        return raw.rstrip("/")
    return _DEFAULT_TOOLS_BASE_URL.rstrip("/")
