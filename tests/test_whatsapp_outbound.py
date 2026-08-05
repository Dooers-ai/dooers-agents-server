import json

import httpx
import pytest
import respx

from dooers.agents.server.features.channels.whatsapp.outbound import create_dooers_whatsapp_outbound
from dooers.agents.server.handlers.pipeline import HandlerContext
from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.protocol.models import User

TOOLS_BASE = "http://127.0.0.1:8810"
MESSAGES_URL = f"{TOOLS_BASE}/api/v1/messages"
HMAC_SECRET = "test-hmac-secret"


class FakePersistence:
    def __init__(self, secrets: dict | None = None) -> None:
        self._secrets = secrets or {}

    async def get_service_secrets(self, agent_id: str) -> dict:
        return self._secrets


def _ctx() -> HandlerContext:
    return HandlerContext(
        handler=lambda: None,
        agent_id="agent-1",
        message="hi",
        channel="whatsapp",
        channel_meta=None,
        user=User(user_id="+5511999999999"),
    )


def _text_event() -> AgentEvent:
    return AgentEvent(
        send_type="text",
        data={
            "text": "hello",
            "whatsapp": {"to_e164": "+5511988887777", "instance_id": "inst-1"},
        },
    )


@pytest.fixture
def tools_base(monkeypatch):
    import importlib

    import dooers.agents.server.features.channels.whatsapp.config as wa_cfg

    monkeypatch.setenv("DOOERS_WHATSAPP_TOOLS_BASE", TOOLS_BASE)
    importlib.reload(wa_cfg)
    yield
    importlib.reload(wa_cfg)


@pytest.mark.asyncio
@respx.mock
async def test_outbound_posts_signed_message(tools_base):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"ok": True, "send_type": "text"}))
    secrets = {"dooers_whatsapp_instance_hmac_json": json.dumps({"inst-1": HMAC_SECRET})}
    outbound = create_dooers_whatsapp_outbound(FakePersistence(secrets))

    await outbound(_text_event(), _ctx())

    assert route.called
    request = route.calls[0].request
    assert request.headers["X-Message-Signature"].startswith("sha256=")
    body = json.loads(request.content.decode("utf-8"))
    assert body["send_type"] == "text"
    assert body["text"] == "hello"
    assert body["instance_id"] == "inst-1"
    assert body["to_e164"] == "+5511988887777"


@pytest.mark.asyncio
async def test_outbound_skips_without_hmac(tools_base, caplog):
    outbound = create_dooers_whatsapp_outbound(FakePersistence({}))

    with caplog.at_level("INFO"):
        await outbound(_text_event(), _ctx())

    assert "no HMAC in service_secrets" in caplog.text


@pytest.mark.asyncio
async def test_outbound_skips_invalid_payload(tools_base, caplog):
    secrets = {"dooers_whatsapp_instance_hmac_json": json.dumps({"inst-1": HMAC_SECRET})}
    outbound = create_dooers_whatsapp_outbound(FakePersistence(secrets))
    event = AgentEvent(send_type="text", data={"text": "hello"})

    with caplog.at_level("INFO"):
        await outbound(event, _ctx())

    assert "missing to_e164/instance_id" in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_outbound_logs_http_error(tools_base, caplog):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(401, text="invalid signature"))
    secrets = {"dooers_whatsapp_instance_hmac_json": json.dumps({"inst-1": HMAC_SECRET})}
    outbound = create_dooers_whatsapp_outbound(FakePersistence(secrets))

    with caplog.at_level("WARNING"):
        await outbound(_text_event(), _ctx())

    assert "returned error" in caplog.text or "returned 401" in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_outbound_posts_template_via_client(tools_base):
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "send_type": "template"})
    )
    secrets = {"dooers_whatsapp_instance_hmac_json": json.dumps({"inst-1": HMAC_SECRET})}
    outbound = create_dooers_whatsapp_outbound(FakePersistence(secrets))
    event = AgentSend().whatsapp.template(
        "order_update",
        "pt_BR",
        to_e164="+5511988887777",
        instance_id="inst-1",
    )

    await outbound(event, _ctx())

    assert route.called
    body = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert body["send_type"] == "template"
    assert body["template_name"] == "order_update"
    assert body["template_language"] == "pt_BR"
