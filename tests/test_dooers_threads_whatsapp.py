"""WhatsApp sends and delivery receipts in Dooers Threads mode (plan 5, Task 7).

Production reference (origin/main): ``HandlerPipeline._resolve_whatsapp_event``/
``_dispatch_whatsapp_outbound``/``_record_whatsapp_delivery`` (``handlers/pipeline.py``, ~1156-1255),
``create_dooers_whatsapp_outbound`` (``features/channels/whatsapp/outbound.py``) and
``_receipt_moves_forward`` (``features/channels/whatsapp/delivery_receipt.py``).

F6 correction (binding, see the task-7 brief): production sends every plain
``send.text/image/document/audio/contact/template`` through WhatsApp when the thread's channel is
``whatsapp``, not only an explicit ``send.whatsapp.*`` call — the sink reuses production's own
``_resolve_whatsapp_event`` for this.
"""

from __future__ import annotations

from typing import Any

import pytest

import dooers.agents.server.dooers_threads.agent_api as agent_api_module
import dooers.agents.server.features.channels.whatsapp.outbound as outbound_module
from dooers.agents.server.dooers_threads.agent_api import DooersThreadsAgentNotFound
from dooers.agents.server.dooers_threads.persistence import DooersThreadsAgentMemory
from dooers.agents.server.features.channels.whatsapp.delivery_receipt import (
    apply_whatsapp_delivery_receipt,
    bind_agent_server,
)
from dooers.agents.server.protocol.dooers_threads import default_contracts
from tests.parity.model import AGENT_ID, ASSISTANT_NAME
from tests.test_dooers_threads_executor import execute_fixture, setup
from tests.test_dooers_threads_legacy_model import AGENT, DooersThreadsThread
from tests.test_dooers_threads_memory_parity import ContextMemory
from tests.test_dooers_threads_sink_parity import published

WHATSAPP_META = {"whatsapp": {"to_e164": "+5511999999999", "instance_id": "inst-1"}}
TO_E164 = "+5511999999999"
INSTANCE_ID = "inst-1"


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return default_contracts()


class _FakeWhatsappAgentApi:
    """Stands in for ``DooersThreadsAgentApi`` in the sink's dispatch (plan 5 Task 5b's real one is tested

    on its own in ``test_dooers_threads_agent_api.py``): records every ``record_channel_delivery`` call.
    """

    def __init__(self, outcome: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcome = outcome or {"applied": True, "event": {"stub": True}}

    async def record_channel_delivery(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._outcome


def _install_fake_whatsapp(monkeypatch, *, outcome: dict[str, Any]) -> _FakeWhatsappAgentApi:
    """Replace ``context.py``'s own ``create_dooers_whatsapp_outbound``/``DooersThreadsAgentApi`` lookups.

    Both are imported lazily, inside ``dooers_threads_handler_context``, exactly at the point this patches:
    ``from ... import create_dooers_whatsapp_outbound`` / ``from .agent_api import DooersThreadsAgentApi``
    resolve the current module attribute at call time, so patching the attribute here reaches them.
    """
    api = _FakeWhatsappAgentApi(outcome)

    async def _outbound(event, context):
        return outcome.get("_outbound_outcome", {"status": "accepted", "wa_message_id": "wamid.123"})

    monkeypatch.setattr(outbound_module, "create_dooers_whatsapp_outbound", lambda persistence: _outbound)
    monkeypatch.setattr(agent_api_module, "DooersThreadsAgentApi", lambda *args, **kwargs: api)
    return api


def _set_whatsapp_thread(fixture, *, channel_meta: dict[str, Any] | None = WHATSAPP_META) -> None:
    """The fixture's default thread view already has ``channel: "whatsapp"`` (``PUBLIC`` fixture case);

    only ``channel_meta`` (production's ``channel_meta["whatsapp"]``) needs setting here.
    """
    fixture.response["thread"]["channel_meta"] = channel_meta


EXPLICIT_CALLS = {
    "text": lambda send: send.whatsapp.text("Olá", to_e164=TO_E164, instance_id=INSTANCE_ID),
    "image": lambda send: send.whatsapp.image("http://127.0.0.1:9/logo.png", TO_E164, INSTANCE_ID),
    "document": lambda send: send.whatsapp.document("http://127.0.0.1:9/nota.pdf", "nota.pdf", "application/pdf", TO_E164, INSTANCE_ID),
    "audio": lambda send: send.whatsapp.audio("http://127.0.0.1:9/voz.wav", TO_E164, INSTANCE_ID, mime_type="audio/wav"),
    "contact": lambda send: send.whatsapp.contact(TO_E164, INSTANCE_ID, display_name="Ana"),
    "template": lambda send: send.whatsapp.template("boas_vindas", "pt_BR", to_e164=TO_E164, instance_id=INSTANCE_ID),
}
PLAIN_CALLS = {
    "text": lambda send: send.text("Olá"),
    "image": lambda send: send.image("http://127.0.0.1:9/logo.png"),
    "document": lambda send: send.document("http://127.0.0.1:9/nota.pdf", filename="nota.pdf", mime_type="application/pdf"),
    "audio": lambda send: send.audio("http://127.0.0.1:9/voz.wav", mime_type="audio/wav"),
    "contact": lambda send: send.contact(display_name="Ana"),
    "template": lambda send: send.template("boas_vindas", "pt_BR"),
}


# -- F6: explicit send.whatsapp.* and the plain send in a WhatsApp thread, same outbound call --


@pytest.mark.parametrize("method", ["text", "image", "document", "audio", "contact", "template"])
@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "plain_in_whatsapp_thread"])
async def test_whatsapp_send_in_dooers_threads_matches_production(private_key, contracts, monkeypatch, method, explicit):
    api = _install_fake_whatsapp(monkeypatch, outcome={"applied": True, "event": {"stub": True}})
    async with setup(private_key, contracts) as fixture:
        _set_whatsapp_thread(fixture)

        async def handler(_on, send, *_args):
            calls = EXPLICIT_CALLS if explicit else PLAIN_CALLS
            yield calls[method](send)

        response = await execute_fixture(fixture, handler)

        assert response.status_code == 200
        assert response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert output["payload"]["whatsapp"] == {"to_e164": TO_E164, "instance_id": INSTANCE_ID}
        # production's _record_whatsapp_delivery: same thread_id/event_id/status/provider_message_id.
        (call,) = api.calls
        stored_event_id = fixture.outputs[next(iter(fixture.outputs))][1]["event"]["event_id"]
        assert call["thread_id"] == fixture.envelope["thread_id"]
        assert call["event_id"] == stored_event_id
        assert call["status"] == "accepted"
        assert call["provider_message_id"] == "wamid.123"
        assert call["error"] is None
        assert call["error_code"] is None


