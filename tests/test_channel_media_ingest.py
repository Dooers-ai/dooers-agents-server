"""Inline channel media ingest (WhatsApp ``data_base64`` → upload store / handler bytes)."""

from __future__ import annotations

import base64

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.handlers.content_policy import (
    HANDLER_SUPPORTED_CONTENT_TYPES,
    content_part_public_type,
    parse_allowed_content_types_setting,
)
from dooers.agents.server.handlers.pipeline import HandlerPipeline
from dooers.agents.server.protocol.models import ContactPart, ImagePart
from dooers.agents.server.storage.channel_media_ingest import prepare_inline_media_part
from dooers.agents.server.upload_store import UploadStore


def test_prepare_inline_media_decodes_data_uri():
    raw = base64.b64encode(b"\xff\xd8\xff").decode("ascii")
    out = prepare_inline_media_part(
        {"type": "image", "mime_type": "image/jpeg", "data_base64": f"data:image/jpeg;base64,{raw}"}
    )
    assert out["_inline_bytes"] == b"\xff\xd8\xff"
    assert "data_base64" not in out
    assert out["filename"] == "image"


def test_contact_is_allowlist_token():
    assert "contact" in HANDLER_SUPPORTED_CONTENT_TYPES
    assert parse_allowed_content_types_setting("text,contact") == frozenset({"text", "contact"})
    assert content_part_public_type(ContactPart(display_name="Ana")) == "contact"


class _SettingsPersistence:
    async def get_settings(self, agent_id: str) -> dict:
        return {}


@pytest.mark.asyncio
async def test_resolve_data_base64_fills_handler_bytes_and_ref_id():
    store = UploadStore()
    pipeline = HandlerPipeline(
        persistence=_SettingsPersistence(),  # type: ignore[arg-type]
        upload_store=store,
        agent_config=AgentConfig(database_type="postgres", store_chat_uploads=False, chat_storage_service="none"),
    )
    raw = base64.b64encode(b"png-bytes").decode("ascii")
    handler, storage = await pipeline._resolve_content_parts_async(
        [{"type": "image", "mime_type": "image/png", "data_base64": raw, "filename": "a.png"}],
        agent_id="agent-1",
        thread_id="thread-1",
    )
    assert len(handler) == 1
    assert isinstance(handler[0], ImagePart)
    assert handler[0].data == b"png-bytes"
    assert storage[0].ref_id
    assert storage[0].url is None
    assert store.consume(storage[0].ref_id) is None
