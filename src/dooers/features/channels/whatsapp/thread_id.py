"""Deterministic thread ids for WhatsApp 1:1 (E.164).

When ``dooers-tools-whatsapp`` forwards to an agent, each tools instance (UUID)
gets its own thread namespace: ``wa:inst:{instance_id}:{+E164}``, so the same
phone number re-linked to a different agent/instance does not collide with
legacy rows keyed only by ``wa:{+E164}`` (per-agent / instance isolation).
"""

from __future__ import annotations

import re

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_e164(raw: str) -> str:
    """Normalize to ``+{digits}`` or raise ValueError.

    Accepts values like ``5511999990001``, ``+55 11 99999-0001``.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty phone")
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        out = "+" + digits
    else:
        out = "+" + digits
    if not _E164_RE.match(out):
        raise ValueError(f"not a valid E.164: {raw!r}")
    return out


def whatsapp_thread_id(e164: str, instance_id: str | None = None) -> str:
    """Thread id: ``wa:inst:{instance_id}:{+E164}`` when *instance_id* is set (tools
    inbound); otherwise ``wa:{+E164}`` (legacy / examples).
    """
    n = normalize_e164(e164)
    iid = (instance_id or "").strip()
    if iid:
        return f"wa:inst:{iid}:{n}"
    return f"wa:{n}"
