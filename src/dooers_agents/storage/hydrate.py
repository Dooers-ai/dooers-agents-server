"""Inject time-limited HTTPS URLs for persisted chat artifact parts (``ref_id``)."""

from __future__ import annotations

import logging

from dooers_agents.config import AgentConfig
from dooers_agents.protocol.models import Thread, ThreadEvent
from dooers_agents.storage.chat_artifact_keys import build_chat_artifact_object_key
from dooers_agents.storage.chat_artifacts import (
    chat_artifact_object_exists,
    chat_storage_service_ready,
    sign_chat_artifact_read_url,
)

logger = logging.getLogger(__name__)


def _default_filename_for_part(part_type: str, filename: str | None) -> str:
    if filename and str(filename).strip():
        return str(filename)
    if part_type == "image":
        return "image"
    if part_type == "audio":
        return "audio"
    return "file"


def _hydrate_media_part(
    cfg: AgentConfig,
    part: object,
    *,
    agent_id: str,
    thread_id: str,
) -> object:
    pt = getattr(part, "type", None)
    if pt not in ("image", "audio", "document"):
        return part
    ref_id = getattr(part, "ref_id", None)
    if not ref_id:
        return part
    fn = _default_filename_for_part(str(pt), getattr(part, "filename", None))
    try:
        key = build_chat_artifact_object_key(
            agent_id=agent_id,
            thread_id=thread_id,
            ref_id=str(ref_id),
            filename=fn,
        )
        key_nt = build_chat_artifact_object_key(
            agent_id=agent_id,
            thread_id=None,
            ref_id=str(ref_id),
            filename=fn,
        )
        # Prefer signing only when ``blob.exists()`` succeeds so we do not mint GET
        # URLs for missing thread keys. Some bucket/IAM setups deny ``exists()`` to
        # workloads that can still sign V4 read URLs — fall back to signing ``no-thread/``
        # without an exists check so the UI gets a URL for orphan uploads.
        url: str | None = None
        if chat_artifact_object_exists(cfg, key):
            url = sign_chat_artifact_read_url(cfg, key)
        elif key_nt != key and chat_artifact_object_exists(cfg, key_nt):
            url = sign_chat_artifact_read_url(cfg, key_nt)
        elif key_nt != key:
            url = sign_chat_artifact_read_url(cfg, key_nt)
        if not url:
            url = sign_chat_artifact_read_url(cfg, key)
    except Exception:
        logger.exception("Chat artifact sign failed for thread=%s ref_id=%s", thread_id, ref_id)
        return part
    if not url:
        return part
    return part.model_copy(update={"url": url})


async def hydrate_thread_events(
    cfg: AgentConfig,
    events: list[ThreadEvent],
    thread: Thread,
) -> list[ThreadEvent]:
    """Used by the WebSocket router and live ``event.append`` broadcasts."""
    if not chat_storage_service_ready(cfg):
        return events
    aid = thread.agent_id
    tid = thread.id
    out: list[ThreadEvent] = []
    for ev in events:
        if not ev.content:
            out.append(ev)
            continue
        new_parts = [_hydrate_media_part(cfg, p, agent_id=aid, thread_id=tid) for p in ev.content]
        out.append(ev.model_copy(update={"content": new_parts}))
    return out
