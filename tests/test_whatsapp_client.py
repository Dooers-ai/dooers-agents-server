import json

import httpx
import pytest
import respx

from dooers.tools.whatsapp import WhatsAppClient, WhatsAppInstance
from dooers.tools.whatsapp.runtime import bind_handler_context, reset_handler_context

TOOLS_BASE = "http://127.0.0.1:8810"
MESSAGES_URL = f"{TOOLS_BASE}/api/v1/messages"
GET_INSTANCE_URL = f"{TOOLS_BASE}/api/v1/agent/instances/get"
HMAC_SECRET = "test-hmac-secret"
AGENT_ID = "agent-1"
INSTANCE_ID = "inst-1"


class FakePersistence:
    def __init__(self, secrets: dict | None = None) -> None:
        self._secrets = secrets or {}

    async def get_service_secrets(self, agent_id: str) -> dict:
        return self._secrets


@pytest.fixture
def tools_base(monkeypatch):
    import importlib

    import dooers.agents.server.features.channels.whatsapp.config as wa_cfg

    monkeypatch.setenv("DOOERS_WHATSAPP_TOOLS_BASE", TOOLS_BASE)
    importlib.reload(wa_cfg)
    yield
    importlib.reload(wa_cfg)


def _secrets() -> dict:
    return {"dooers_whatsapp_instance_hmac_json": json.dumps({INSTANCE_ID: HMAC_SECRET})}


@pytest.mark.asyncio
@respx.mock
async def test_whatsapp_client_text_send(tools_base):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"ok": True, "send_type": "text"}))
    persistence = FakePersistence(_secrets())
    tokens = bind_handler_context(persistence=persistence, agent_id=AGENT_ID)
    try:
        wa = WhatsAppClient(INSTANCE_ID)
        result = await wa.text.send(to_e164="+5511988887777", text="hello")
    finally:
        reset_handler_context(tokens)

    assert result["ok"] is True
    assert route.called
    body = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert body["send_type"] == "text"
    assert body["instance_id"] == INSTANCE_ID


@pytest.mark.asyncio
async def test_whatsapp_client_requires_handler_context(tools_base):
    with pytest.raises(Exception, match="handler turn"):
        WhatsAppClient(INSTANCE_ID)


@pytest.mark.asyncio
async def test_whatsapp_client_instances_list_ids_only(tools_base):
    persistence = FakePersistence(_secrets())
    tokens = bind_handler_context(persistence=persistence, agent_id=AGENT_ID)
    try:
        wa = WhatsAppClient(INSTANCE_ID)
        instances = await wa.instances.list(with_details=False)
    finally:
        reset_handler_context(tokens)

    assert instances == [WhatsAppInstance(instance_id=INSTANCE_ID)]


@pytest.mark.asyncio
@respx.mock
async def test_whatsapp_client_instances_get(tools_base):
    respx.post(GET_INSTANCE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "instance_id": INSTANCE_ID,
                "phone_number": "+5511999999999",
                "status": "open",
                "last_error": None,
            },
        )
    )
    persistence = FakePersistence(_secrets())
    tokens = bind_handler_context(persistence=persistence, agent_id=AGENT_ID)
    try:
        wa = WhatsAppClient(INSTANCE_ID)
        inst = await wa.instances.get(INSTANCE_ID)
    finally:
        reset_handler_context(tokens)

    assert inst.instance_id == INSTANCE_ID
    assert inst.phone_number == "+5511999999999"
    assert inst.status == "open"