async def test_whatsapp_send_failed_outcome_records_error_fields(private_key, contracts, monkeypatch):
    """production keeps ``error_code``/``error_message`` (truncated to 500) on a ``failed`` outcome too."""
    outcome = {"_outbound_outcome": {"status": "failed", "error_code": 131047, "error_message": "número inválido"}}
    api = _install_fake_whatsapp(monkeypatch, outcome=outcome)
    async with setup(private_key, contracts) as fixture:
        _set_whatsapp_thread(fixture)

        async def handler(_on, send, *_args):
            yield send.whatsapp.text("Olá", to_e164=TO_E164, instance_id=INSTANCE_ID)

        response = await execute_fixture(fixture, handler)

        assert response.status_code == 200 and response.json()["state"] == "responded"
        (call,) = api.calls
        assert call["status"] == "failed"
        assert call["error_code"] == 131047
        assert call["error"] == "número inválido"


async def test_whatsapp_send_skipped_when_outbound_returns_an_unexpected_status(private_key, contracts, monkeypatch):
    """production ignores an outcome whose status is not ``accepted``/``failed`` (no record written)."""
    outcome = {"_outbound_outcome": {"status": "queued"}}
    api = _install_fake_whatsapp(monkeypatch, outcome=outcome)
    async with setup(private_key, contracts) as fixture:
        _set_whatsapp_thread(fixture)

        async def handler(_on, send, *_args):
            yield send.whatsapp.text("Olá", to_e164=TO_E164, instance_id=INSTANCE_ID)

        response = await execute_fixture(fixture, handler)

        assert response.status_code == 200 and response.json()["state"] == "responded"
        assert api.calls == []


async def test_non_whatsapp_thread_plain_send_is_unaffected(private_key, contracts, monkeypatch):
    """F6: a plain send in a non-WhatsApp thread gets no ``whatsapp`` field and no outbound call."""
    api = _install_fake_whatsapp(monkeypatch, outcome={"applied": True, "event": {"stub": True}})
    async with setup(private_key, contracts) as fixture:
        fixture.response["thread"]["channel"] = "dooers-platform"
        fixture.response["thread"]["channel_meta"] = None

        async def handler(_on, send, *_args):
            yield send.text("Olá")

        response = await execute_fixture(fixture, handler)

        assert response.status_code == 200 and response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert "whatsapp" not in output["payload"]
        assert api.calls == []


