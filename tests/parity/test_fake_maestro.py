"""FakeMaestro: the in-memory Maestro the parity suite runs the SDK's Maestro mode against."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

import dooers.agents.server.maestro.media_fetch as media_fetch_module
from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.maestro import create_text_delivery_adapter
from dooers.agents.server.protocol.models import User, WorkspaceQueueCatalog
from tests.parity import samples
from tests.parity.fake_maestro import (
    CALLBACK_BASE_URL,
    WEB_CHAT_PERSON_NAME,
    FakeMaestro,
    FakeMaestroRejected,
    private_resources,
    shared_contracts,
)
from tests.parity.model import AGENT_ID, USER
from tests.test_maestro_legacy_model import MaestroThread, _every_send, _runs_only

ANA = User(user_id="user-1", user_name="Ana Souza", user_email="ana@example.com", organization_role="owner", workspace_role="manager")


async def _deliver(fake, handler, content=None, *, title="Atendimento"):
    thread_id = fake.create_thread(ANA, title=title)
    trigger = fake.publish(thread_id, ANA, content or [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    (delivery,) = fake.deliveries_for(trigger)
    adapter = create_text_delivery_adapter(fake.config, handler, private_resources)
    response = await fake.deliver(adapter, delivery)
    return thread_id, delivery, response


def _after_creation(fake: FakeMaestro, thread_id: str) -> list[dict[str, Any]]:
    created, *events = fake.events[thread_id]
    assert created["type"] == "thread.created" and created["sequence"] == "1"
    return events


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
    # The agent's context has what Core says about the web chat person (browsers read the placeholder name).
    assert seen == {"message": "Olá", "user": ANA}
    events = _after_creation(fake, thread_id)
    assert [event["type"] for event in events] == ["message.created", "run.updated", "message.created", "run.updated"]
    human, start, reply, end = events
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


def test_thread_created_is_the_first_event_with_the_sdk_fields():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title="Atendimento")
    (created,) = fake.events[thread_id]
    view = created["payload"]["thread"]
    assert (created["type"], created["sequence"], created["actor"]["kind"]) == ("thread.created", "1", "human")
    assert (view["title"], view["queue"], view["channel"], view["channel_meta"]) == ("Atendimento", None, "dooers-platform", None)
    # The shared event never carries the creator's permissions.
    assert not any(view["permissions"].values())
    assert [p["joined_sequence"] for p in view["participants"]] == ["1", "1"]


async def test_send_whatsapp_publishes_then_fails_without_the_agent_api_scope():
    """F6/Task 7 correction: ``send.whatsapp.*`` is no longer refused by the sink; the message publishes.

    ``FakeMaestro`` fakes the delivery callbacks, not the separate agent API (``threads:manage``):
    the record of the initial delivery status (``MaestroAgentApi.record_channel_delivery``) has no
    credential here, so the turn still fails, but only after the message itself was stored —
    ``tests/test_maestro_whatsapp.py`` covers the agent API wired end to end.
    """
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        yield send.whatsapp.text("Oi", to_e164="+5511999990000", instance_id="instancia-1")

    with fake.serving():
        thread_id, delivery, response = await _deliver(fake, handler)

    assert response.json()["state"] == "failed"
    assert (delivery.state, delivery.reason_code) == ("failed", "handler_error")
    assert [event["actor"]["kind"] for event in _after_creation(fake, thread_id)] == ["human", "agent"]


def test_publish_outside_the_contract_is_rejected():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    image = {"type": "image", "url": "https://cdn.example.com/foto.png", "mime_type": "image/png"}
    link = {"type": "media_link", "kind": "image", "url": "https://cdn.example.com/foto.png"}
    for part in (image, link):
        with pytest.raises(FakeMaestroRejected) as error:
            fake.publish(thread_id, ANA, [{"type": "text", "text": "Veja"}, part], client_event_id="client-1")
        assert error.value.action == "message.create"
    assert _after_creation(fake, thread_id) == []


def test_agent_publish_accepts_at_most_20_parts_as_maestro_does():
    """``messageOf`` (``events.ts``): more than 20 parts is ``PAYLOAD_TOO_LARGE``."""
    fake = FakeMaestro()
    thread_id = fake.create_thread(USER, title=None)
    parts = [{"type": "text", "text": f"parte {index}"} for index in range(21)]

    fake.agent_publish(thread_id, USER, parts[:20], client_event_id="client-1")
    with pytest.raises(FakeMaestroRejected, match="PAYLOAD_TOO_LARGE"):
        fake.agent_publish(thread_id, USER, parts, client_event_id="client-2")
    assert len(fake.events[thread_id]) == 2  # thread.created and the 20 parts


def test_agent_publish_declares_the_person_only_to_the_agent():
    """``AgentServer.dispatch``'s Maestro side: the declared name goes with the message; e-mail and identity IDs only to the agent."""
    fake = FakeMaestro()
    thread_id = fake.create_thread(USER, title=None)
    link = {"type": "media_link", "kind": "image", "url": "https://cdn.example.com/foto.png", "mime_type": "image/png"}
    event = fake.agent_publish(thread_id, USER, [{"type": "text", "text": "Veja"}, link], client_event_id="client-1")

    assert event["actor"]["display_name"] == "Ana Souza" and {"email", "identity_ids"}.isdisjoint(event["actor"])
    assert shared_contracts().is_valid("Public.Event.message.created", fake.events[thread_id][-1])
    actor = fake.agent_view(thread_id)["events"][-1]["actor"]
    assert (actor["email"], actor["identity_ids"]) == ("ana@example.com", ["idp-ana"])
    with pytest.raises(FakeMaestroRejected) as error:
        fake.agent_publish(thread_id, USER, [{"type": "image", "url": "https://cdn.example.com/foto.png"}], client_event_id="client-2")
    assert error.value.action == "agent.event.create"
    (delivery,) = fake.deliveries_for(event)
    assert delivery.envelope["trigger_event_id"] == event["event_id"]


