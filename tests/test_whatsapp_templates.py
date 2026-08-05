from dooers.agents.server.features.channels.whatsapp.messages import body_for_event
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.protocol.models import User


def _ctx(*, channel: str, channel_meta: dict | None = None) -> HandlerContext:
    return HandlerContext(
        handler=lambda: None,
        agent_id="agent-1",
        message="hi",
        channel=channel,
        channel_meta=channel_meta,
        user=User(user_id="+5511999999999"),
    )


def test_outbound_body_for_template_event():
    event = AgentEvent(
        send_type="template",
        data={
            "template_name": "order_update",
            "template_language": "pt_BR",
            "template_components": [
                {"type": "body", "parameters": [{"type": "text", "text": "123"}]}
            ],
            "whatsapp": {"to_e164": "+5511988887777", "instance_id": "inst-1"},
        },
    )
    payload = body_for_event(event)
    assert payload is not None
    assert payload["send_type"] == "template"
    assert payload["template_name"] == "order_update"
    assert payload["template_language"] == "pt_BR"
    assert len(payload["template_components"]) == 1


def test_resolve_whatsapp_template_event():
    event = AgentSend().whatsapp.template(
        "welcome",
        "en_US",
        to_e164="+5511988887777",
        instance_id="inst-1",
        template_components=[{"type": "body", "parameters": []}],
    )
    resolved = HandlerPipeline._resolve_whatsapp_event(event, _ctx(channel="whatsapp"))
    assert resolved.send_type == "template"
    assert resolved.data["template_name"] == "welcome"
    assert resolved.data["template_language"] == "en_US"
    assert resolved.data["whatsapp"]["instance_id"] == "inst-1"
