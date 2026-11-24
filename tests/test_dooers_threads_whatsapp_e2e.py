"""Opt-in: a WhatsApp message round trip, end to end, on ``thread_management="dooers_threads"`` (plan 5, task 8).

Set ``DOOERS_TEST_WHATSAPP_E2E=1`` to run it (``tests/test_dooers_threads_journal.py``'s own convention for an
opt-in suite: skipped unless the flag is set, same as a dedicated-database suite is skipped without one).

It proves, locally, the shape production's WhatsApp webhook has (``examples/fastapi_whatsapp_webhook.py``):
an inbound webhook calls ``AgentServer.dispatch`` on a Dooers Threads-mode agent; the trigger publishes over the
agent API (``DooersThreadsIngress.publish``) and is delivered for real through ``create_text_delivery_adapter`` —
the same adapter ``tests/parity/harness.py``'s ``_run_dooers_threads_deliveries`` already uses, reached here the
same way ``_run_dooers_threads_server`` reaches it (plan 5, task 8: ``FakeAgentApi`` answers the agent API,
``FakeDooersThreads`` answers the delivery callback protocol, one combined respx route for both — see
``tests/parity/harness.py``'s ``_dooers_threads_or_agent_api``/``_deliver_agent_trigger``, reused here instead of
rebuilt). The handler's ``send.whatsapp.text`` reaches a local fake tools-whatsapp
(``_FakeServices.whatsapp_tools``, the same shape ``tests/test_whatsapp_outbound.py`` asserts — a signed
``X-Message-Signature`` — F23 ruling, reused, not rebuilt). A ``delivered`` receipt and then a ``read``
receipt go through ``apply_whatsapp_delivery_receipt``, the exact call production's own WhatsApp status
webhook makes.

No sandbox, no real tools-whatsapp instance, no real destination number, no network: everything here is a
local simulator in this process (step 4 of plan 5's task 8 is explicitly out of scope and is not attempted).

The final delivery status is checked through ``DooersThreadsAgentApi.find_channel_delivery`` and the thread's own
``channel_delivery.updated`` events, not through ``AgentServer.memory()``'s folded view: reading a thread's
events through the agent API outside a delivery has no thread view to stamp ``data.delivery`` with
(``DooersThreadsExecutionPersistence._channel_state``'s own docstring) — a declared, permanent difference from
legacy already recorded for the WhatsApp scenarios in ``tests/parity/known_gaps.py``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from dooers.agents.server.dooers_threads import create_text_delivery_adapter
from dooers.agents.server.dooers_threads.agent_api import DooersThreadsAgentApi
from dooers.agents.server.dooers_threads.context import DooersThreadsPrivateResources
from dooers.agents.server.features.channels.whatsapp.delivery_receipt import apply_whatsapp_delivery_receipt
from dooers.agents.server.server import AgentServer
from tests.parity.fake_dooers_threads import FakeDooersThreads
from tests.parity.harness import _config, _offline_environment, _prepare_agent_api
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import (
    AGENT_ID,
    ASSISTANT_NAME,
    DASHBOARD_CONNECT,
    ORG_ID,
    WHATSAPP_AGENT_PHONE,
    WHATSAPP_CUSTOMER,
    WHATSAPP_INSTANCE,
    WHATSAPP_OUTBOUND_ID,
    WHATSAPP_SECRETS,
    WHATSAPP_USER,
    WORKSPACE_ID,
    ScenarioInput,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DOOERS_TEST_WHATSAPP_E2E"),
    reason="opt-in: set DOOERS_TEST_WHATSAPP_E2E=1 to run the local WhatsApp round-trip E2E",
)

AGENT_REPLY = "Seu pedido saiu para entrega."
WHATSAPP_CONFIG = {"dooers_whatsapp_service": True}
INBOUND_CHANNEL_META = {
    "whatsapp": {
        "instance_id": WHATSAPP_INSTANCE,
        "from_e164": WHATSAPP_CUSTOMER,
        "agent_phone_e164": WHATSAPP_AGENT_PHONE,
        "wa_message_id": "wamid.in.e2e",
    }
}


async def _whatsapp_webhook_handler(on, send, memory, analytics, settings):
    """Same shape as ``examples/fastapi_whatsapp_webhook.py``'s own handler: echo back over WhatsApp."""
    yield send.run_start()
    yield send.whatsapp.text(AGENT_REPLY, to_e164=WHATSAPP_CUSTOMER, instance_id=WHATSAPP_INSTANCE)
    yield send.run_end()


def _receipt_body(status: str) -> bytes:
    return json.dumps({"delivery_receipt": True, "wa_message_id": WHATSAPP_OUTBOUND_ID, "status": status}).encode()