def test_form_response_needs_a_production_form():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    message = fake.publish(thread_id, ANA, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    with pytest.raises(FakeMaestroRejected) as error:
        fake.respond_form(thread_id, ANA, message["event_id"], values={"nome": "Ana"}, cancelled=False)
    assert error.value.action == "form.response" and "NOT_FOUND" in error.value.reason


@pytest.mark.parametrize("cancelled", [False, True])
async def test_form_response_is_a_delivery_to_the_form_author(cancelled):
    fake = FakeMaestro()
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        if on.form_event_id is None:
            yield send.form("Seu nome?", [send.form_text("nome", label="Nome")])
        else:
            seen.update(form_data=on.form_data, form_cancelled=on.form_cancelled, form_event_id=on.form_event_id)
            yield send.text("Obrigado.")

    with fake.serving():
        adapter = create_text_delivery_adapter(fake.config, handler, private_resources)
        thread_id = fake.create_thread(ANA, title=None)
        message = fake.publish(thread_id, ANA, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
        (first,) = fake.deliveries_for(message)
        await fake.deliver(adapter, first)
        form = next(event for event in fake.events[thread_id] if event["type"] == "form.created")
        values = {} if cancelled else {"nome": "Ana"}
        response = fake.respond_form(thread_id, ANA, form["event_id"], values=values, cancelled=cancelled)
        (second,) = fake.deliveries_for(response)
        reply = await fake.deliver(adapter, second)

    assert reply.json()["state"] == "responded"
    assert (response["type"], response["root_event_id"], response["causation_event_id"]) == ("form.responded", response["event_id"], None)
    assert response["payload"] == {"form_event_id": form["event_id"], "values": values, "cancelled": cancelled}
    assert second.envelope["recipient_participant_id"] == form["actor"]["participant_id"]
    assert seen == {"form_data": values, "form_cancelled": cancelled, "form_event_id": form["event_id"]}
    assert fake.rejections == []


async def test_thread_changes_are_applied_and_a_present_person_is_refused(caplog):
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        yield send.update_thread(title="Pedido 42", queue="suporte")
        yield send.add_participant("user-2", user_name="Bia Lima", user_email="bia@example.com")
        # Already in the thread: Maestro refuses it (already_participant) and the handler goes on, as in production.
        yield send.add_participant("user-1")
        yield send.text("Pronto.")

    with fake.serving():
        thread_id, delivery, response = await _deliver(fake, handler)

    thread = fake.threads[thread_id]
    assert response.json()["state"] == "responded"
    assert (thread["title"], thread["queue"], thread["version"]) == ("Pedido 42", "suporte", 3)
    added = thread["participants"][-1]
    # Maestro keeps only the principal: no name, e-mail or identity IDs of the request.
    assert (added["principal_id"], added["display_name"], added["role"]) == ("user-2", WEB_CHAT_PERSON_NAME, "member")
    updated, joined = (event for event in fake.events[thread_id] if event["type"] in {"thread.updated", "participant.added"})
    assert updated["payload"] == {"thread_id": thread_id, "title": "Pedido 42", "queue": "suporte", "version": 2, "state": "active"}
    assert joined["payload"]["participant"] == added and added["joined_sequence"] == joined["sequence"]
    assert fake.rejections == [("outputs", "VALIDATION_FAILED")]
    assert [event["type"] for event in _after_creation(fake, thread_id)][-1] == "message.created"


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
    reply = _after_creation(fake, thread_id)[1]
    (attachment,) = reply["payload"]["content"]
    assert attachment == {
        "type": "attachment",
        "artifact_id": attachment["artifact_id"],
        "filename": "recibo.txt",
        "mime_type": "text/plain",
        "size_bytes": 9,
    }
    assert fake.artifacts[attachment["artifact_id"]]["data"] == b"recibo 42"
    assert {artifact["state"] for artifact in fake.artifacts.values()} == {"linked"}


def test_upload_follows_the_production_file_types():
    fake = FakeMaestro()
    assert fake.upload(ANA, b"OggS audio", filename="recado.ogg", mime_type="audio/ogg")["mime_type"] == "audio/ogg"
    with pytest.raises(FakeMaestroRejected):
        fake.upload(ANA, b"MZ binary", filename="setup.exe", mime_type="application/x-msdownload")


def test_upload_refuses_what_the_content_inspection_refuses():
    fake = FakeMaestro()
    for filename, mime_type, data in [
        ("grafico.png", "image/png", b"\x89PNG\r\n\x1a\nparity-chart"),
        ("relatorio.pdf", "application/pdf", b"%PDF-1.4 parity-report"),
        ("pagina.txt", "text/plain", b"<html></html>"),
    ]:
        with pytest.raises(FakeMaestroRejected):
            fake.upload(ANA, data, filename=filename, mime_type=mime_type)
    assert fake.artifacts == {}
    assert fake.upload(ANA, samples.png_bytes(), filename="grafico.png", mime_type="image/png")["size_bytes"] == len(samples.png_bytes())


async def test_both_uploads_store_and_return_the_canonical_mime():
    """``canonicalMimeType``: the extension decides when the type is inspected or generic; the receipt carries it."""
    fake = FakeMaestro()
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen["parts"] = [(part.type, part.mime_type) for part in on.content if part.type != "text"]
        yield send.artifact(b"a,b\n1,2\n", filename="dados.csv", mime_type="application/octet-stream")
        yield send.artifact(samples.pdf_bytes(), filename="Relatorio.PDF", mime_type="text/html")

    with fake.serving():
        person = fake.upload(ANA, b"x,y\n", filename="entrada.csv", mime_type="application/octet-stream")
        thread_id, _delivery, response = await _deliver(fake, handler, [{"type": "text", "text": "Veja"}, person])

    assert response.json()["state"] == "responded"
    assert person["mime_type"] == "text/csv"
    assert seen["parts"] == [("document", "text/csv")]
    attachments = [
        part for event in _after_creation(fake, thread_id) for part in event["payload"].get("content", []) if part["type"] == "attachment"
    ]
    mime_types = [part["mime_type"] for part in attachments]
    assert mime_types == ["text/csv", "text/csv", "application/pdf"]
    assert {artifact["mime_type"] for artifact in fake.artifacts.values()} == {"text/csv", "application/pdf"}


async def test_context_participants_come_in_the_order_of_their_id_not_of_joining():
    """Maestro lists them ``ORDER BY participants.id`` (a random UUID); the SDK still gives [owner, agent, added]."""
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    owner, agent = fake.threads[thread_id]["participants"]
    owner["participant_id"], agent["participant_id"] = "ffffffff-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        yield send.add_participant("user-2")
        seen["users"] = [user.user_id for user in (await memory.get_thread()).users]
        yield send.text("Pronto.")

    with fake.serving():
        trigger = fake.publish(thread_id, ANA, [{"type": "text", "text": "Oi"}], client_event_id="client-1")
        (delivery,) = fake.deliveries_for(trigger)
        adapter = create_text_delivery_adapter(fake.config, handler, private_resources)
        response = await fake.deliver(adapter, delivery)

    assert response.json()["state"] == "responded"
    assert fake.agent_view(thread_id)["participants"][0]["kind"] == "agent"  # its id sorts first: not the join order
    assert [participant["participant_id"] for participant in fake.agent_view(thread_id)["participants"]] == sorted(
        participant["participant_id"] for participant in fake.threads[thread_id]["participants"]
    )
    assert seen["users"] == ["user-1", AGENT_ID, "user-2"]


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
    # Outside an execution (the harness's read back), any file linked to the thread has a link.
    assert fake.artifact_link(thread_id, note["artifact_id"])["url"].startswith(f"{CALLBACK_BASE_URL}/artifact-links/")
    with pytest.raises(LookupError):
        fake.artifact_link(fake.create_thread(ANA, title=None), note["artifact_id"])


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
    assert _after_creation(fake, thread_id)[1]["payload"]["content"] == [{"type": "text", "text": "Olá, mundo"}]


async def test_context_pages_cover_the_whole_history():
    fake = FakeMaestro()
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen["raw"] = len(await memory.get_history_raw(limit=1000))
        seen["thread"] = (await memory.get_thread()).id
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


# -- The Maestro shapes ``tests/test_maestro_legacy_model.py`` writes by hand --

_ID_KEYS = frozenset({"run_id", "event_id", "trigger_event_id", "participant_id", "thread_id"})
#: Bookkeeping Maestro adds to an agent's message; the conversion does not read it.
_BOOKKEEPING = frozenset({"execution_id", "output_id"})


def _shape(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v, k) for k, v in value.items() if k not in _BOOKKEEPING}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if key in _ID_KEYS or key == "joined_sequence":
        return f"<{key}>"
    return value


def _outputs_shape(events: list[dict[str, Any]]) -> list[tuple[str, str, Any]]:
    return [(event["type"], event["actor"]["kind"], _shape(event["payload"])) for event in events]


async def _offline_resolver(host: str, port: int) -> list[str]:
    raise OSError(f"offline: no DNS for {host}")


@pytest.mark.parametrize("build", [_every_send, _runs_only], ids=["every_send", "runs"])
async def test_legacy_model_test_shapes_are_the_ones_maestro_stores(build):
    """The events ``MaestroThread.publish`` writes by hand equal what FakeMaestro stores for the same sends.

    Same ``AgentSend`` list, once through the SDK's sink and FakeMaestro (offline media URLs:
    ``media_link``), once through the round-trip test's ``MaestroThread``. Ids, sequences and
    Maestro's bookkeeping on agent messages are left out.
    """
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        for event in build(send, on.context.event_id):
            yield event

    with fake.serving(), patch.object(media_fetch_module, "_system_resolver", _offline_resolver):
        thread_id, _delivery, response = await _deliver(fake, handler, title="Nova conversa")
    assert response.json()["state"] == "responded" and fake.rejections == []
    stored = _after_creation(fake, thread_id)[1:]

    person = _after_creation(fake, thread_id)[0]["actor"]
    thread = MaestroThread(shared_contracts())
    trigger = thread.message(person, [{"type": "text", "text": "Olá"}])
    thread.publish(build(AgentSend(), trigger["event_id"]), trigger, actor=stored[0]["actor"])

    assert _outputs_shape(stored) == _outputs_shape(thread.events[1:])


# -- Core's person, workspace and thread data in the agent's context (Maestro's agent-profiles.ts and callbacks.ts) --

BIA = User(user_id="user-2", user_name="Bia Lima", user_email="bia@example.com", identity_ids=["idp-bia"])
QUEUE_CATALOG = (WorkspaceQueueCatalog(slug="suporte", name="Suporte"), WorkspaceQueueCatalog(slug="vendas", name="Vendas"))
ROLES = ("system_role", "organization_role", "workspace_role")


def _human(people: list[dict[str, Any]], principal_id: str) -> dict[str, Any]:
    return next(person for person in people if person["kind"] == "human" and person["principal_id"] == principal_id)


async def test_browsers_read_the_placeholder_and_the_agent_context_has_the_core_profile():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title="Atendimento")
    event = fake.publish(thread_id, ANA, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    assert event["actor"]["display_name"] == WEB_CHAT_PERSON_NAME and "email" not in event["actor"]
    assert _human(fake.threads[thread_id]["participants"], "user-1")["display_name"] == WEB_CHAT_PERSON_NAME
    view = fake.agent_view(thread_id)
    for person in (_human(view["participants"], "user-1"), view["trigger_event"]["actor"]):
        assert (person["display_name"], person["email"], person["identity_ids"]) == ("Ana Souza", "ana@example.com", [])
        assert [person[role] for role in ROLES] == ["user", "owner", "manager"]
    # thread.created lists the people too (``enrichEvent``).
    created = view["events"][0]["payload"]["thread"]
    assert _human(created["participants"], "user-1")["workspace_role"] == "manager"


async def test_core_person_without_a_name_keeps_the_placeholder_and_a_null_email():
    fake = FakeMaestro(core_people=[User(user_id="user-1")])
    thread_id = fake.create_thread(User(user_id="user-1"), title=None)
    person = _human(fake.agent_view(thread_id)["participants"], "user-1")
    assert (person["display_name"], person["email"], person["system_role"]) == (WEB_CHAT_PERSON_NAME, None, "user")


async def test_declared_person_keeps_the_declared_profile_and_the_participant_has_the_core_roles():
    fake = FakeMaestro()
    thread_id = fake.create_thread(ANA, title=None)
    declared = User(user_id="user-1", user_name="Ana Declarada", user_email="declarada@example.com", identity_ids=["idp-declarada"])
    event = fake.agent_publish(thread_id, declared, [{"type": "text", "text": "Olá"}], client_event_id="client-1")
    view = fake.agent_view(thread_id)
    actor = view["events"][-1]["actor"]
    assert (actor["display_name"], actor["email"], actor["identity_ids"]) == ("Ana Declarada", "declarada@example.com", ["idp-declarada"])
    assert not any(role in actor for role in ROLES) and event["actor"].get("email") is None
    assert [_human(view["participants"], "user-1")[role] for role in ROLES] == ["user", "owner", "manager"]


async def test_added_person_is_described_when_the_core_knows_it_and_not_otherwise():
    async def handler(on, send, memory, analytics, settings):
        yield send.run_start()
        yield send.add_participant("user-2")
        yield send.add_participant("user-9")
        yield send.run_end()

    fake = FakeMaestro(core_people=[BIA])
    with fake.serving():
        thread_id, _, response = await _deliver(fake, handler)
    assert response.json()["state"] == "silent"
    stored = _human(fake.threads[thread_id]["participants"], "user-2")
    assert stored["display_name"] == WEB_CHAT_PERSON_NAME and "email" not in stored
    view = fake.agent_view(thread_id)
    known, unknown = _human(view["participants"], "user-2"), _human(view["participants"], "user-9")
    assert (known["display_name"], known["email"], known["identity_ids"]) == ("Bia Lima", "bia@example.com", ["idp-bia"])
    assert [known[role] for role in ROLES] == ["user", "member", "member"]
    assert unknown["display_name"] == WEB_CHAT_PERSON_NAME and not any(key in unknown for key in ("email", *ROLES))
    added = [event for event in view["events"] if event["type"] == "participant.added"]
    assert [event["payload"]["participant"].get("email") for event in added] == ["bia@example.com", None]


async def test_a_person_who_left_the_thread_is_not_described():
    fake = FakeMaestro(core_people=[BIA])
    thread_id = fake.create_thread(ANA, title=None)
    fake.threads[thread_id]["participants"].append(
        {
            "participant_id": str(uuid4()),
            "display_name": WEB_CHAT_PERSON_NAME,
            "state": "removed",
            "joined_sequence": "1",
            "left_sequence": "1",
            "kind": "human",
            "principal_id": "user-2",
            "role": "member",
        }
    )
    assert "system_role" not in _human(fake.agent_view(thread_id)["participants"], "user-2")


async def test_context_carries_the_queue_catalog_and_the_thread_view():
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen.update(queues=on.context.queues, allowed=on.context.maestro.allowed_queues, title=on.context.thread_title)
        yield send.run_start()
        yield send.update_thread(title="Novo título", queue="vendas")
        yield send.run_end()

    fake = FakeMaestro(queues=QUEUE_CATALOG)
    with fake.serving():
        thread_id, delivery, response = await _deliver(fake, handler)
    assert response.json()["state"] == "silent"
    assert (seen["queues"], seen["allowed"], seen["title"]) == (list(QUEUE_CATALOG), ("suporte", "vendas"), "Atendimento")
    view = fake.agent_view(thread_id)
    assert view["workspace"] == {"allowed_queues": ["suporte", "vendas"], "queues": [queue.model_dump() for queue in QUEUE_CATALOG]}
    assert (view["thread"]["title"], view["thread"]["queue"]) == ("Novo título", "vendas")
    # As of the context cutoff: the change the execution published is after it.
    cutoff = int(delivery.envelope["context_cutoff_sequence"])
    old = fake._thread_view(thread_id, cutoff)
    assert (old["title"], old["queue"], old["channel"], old["channel_meta"]) == ("Atendimento", None, "dooers-platform", None)
    assert shared_contracts().is_valid("Public.ContextThread", old)


async def test_without_the_connection_context_the_pages_are_those_of_an_older_maestro():
    seen = {}

    async def handler(on, send, memory, analytics, settings):
        seen.update(user=on.context.user, queues=on.context.queues, title=on.context.thread_title)
        yield send.text("ok")

    fake = FakeMaestro(queues=QUEUE_CATALOG, connection_context=False)
    with fake.serving():
        thread_id, _, response = await _deliver(fake, handler)
    assert response.json()["state"] == "responded"
    assert seen["queues"] == [] and seen["user"] == User(user_id="user-1")
    view = fake.agent_view(thread_id)
    assert "workspace" not in view and "thread" not in view
    assert not any(role in _human(view["participants"], "user-1") for role in ROLES)


# -- The public chat visitor's own thread (``public-chat.ts``) --


def test_visitor_thread_lists_participants_by_kind_like_maestro_and_a_guest_has_no_profile_from_the_core():
    guest = User(user_id="guest:abc123", system_role="guest", organization_role="guest", workspace_role="guest")
    fake = FakeMaestro()
    thread_id = fake.ensure_external_thread(
        guest, external_key="public:link:guest:guest:abc123", connection_type="guest", guest_profile={"name": "Carlos Lima"}
    )

    # ``rows`` come ``orderBy(participants.kind, participants.principalId)``: the agent, then the visitor.
    assert [participant["kind"] for participant in fake.threads[thread_id]["participants"]] == ["agent", "external"]
    visitor = next(p for p in fake.agent_view(thread_id)["participants"] if p["kind"] == "external")
    # ``agentParticipantView``: an empty core e-mail is null, the identity IDs are empty, the roles are ``guest``.
    assert (visitor["email"], visitor["identity_ids"]) == (None, [])
    assert (visitor["system_role"], visitor["organization_role"], visitor["workspace_role"]) == ("guest",) * 3
    assert "display_name" not in visitor
    assert visitor["guest_profile"] == {"name": "Carlos Lima"}
