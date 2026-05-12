"""Deterministic object keys for chat attachments (shared by upload + signed URL replay)."""

from __future__ import annotations

import re

CHAT_ARTIFACT_PREFIX = "chat-artifacts/v1"
_NO_THREAD = "no-thread"
_SAFE_THREAD = re.compile(r"[^a-zA-Z0-9._-]+")


def thread_segment(thread_id: str | None) -> str:
    tid = (thread_id or "").strip()
    if not tid:
        return _NO_THREAD
    return _SAFE_THREAD.sub("-", tid)[:200] or _NO_THREAD


def safe_filename(name: str) -> str:
    base = (name or "file").replace("\\", "/").split("/")[-1]
    return base.replace("/", "_") or "file"


def build_chat_artifact_thread_prefix(*, agent_id: str, thread_id: str) -> str:
    """Prefix for all durable uploads for this thread (``…/agent/thread_seg/``)."""
    agent = (agent_id or "").strip() or "unknown-agent"
    seg = thread_segment(thread_id)
    return f"{CHAT_ARTIFACT_PREFIX}/{agent}/{seg}/"


def build_chat_artifact_object_key(
    *,
    agent_id: str,
    thread_id: str | None,
    ref_id: str,
    filename: str,
) -> str:
    """Full blob path inside bucket or container."""
    agent = (agent_id or "").strip() or "unknown-agent"
    seg = thread_segment(thread_id)
    rid = (ref_id or "").strip() or "unknown-ref"
    return f"{CHAT_ARTIFACT_PREFIX}/{agent}/{seg}/{rid}/{safe_filename(filename)}"