async def test_whatsapp_thread_without_the_whatsapp_meta_is_unaffected(private_key, contracts, monkeypatch):
    """F6: ``channel == "whatsapp"`` with no ``channel_meta["whatsapp"]`` resolves no route either."""
    api = _install_fake_whatsapp(monkeypatch, outcome={"applied": True, "event": {"stub": True}})
    async with setup(private_key, contracts) as fixture:
        _set_whatsapp_thread(fixture, channel_meta={"instance": "inst-1"})

        async def handler(_on, send, *_args):
            yield send.text("Olá")

        response = await execute_fixture(fixture, handler)

        assert response.status_code == 200 and response.json()["state"] == "responded"
        (output,) = published(fixture)
        assert "whatsapp" not in output["payload"]
        assert api.calls == []


# -- F15: apply_whatsapp_delivery_receipt in Dooers Threads mode --


class _FakeDooersThreadsAgentApi:
    def __init__(self, deliveries: dict[str, dict[str, Any]]) -> None:
        self._deliveries = deliveries
        self.recorded: list[dict[str, Any]] = []

    async def find_channel_delivery(self, provider_message_id: str) -> dict[str, Any]:
        current = self._deliveries.get(provider_message_id)
        if current is None:
            raise DooersThreadsAgentNotFound("NOT_FOUND")
        return dict(current)

    async def record_channel_delivery(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        self._deliveries[kwargs["provider_message_id"]] = {
            "thread_id": kwargs["thread_id"],
            "event_id": kwargs["event_id"],
            "status": kwargs["status"],
        }
        return {"applied": True, "event": {"stub": True}}


class _LocalPersistence:
    """A weak-referenceable stand-in for the server's local (secrets/settings) persistence."""


class _DooersThreadsModeServer:
    """What :func:`bind_agent_server` and the Dooers Threads branch need from ``AgentServer``."""

    def __init__(self, api: _FakeDooersThreadsAgentApi) -> None:
        from types import SimpleNamespace

        self._persistence = _LocalPersistence()
        self._config = SimpleNamespace(thread_management="dooers_threads")
        self._api = api

    def _dooers_threads_api(self) -> _FakeDooersThreadsAgentApi:
        return self._api


def _receipt_body(wa_message_id: str, status: str, **extra: Any) -> bytes:
    import json

    payload = {"delivery_receipt": True, "wa_message_id": wa_message_id, "status": status, **extra}
    return json.dumps(payload).encode()


async def test_receipt_moves_status_forward_only_in_dooers_threads_mode():
    api = _FakeDooersThreadsAgentApi({"wamid.1": {"thread_id": "th-1", "event_id": "ev-1", "status": "accepted"}})
    server = _DooersThreadsModeServer(api)
    bind_agent_server(server)

    applied = await apply_whatsapp_delivery_receipt(server._persistence, "agent-1", _receipt_body("wamid.1", "read"))
    assert applied is True
    (call,) = api.recorded
    assert call == {
        "thread_id": "th-1",
        "event_id": "ev-1",
        "status": "read",
        "provider_message_id": "wamid.1",
        "error": None,
        "error_code": None,
    }

    # equal status: production's own rank check treats it as moving forward (idempotent re-send).
    applied = await apply_whatsapp_delivery_receipt(server._persistence, "agent-1", _receipt_body("wamid.1", "read"))
    assert applied is True
    assert len(api.recorded) == 2

    # downgrade: delivered after read does not move the rank forward; kept, no further API call.
    applied = await apply_whatsapp_delivery_receipt(server._persistence, "agent-1", _receipt_body("wamid.1", "delivered"))
    assert applied is True
    assert len(api.recorded) == 2  # unchanged: no new record_channel_delivery call


async def test_receipt_failed_is_final_in_dooers_threads_mode():
    api = _FakeDooersThreadsAgentApi({"wamid.1": {"thread_id": "th-1", "event_id": "ev-1", "status": "failed"}})
    server = _DooersThreadsModeServer(api)
    bind_agent_server(server)

    applied = await apply_whatsapp_delivery_receipt(server._persistence, "agent-1", _receipt_body("wamid.1", "delivered"))
    assert applied is True
    assert api.recorded == []  # failed sticks: no record written


async def test_receipt_for_unknown_message_matches_production():
    api = _FakeDooersThreadsAgentApi({})
    server = _DooersThreadsModeServer(api)
    bind_agent_server(server)

    applied = await apply_whatsapp_delivery_receipt(server._persistence, "agent-1", _receipt_body("wamid.unknown", "delivered"))
    assert applied is False
    assert api.recorded == []


async def test_receipt_with_error_fields_maps_error_message_to_error():
    api = _FakeDooersThreadsAgentApi({"wamid.1": {"thread_id": "th-1", "event_id": "ev-1", "status": "accepted"}})
    server = _DooersThreadsModeServer(api)
    bind_agent_server(server)

    applied = await apply_whatsapp_delivery_receipt(
        server._persistence,
        "agent-1",
        _receipt_body("wamid.1", "failed", error_code=130497, error_message="restricted"),
    )
    assert applied is True
    (call,) = api.recorded
    assert call["status"] == "failed"
    assert call["error_code"] == 130497
    assert call["error"] == "restricted"


# -- F7: assistant_delivery_data + channel_delivery.updated fold onto data.delivery on read-back --


def _whatsapp_thread_view(thread: DooersThreadsThread) -> dict[str, Any]:
    return {
        "thread_id": thread.thread_id,
        "title": "Chat",
        "queue": None,
        "channel": "whatsapp",
        "channel_meta": dict(WHATSAPP_META),
        "created_at": "2026-11-12T10:00:00Z",
        "owner_instance_agent_id": AGENT_ID,
    }


async def test_assistant_message_in_whatsapp_thread_carries_delivery_data():
    thread = DooersThreadsThread(default_contracts())
    thread.message(AGENT, [{"type": "text", "text": "Olá! Como posso ajudar?"}])
    context = ContextMemory(thread, thread_view=_whatsapp_thread_view(thread))
    memory = DooersThreadsAgentMemory(context, assistant_name=ASSISTANT_NAME)

    events = await memory.get_history_raw()

    assert events[0].data == {"delivery": {"channel": "whatsapp", "to_e164": TO_E164, "instance_id": INSTANCE_ID}}


async def test_channel_delivery_updated_folds_onto_data_delivery_on_read_back():
    thread = DooersThreadsThread(default_contracts())
    message = thread.message(AGENT, [{"type": "text", "text": "Olá! Como posso ajudar?"}])
    thread.append(
        "channel_delivery.updated",
        AGENT,
        {"event_id": message["event_id"], "channel": "whatsapp", "status": "accepted"},
    )
    thread.append(
        "channel_delivery.updated",
        AGENT,
        {
            "event_id": message["event_id"],
            "channel": "whatsapp",
            "status": "failed",
            "provider_message_id": "wamid.123",
            "error_code": 131047,
            "error": "número inválido",
        },
    )
    context = ContextMemory(thread, thread_view=_whatsapp_thread_view(thread))
    memory = DooersThreadsAgentMemory(context, assistant_name=ASSISTANT_NAME)

    events = await memory.get_history_raw()

    assert events[0].data == {
        "delivery": {
            "channel": "whatsapp",
            "to_e164": TO_E164,
            "instance_id": INSTANCE_ID,
            "status": "failed",
            "wa_message_id": "wamid.123",
            "error_code": 131047,
            "error_message": "número inválido",
        }
    }
    # channel_delivery.updated is not an event of its own (production has none for it either).
    assert len(events) == 1


async def test_channel_delivery_updated_leaves_an_earlier_field_a_later_update_omits():
    thread = DooersThreadsThread(default_contracts())
    message = thread.message(AGENT, [{"type": "text", "text": "Olá!"}])
    thread.append(
        "channel_delivery.updated",
        AGENT,
        {
            "event_id": message["event_id"],
            "channel": "whatsapp",
            "status": "failed",
            "error_code": 131047,
            "error": "número inválido",
        },
    )
    thread.append(
        "channel_delivery.updated",
        AGENT,
        {"event_id": message["event_id"], "channel": "whatsapp", "status": "read"},
    )
    context = ContextMemory(thread, thread_view=_whatsapp_thread_view(thread))
    memory = DooersThreadsAgentMemory(context, assistant_name=ASSISTANT_NAME)

    events = await memory.get_history_raw()

    delivery = events[0].data["delivery"]
    assert delivery["status"] == "read"
    # later update carried no error fields: the earlier ones stay, as production's cumulative merge keeps them.
    assert delivery["error_code"] == 131047
    assert delivery["error_message"] == "número inválido"