async def test_whatsapp_round_trip_end_to_end():
    """Inbound webhook -> Dooers Threads thread (external participant) -> reply -> delivered -> read."""
    dooers_threads = FakeDooersThreads(assistant_name=ASSISTANT_NAME)
    spec = ScenarioInput(user=WHATSAPP_USER, channel="whatsapp")
    fake = _prepare_agent_api(spec)
    persistence = MemoryPersistence()

    with _offline_environment(persistence, DASHBOARD_CONNECT, dooers_threads, agent_api=fake) as fakes:
        agent_config = _config(spec, thread_management="dooers_threads", dooers_threads=dooers_threads.config)
        server = AgentServer(agent_config)

        async def resources(scope):
            # The same persistence AgentServer merges the WhatsApp service secrets onto below: a handler
            # output's ``send.whatsapp.*`` reads the HMAC secret from it, same as a mounted agent would.
            return DooersThreadsPrivateResources(scope, server.persistence, {}, {})

        adapter = create_text_delivery_adapter(agent_config, _whatsapp_webhook_handler, resources)
        pending: list[asyncio.Task[None]] = []

        def on_trigger(thread_id: str, trigger_event: dict, delivery_id: str) -> None:
            from tests.parity.harness import _deliver_agent_trigger

            pending.append(
                asyncio.ensure_future(_deliver_agent_trigger(dooers_threads, fake, adapter, thread_id, trigger_event, delivery_id))
            )

        fake.on_trigger = on_trigger

        try:
            await server.ensure_initialized()
            await server.persistence.merge_service_secrets(AGENT_ID, dict(WHATSAPP_SECRETS))

            # -- inbound: the webhook's own job (examples/fastapi_whatsapp_webhook.py) --
            stream = await server.dispatch(
                _whatsapp_webhook_handler,
                AGENT_ID,
                "Oi, quero saber do meu pedido",
                user=WHATSAPP_USER,
                organization_id=ORG_ID,
                workspace_id=WORKSPACE_ID,
                channel="whatsapp",
                channel_meta=dict(INBOUND_CHANNEL_META),
            )
            collected = await stream.collect()
            if pending:
                await asyncio.gather(*pending)

            # The webhook's own thread: new, with the customer as an external (WhatsApp) participant.
            assert stream.is_new_thread is True
            thread_id = stream.thread_id
            # ``stream.thread_id`` is the SDK's own id (a fresh dispatch's ``external_key``, F8); FakeAgentApi
            # keys its threads by its own internal id, found the same way DooersThreadsAgentApi finds it by either.
            agent_thread = next(t for t in fake.threads.values() if t.external_key == thread_id or t.id == thread_id)
            assert any(
                participant["kind"] == "external" and participant["external_id"] == WHATSAPP_CUSTOMER
                for participant in agent_thread.participants
            )

            # The handler ran for real (create_text_delivery_adapter, not a scripted stand-in): its reply —
            # send.whatsapp.text, send_type "text" tagged with data.whatsapp — is one of the collected events.
            replies = [event for event in collected if event.send_type == "text" and event.data.get("whatsapp")]
            assert [reply.data["text"] for reply in replies] == [AGENT_REPLY]

            # The reply reached the local fake tools-whatsapp: a real, signed outbound POST, no network.
            assert fakes.whatsapp_requests, "the agent's reply never reached the fake tools-whatsapp"
            sent = fakes.whatsapp_requests[-1]
            assert sent["signed"] is True
            assert sent["body"]["send_type"] == "text"
            assert sent["body"]["text"] == AGENT_REPLY
            assert sent["body"]["to_e164"] == WHATSAPP_CUSTOMER
            assert sent["body"]["instance_id"] == WHATSAPP_INSTANCE

            # delivered, then read: AgentServer.persistence (bound by bind_agent_server) routes both receipts
            # to the agent API in Dooers Threads mode (tests/parity/known_gaps.py's "agent_api_no_delivery_data": the
            # status is checked through the agent API directly, not through AgentServer.memory()'s folded view).
            api = DooersThreadsAgentApi(agent_config.dooers_threads)
            assert await apply_whatsapp_delivery_receipt(server.persistence, AGENT_ID, _receipt_body("delivered")) is True
            assert (await api.find_channel_delivery(WHATSAPP_OUTBOUND_ID))["status"] == "delivered"

            assert await apply_whatsapp_delivery_receipt(server.persistence, AGENT_ID, _receipt_body("read")) is True
            assert (await api.find_channel_delivery(WHATSAPP_OUTBOUND_ID))["status"] == "read"

            # The final status, visible on the thread's own event lineage (the ThreadEvent's underlying
            # Dooers Threads events): the last channel_delivery.updated of the reply is "read".
            deliveries = [event["payload"]["status"] for event in agent_thread.events if event["type"] == "channel_delivery.updated"]
            assert deliveries[-1] == "read"
        finally:
            await server.close()
