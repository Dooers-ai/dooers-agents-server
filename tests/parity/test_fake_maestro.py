"""FakeMaestro: the in-memory Maestro the parity suite runs the SDK's Maestro mode against."""

from __future__ import annotations

import httpx
import pytest

from dooers.agents.server.maestro import create_text_delivery_adapter
from dooers.agents.server.protocol.models import User
from tests.parity.fake_maestro import (
    CALLBACK_BASE_URL,
    FakeMaestro,
    FakeMaestroRejected,
    legacy_thread_view,
    legacy_view,
    private_resources,
    shared_contracts,
)

ANA = User(user_id="user-1", user_name="Ana Souza", user_email="ana@example.com")


async def _deliver(fake, handler, content=None, *, title="Atendimento"):
    thread_id = fake.create_thread(ANA, title=title)
    trigger = fake.publish(thread_id, ANA, content or [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    (delivery,) = fake.deliveries_for(trigger)
    adapter = create_text_delivery_adapter(fake.config, handler, private_resources)
    response = await fake.deliver(adapter, delivery)
    return thread_id, delivery, response


async def test_text_reply_becomes_an_agent_message_event():
    fake = FakeMaestro()
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen["message"] = on.message
        seen["user"] = on.context.user
        yield send.run_start()
        yield send.text("Oi, Ana!")
        yield send.run_end()

    with fake.serving():
        thread_id, delivery, response = await _deliver(fake, handler)

    assert response.status_code == 200 and response.json()["state"] == "responded"
    assert seen == {"message": "Olá", "user": User(user_id="user-1", user_name="Ana Souza")}
    assert [event["type"] for event in fake.events[thread_id]] == ["message.created", "run.updated", "message.created", "run.updated"]
    human, start, reply, end = fake.events[thread_id]
    assert (start["payload"]["status"], end["payload"]["status"]) == ("running", "succeeded")
    assert start["payload"]["run_id"] == reply["payload"]["run_id"] == end["payload"]["run_id"]
    assert start["payload"]["trigger_event_id"] == human["event_id"]
    assert human["actor"]["kind"] == "human" and human["actor"]["principal_id"] == "user-1"
    assert reply["actor"] == {
        "kind": "agent",
        "principal_id": fake.config.instance_agent_id,
        "instance_agent_id": fake.config.instance_agent_id,
        "participant_id": delivery.envelope["recipient_participant_id"],
        "display_name": "Assistente",
    }
    assert reply["payload"]["content"] == [{"type": "text", "text": "Oi, Ana!"}]
    assert reply["causation_event_id"] == human["event_id"] == reply["root_event_id"]
    assert reply["execution_id"] == delivery.execution_id
    assert delivery.progress == ["admitted", "run_started", "run_finished"]
    assert (delivery.state, delivery.reason_code) == ("responded", "responded")
    for event in fake.events[thread_id]:
        assert shared_contracts().is_valid(f"Public.Event.{event['type']}", event)
    assert fake.rejections == []


async def test_send_refused_by_the_sink_completes_failed_without_an_event():
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        # send.whatsapp.* is the send function the sink still refuses (plan 5).
        yield send.whatsapp.text("Oi", to_e164="+5511999990000", instance_id="instancia-1")

    with fake.serving():
        thread_id, delivery, response = await _deliver(fake, handler)

    assert response.json()["state"] == "failed"
    assert (delivery.state, delivery.reason_code) == ("failed", "unsupported_capability")
    assert [event["actor"]["kind"] for event in fake.events[thread_id]] == ["human"]


def test_publish_outside_the_contract_is_rejected():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    image = {"type": "image", "url": "https://cdn.example.com/foto.png", "mime_type": "image/png"}
    with pytest.raises(FakeMaestroRejected) as error:
        fake.publish(thread_id, ANA, [{"type": "text", "text": "Veja"}, image], client_event_id="client-1")
    assert error.value.action == "message.create"
    assert "image" in str(error.value)
    assert fake.events[thread_id] == []


def test_form_response_needs_an_open_form_interaction():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    with pytest.raises(FakeMaestroRejected) as error:
        fake.respond_form(thread_id, ANA, {"cancelled": False, "values": {"nome": "Ana"}})
    assert error.value.action == "form.response"


def test_callbacks_outside_the_contract_get_contract_errors():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    trigger = fake.publish(thread_id, ANA, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    (delivery,) = fake.deliveries_for(trigger)
    url = f"{fake.config.callback_base_url}/executions/{delivery.execution_id}/heartbeat"
    headers = {"Authorization": f"Bearer {fake.core_token}", "Maestro-Execution-Capability": delivery.capability}

    invalid = fake.handle(httpx.Request("POST", url, json={"lease_fence": "1", "extra": True}, headers=headers))
    stranger = fake.handle(httpx.Request("POST", url, json={"lease_fence": "1"}, headers={**headers, "Authorization": "Bearer x.y.z"}))
    stale = fake.handle(httpx.Request("POST", url, json={"lease_fence": "2"}, headers=headers))
    unknown = fake.handle(httpx.Request("GET", f"{fake.config.callback_base_url}/threads", headers=headers))

    assert [r.status_code for r in (invalid, stranger, stale, unknown)] == [400, 401, 409, 404]
    for response in (invalid, stranger, stale, unknown):
        assert shared_contracts().is_valid("Public.Error", response.json())
    assert fake.rejections == [
        ("heartbeat", "INVALID_REQUEST"),
        ("heartbeat", "UNAUTHENTICATED"),
        ("heartbeat", "LEASE_STALE"),
        ("GET /threads", "NOT_FOUND"),
    ]


async def test_legacy_view_maps_message_events_without_inventing_fields():
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        yield send.text("Oi, Ana!")

    with fake.serving():
        thread_id, _delivery, _response = await _deliver(fake, handler)
    human, reply = (legacy_view(event) for event in fake.events[thread_id])

    assert human["id"] == fake.events[thread_id][0]["event_id"] and human["thread_id"] == thread_id
    assert (human["type"], human["actor"], human["author"]) == ("message", "user", "Ana Souza")
    # The Maestro actor has no e-mail, run or client event id: the view leaves them empty.
    assert human["user"] == User(user_id="user-1", user_name="Ana Souza").model_dump()
    assert (human["run_id"], human["client_event_id"], human["data"]) == (None, None, None)
    assert (reply["actor"], reply["author"], reply["user"]) == ("assistant", "Assistente", User(user_id="").model_dump())
    assert reply["content"] == [{"type": "text", "text": "Oi, Ana!"}]
    with pytest.raises(ValueError, match="execution.started"):
        legacy_view({**fake.events[thread_id][0], "type": "execution.started"})


async def test_legacy_thread_view_maps_the_maestro_thread():
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        yield send.text("Oi")

    with fake.serving():
        thread_id, _delivery, _response = await _deliver(fake, handler, title="Atendimento")
    view = legacy_thread_view(fake.threads[thread_id], fake.events[thread_id])

    assert (view["id"], view["agent_id"], view["title"], view["queue"]) == (
        thread_id,
        fake.config.instance_agent_id,
        "Atendimento",
        None,
    )
    assert (view["organization_id"], view["workspace_id"]) == (fake.config.organization_id, fake.config.workspace_id)
    assert view["owner"] == User(user_id="user-1", user_name="Ana Souza").model_dump()
    # Production lists the agent among the users, after the person.
    assert view["users"] == [view["owner"], User(user_id=fake.config.instance_agent_id, user_name="Assistente").model_dump()]


async def test_artifacts_upload_hydrate_and_link():
    fake = FakeMaestro(artifact_mime_types=("text/plain",))
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen["parts"] = [(part.type, getattr(part, "data", None), getattr(part, "filename", None)) for part in on.content]
        yield send.artifact(b"recibo 42", filename="recibo.txt", mime_type="text/plain")

    with fake.serving():
        note = fake.upload(ANA, b"nota fiscal", filename="nota.txt", mime_type="text/plain")
        thread_id, _delivery, response = await _deliver(fake, handler, [{"type": "text", "text": "Segue"}, note])

    assert response.json()["state"] == "responded"
    assert seen["parts"] == [("text", None, None), ("document", b"nota fiscal", "nota.txt")]
    reply = fake.events[thread_id][1]
    (attachment,) = reply["payload"]["content"]
    assert fake.artifacts[attachment["artifact_id"]]["data"] == b"recibo 42"
    assert {artifact["state"] for artifact in fake.artifacts.values()} == {"linked"}
    assert legacy_view(reply)["content"] == [
        {
            "type": "document",
            "url": None,
            "filename": "recibo.txt",
            "mime_type": "text/plain",
            "size_bytes": 9,
            "ref_id": attachment["artifact_id"],
        }
    ]


def test_artifact_link_only_for_files_linked_up_to_the_cutoff():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    first = fake.publish(thread_id, ANA, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    (early,) = fake.deliveries_for(first)
    note = fake.upload(ANA, b"nota fiscal", filename="nota.txt", mime_type="text/plain")
    second = fake.publish(thread_id, ANA, [note], client_event_id="client-2")
    (late,) = fake.deliveries_for(second)

    def issue(delivery):
        url = f"{CALLBACK_BASE_URL}/executions/{delivery.execution_id}/artifacts/{note['artifact_id']}/links"
        headers = {"Authorization": f"Bearer {fake.core_token}", "Maestro-Execution-Capability": delivery.capability}
        return fake.handle(httpx.Request("POST", url, headers=headers, content=b"{}"))

    # Linked after the first delivery's cutoff: Maestro answers 404, as for a file of another thread.
    assert issue(early).status_code == 404
    response = issue(late)
    assert response.status_code == 200 and response.json()["url"].startswith(f"{CALLBACK_BASE_URL}/artifact-links/")
    assert fake.rejections == [("artifact.link.issue", "NOT_FOUND")]


@pytest.mark.parametrize(
    "outcome,outputs,accepted",
    [
        ("responded", [], False),
        ("responded", ["run.update", "tool_call.create"], False),
        ("responded", ["run.update", "message.create"], True),
        ("responded", ["form.create"], True),
        ("responded", ["chart.create"], True),
        ("silent", [], True),
        ("silent", ["run.update", "reasoning.create", "tool_call.create", "thread.update"], True),
        ("silent", ["run.update", "message.create"], False),
        ("silent", ["form.create"], False),
    ],
)
def test_complete_enforces_the_maestro_outcome_rule(outcome, outputs, accepted):
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    trigger = fake.publish(thread_id, ANA, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    (delivery,) = fake.deliveries_for(trigger)
    delivery.output_kinds.extend(outputs)
    url = f"{fake.config.callback_base_url}/executions/{delivery.execution_id}/complete"
    headers = {
        "Authorization": f"Bearer {fake.core_token}",
        "Maestro-Execution-Capability": delivery.capability,
        "Idempotency-Key": f"complete:{delivery.envelope['delivery_id']}:{delivery.envelope['phase_id']}",
    }
    body = {"lease_fence": delivery.envelope["lease_fence"], "outcome": outcome, "reason_code": "x_reason"}

    response = fake.handle(httpx.Request("POST", url, json=body, headers=headers))

    assert (response.status_code == 200) is accepted, response.text
    if not accepted:
        assert fake.rejections == [("complete", "VALIDATION_FAILED")]
        assert shared_contracts().is_valid("Public.Error", response.json())


async def test_stream_deltas_are_ephemeral_and_the_final_text_is_one_event():
    fake = FakeMaestro(product_capabilities=("stream.delta",))

    async def handler(on, send, memory, analytics, settings):
        stream = send.open_text()
        await stream.delta("Olá, ")
        await stream.delta("mundo")
        await stream.finish()

    with fake.serving():
        thread_id, delivery, response = await _deliver(fake, handler)

    assert response.json()["state"] == "responded"
    assert "".join(chunk["delta"] for chunk in delivery.stream_chunks) == "Olá, mundo"
    assert fake.events[thread_id][1]["payload"]["content"] == [{"type": "text", "text": "Olá, mundo"}]


async def test_context_pages_cover_the_whole_history():
    fake = FakeMaestro()
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen["raw"] = len(await memory.get_history_raw(limit=1000))
        seen["thread"] = (await memory.get_thread()).thread_id
        yield send.text("Li tudo.")

    thread_id = fake.create_thread(ANA, title=None)
    for number in range(1, 105):
        fake.publish(thread_id, ANA, [{"type": "text", "text": f"Mensagem {number}"}], client_event_id=f"client-{number}")
    trigger = fake.publish(thread_id, ANA, [{"type": "text", "text": "Última"}], client_event_id="client-105")
    (delivery,) = fake.deliveries_for(trigger)
    with fake.serving():
        response = await fake.deliver(create_text_delivery_adapter(fake.config, handler, private_resources), delivery)

    assert response.json()["state"] == "responded"
    assert seen == {"raw": 105, "thread": thread_id}
    assert delivery.context_pages > 2
