from collections.abc import AsyncGenerator

from dooers.features.channels.whatsapp.thread_id import normalize_e164, whatsapp_thread_id
from dooers.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.handlers.send import AgentEvent, AgentSend
from dooers.protocol.models import User


async def _dummy_handler(*_args) -> AsyncGenerator[AgentEvent, None]:
    if False:
        yield AgentSend().run_end()


def test_whatsapp_thread_id_scoped_by_instance():
    n = normalize_e164("5511981049497")
    inst = "9b2b5c4e-0e2a-4a3f-8c1d-1a0e7f2b3c4d"
    assert whatsapp_thread_id("5511981049497", instance_id=inst) == f"wa:inst:{inst}:{n}"


def test_whatsapp_thread_id_legacy_unchanged():
    n = normalize_e164("+5511981049497")
    assert whatsapp_thread_id("+5511981049497") == f"wa:{n}"


def _ctx(*, channel: str, channel_meta: dict | None = None) -> HandlerContext:
    return HandlerContext(
        handler=_dummy_handler,
        agent_id="agent-1",
        message="hi",
        channel=channel,
        channel_meta=channel_meta,
        user=User(user_id="+5511999999999"),
    )


def test_resolve_whatsapp_event_uses_explicit_event_payload():
    event = AgentSend().whatsapp.text(
        "hello",
        to_e164="+5511988887777",
        instance_id="inst-1",
    )
    resolved = HandlerPipeline._resolve_whatsapp_event(event, _ctx(channel="whatsapp"))
    assert resolved.data["whatsapp"]["to_e164"] == "+5511988887777"
    assert resolved.data["whatsapp"]["instance_id"] == "inst-1"


def test_resolve_whatsapp_event_builds_route_from_context_meta():
    event = AgentSend().text("hello")
    resolved = HandlerPipeline._resolve_whatsapp_event(
        event,
        _ctx(
            channel="whatsapp",
            channel_meta={"whatsapp": {"to_e164": "+5511977776666", "instance_id": "inst-2"}},
        ),
    )
    assert resolved.data["whatsapp"]["to_e164"] == "+5511977776666"
    assert resolved.data["whatsapp"]["instance_id"] == "inst-2"


def test_resolve_whatsapp_event_skips_non_whatsapp_channel():
    event = AgentSend().text("hello")
    resolved = HandlerPipeline._resolve_whatsapp_event(
        event,
        _ctx(
            channel="dooers-platform",
            channel_meta={"whatsapp": {"to_e164": "+5511977776666", "instance_id": "inst-2"}},
        ),
    )
    assert "whatsapp" not in resolved.data
