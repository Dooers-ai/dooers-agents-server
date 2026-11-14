"""Chat replay signs the org-prefixed object key in managed storage."""

from datetime import UTC, datetime

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.protocol.models import Thread, ThreadEvent, User, WireS2C_DocumentPart
from dooers.agents.server.storage.hydrate import hydrate_thread_events

ORG_PREFIX = "agents/e7d3594c-f61f-49c7-a4a0-ca288dd0619e/"


def _thread() -> Thread:
    now = datetime.now(UTC)
    return Thread(
        id="t1",
        agent_id="a1",
        owner=User(user_id="u1"),
        created_at=now,
        updated_at=now,
        last_event_at=now,
    )


@pytest.mark.asyncio
async def test_hydrate_signs_key_under_org_prefix(monkeypatch):
    signed: list[str] = []

    monkeypatch.setattr(
        "dooers.agents.server.storage.hydrate.chat_artifact_object_exists",
        lambda _cfg, key: key.startswith(ORG_PREFIX) and "/t1/" in key,
    )
    monkeypatch.setattr(
        "dooers.agents.server.storage.hydrate.sign_chat_artifact_read_url",
        lambda _cfg, key: signed.append(key) or "https://storage.googleapis.com/signed",
    )
    cfg = AgentConfig(
        database_type="postgres",
        chat_storage_service="dooers",
        dooers_storage_prefix=ORG_PREFIX,
        gcp_storage_bucket="b",
    )
    event = ThreadEvent(
        id="ev1",
        thread_id="t1",
        type="message",
        actor="user",
        content=[WireS2C_DocumentPart(filename="doc.pdf", mime_type="application/pdf", ref_id="r1")],
        created_at=datetime.now(UTC),
    )
    out = await hydrate_thread_events(cfg, [event], _thread())
    assert signed == [f"{ORG_PREFIX}chat-artifacts/v1/a1/t1/r1/doc.pdf"]
    assert out[0].content[0].url == "https://storage.googleapis.com/signed"
