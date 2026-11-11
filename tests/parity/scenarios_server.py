"""Scenarios for ``AgentServer`` methods, WhatsApp and public chat.

Scripts call only the public ``AgentServer`` API (plus the WhatsApp receipt entry
point) and return what the caller gets back.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from dooers.agents.server.features.channels.whatsapp.delivery_receipt import apply_whatsapp_delivery_receipt
from dooers.agents.server.protocol.models import User, WireS2C_TextPart
from tests.parity.model import (
    AGENT_ID,
    ASSISTANT_NAME,
    ORG_ID,
    USER,
    WHATSAPP_AGENT_PHONE,
    WHATSAPP_CHANNEL_META,
    WHATSAPP_CUSTOMER,
    WHATSAPP_INSTANCE,
    WHATSAPP_OUTBOUND_ID,
    WHATSAPP_SECRETS,
    WHATSAPP_USER,
    WORKSPACE_ID,
    Connect,
    Scenario,
    ScenarioInput,
    Turn,
    capture_incoming,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nparity-image"
PHOTO_URL = "https://cdn.example.com/foto.png"
WHATSAPP_CONFIG = {"dooers_whatsapp_service": True}
PUBLIC_CHAT_CHANNEL = {"channel": "dooers-public-chat", "channel_meta": {"public_chat_label": "Site institucional"}}


def _same_identity() -> dict[str, Any]:
    return {"user": USER, "organization_id": ORG_ID, "workspace_id": WORKSPACE_ID}


async def _echo(on, send, memory, analytics, settings):
    yield send.run_start()
    yield send.text(f"Eco: {on.message}")
    yield send.run_end()


async def _stream_view(stream: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "thread_id": stream.thread_id,
        "event_id": stream.event_id,
        "is_new_thread": stream.is_new_thread,
    }
    view["collected"] = [{"send_type": e.send_type, "data": e.data} for e in await stream.collect()]
    return view


async def _server_dispatch(server: Any, handler: Any) -> dict[str, Any]:
    first = await server.dispatch(handler, AGENT_ID, "Olá via dispatch", thread_title="Atendimento", **_same_identity())
    calls = [await _stream_view(first)]
    again = await server.dispatch(handler, AGENT_ID, "Continuação", thread_id=first.thread_id, **_same_identity())
    calls.append(await _stream_view(again))
    fixed = await server.dispatch(handler, AGENT_ID, "Thread com id fixo", thread_id="thread-fixa-1", **_same_identity())
    calls.append(await _stream_view(fixed))
    return {"dispatch": calls}


PEER_CHANNEL_META = {
    "whatsapp": {
        "instance_id": WHATSAPP_INSTANCE,
        "from_e164": WHATSAPP_CUSTOMER,
        "agent_phone_e164": WHATSAPP_AGENT_PHONE,
        "from_me": True,
        "self_chat": False,
        "wa_message_id": "wamid.peer.1",
    }
}


async def _server_ingest(server: Any, handler: Any) -> dict[str, Any]:
    common = {
        "organization_id": ORG_ID,
        "workspace_id": WORKSPACE_ID,
        "channel": "whatsapp",
        "channel_meta": copy.deepcopy(PEER_CHANNEL_META),
    }
    calls: list[dict[str, Any]] = []
    thread_id = await server.ingest(
        AGENT_ID, "Mensagem do cliente", user=WHATSAPP_USER, actor="user", data={"origem": "whatsapp_peer"}, **common
    )
    calls.append({"call": "actor_user_new_thread", "thread_id": thread_id})
    try:
        # Default actor ("assistant"), as the docstring describes for human outbound.
        returned = await server.ingest(AGENT_ID, "Resposta do atendente", thread_id=thread_id, author="Maria", **common)
        calls.append({"call": "default_actor", "thread_id": returned})
    except Exception as exc:  # production behavior is recorded, not judged
        calls.append({"call": "default_actor", "raised": type(exc).__name__})
    returned = await server.ingest(AGENT_ID, "Segunda mensagem", user=WHATSAPP_USER, thread_id=thread_id, actor="user", **common)
    calls.append({"call": "actor_user_existing_thread", "thread_id": returned})
    return {"ingest": calls}


async def _repository_crud(server: Any, handler: Any) -> dict[str, Any]:
    repo = await server.repository()
    thread = await repo.create_thread(AGENT_ID, ORG_ID, WORKSPACE_ID, user=USER, title="Via repositório", queue="suporte")
    fetched = await repo.get_thread(thread.id)
    updated = await repo.update_thread(thread.id, title="Título novo", queue="vendas")
    missing_update = await repo.update_thread("thread-inexistente", title="x")
    first = await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Oi")], user=USER, author="Ana Souza")
    second = await repo.create_event(
        thread.id,
        "message",
        "assistant",
        content=[WireS2C_TextPart(text="Olá, Ana")],
        data={"origem": "crm"},
        author=ASSISTANT_NAME,
    )
    draft = await repo.create_event(thread.id, "message", "assistant", content=[WireS2C_TextPart(text="Rascunho")])
    await repo.remove_event(draft.id)
    newest_first = await repo.list_events({"thread_id": thread.id})
    from_user = await repo.list_events({"thread_id": thread.id, "actor": "user"}, order={"direction": "asc"})
    fetched_event = await repo.get_event(first.id)
    try:
        await repo.list_events({})
        list_events_error = None
    except ValueError as exc:
        list_events_error = str(exc)
    temporary = await repo.create_thread(AGENT_ID, ORG_ID, WORKSPACE_ID, title="Temporária")
    await repo.remove_thread(temporary.id)
    removed = await repo.get_thread(temporary.id)
    listed = await repo.list_threads(
        {"agent_id": AGENT_ID, "organization_id": ORG_ID, "workspace_id": WORKSPACE_ID, "user_id": USER.user_id}
    )
    runs = await repo.list_runs({"thread_id": thread.id})
    missing_run = await repo.get_run("run-inexistente")
    await repo.update_settings(AGENT_ID, {"tom": "formal"})
    settings = await repo.get_settings(AGENT_ID)
    return {
        "repository": {
            "create_thread": thread.model_dump(),
            "get_thread": fetched.model_dump() if fetched else None,
            "update_thread": updated.model_dump() if updated else None,
            "update_missing_thread": missing_update,
            "create_event": [first.model_dump(), second.model_dump()],
            "list_events_default": [e.model_dump() for e in newest_first],
            "list_events_user_asc": [e.model_dump() for e in from_user],
            "get_event": fetched_event.model_dump() if fetched_event else None,
            "list_events_without_thread": list_events_error,
            "get_removed_thread": removed,
            "list_threads": [t.model_dump() for t in listed],
            "list_runs": [r.model_dump() for r in runs],
            "get_missing_run": missing_run,
            "get_settings": settings,
        }
    }


async def _photo_reply(on, send, memory, analytics, settings):
    yield send.run_start()
    yield send.text("Recebi a foto.")
    yield send.image("https://cdn.example.com/resposta.png", mime_type="image/png", alt="Resposta")
    yield send.run_end()


PHOTO_CONTENT = [
    {"type": "text", "text": "Veja a foto"},
    {"type": "image", "url": PHOTO_URL, "mime_type": "image/png", "filename": "foto.png"},
]


async def _memory_by_thread_id(server: Any, handler: Any) -> dict[str, Any]:
    stream = await server.dispatch(handler, AGENT_ID, "", content=copy.deepcopy(PHOTO_CONTENT), **_same_identity())
    await stream.collect()
    memory = await server.memory(stream.thread_id)
    thread = await memory.get_thread()
    raw = await memory.get_history_raw(limit=10)
    history = await memory.get_history(limit=10)
    artifacts, cursor, has_more = await memory.list_artifacts()
    outgoing, _, _ = await memory.list_artifacts(direction="out")
    missing = await server.memory("thread-inexistente")
    return {
        "memory": {
            "get_thread": thread.model_dump() if thread else None,
            "get_history_raw": [e.model_dump() for e in raw],
            "get_history": history,
            "list_artifacts": [a.model_dump() for a in artifacts],
            "list_artifacts_cursor": cursor,
            "list_artifacts_has_more": has_more,
            "list_artifacts_out": [a.model_dump() for a in outgoing],
            "missing_get_thread": await missing.get_thread(),
            "missing_get_history": await missing.get_history(),
            "missing_list_artifacts": list(await missing.list_artifacts()),
        }
    }


async def _upload_reader(on, send, memory, analytics, settings):
    capture_incoming(on, ("message", "content"))
    yield send.run_start()
    yield send.text("Arquivo recebido.")
    yield send.run_end()


async def _chat_upload(server: Any, handler: Any) -> dict[str, Any]:
    base = {"data": PNG_BYTES, "filename": "foto.png", "mime_type": "image/png", "agent_id": AGENT_ID}
    upload = await server.chat_upload(**base)
    errors: dict[str, str | None] = {}
    for label, change in (("empty_file", {"data": b""}), ("blank_agent_id", {"agent_id": "  "})):
        try:
            await server.chat_upload(**{**base, **change})
            errors[label] = None
        except ValueError as exc:
            errors[label] = str(exc)
    content = [
        {"type": "text", "text": "Segue a foto"},
        {"type": "image", "ref_id": upload["ref_id"], "filename": "foto.png", "mime_type": "image/png"},
    ]
    stream = await server.dispatch(handler, AGENT_ID, "", content=content, **_same_identity())
    await stream.collect()
    return {"chat_upload": upload, "chat_upload_errors": errors}


async def _whatsapp_text(on, send, memory, analytics, settings):
    yield send.run_start()
    yield send.whatsapp.text("Seu pedido saiu para entrega.", to_e164=WHATSAPP_CUSTOMER, instance_id=WHATSAPP_INSTANCE)
    yield send.run_end()


async def _whatsapp_receipt(server: Any, handler: Any) -> dict[str, Any]:
    stream = await server.dispatch(
        handler,
        AGENT_ID,
        "Oi, quero saber do meu pedido",
        user=WHATSAPP_USER,
        organization_id=ORG_ID,
        workspace_id=WORKSPACE_ID,
        channel="whatsapp",
        channel_meta=copy.deepcopy(WHATSAPP_CHANNEL_META),
    )
    await stream.collect()
    memory = await server.memory(stream.thread_id)
    steps = (
        (WHATSAPP_OUTBOUND_ID, "delivered", {}),
        (WHATSAPP_OUTBOUND_ID, "read", {}),
        (WHATSAPP_OUTBOUND_ID, "delivered", {}),
        ("wamid.desconhecido", "read", {}),
        (WHATSAPP_OUTBOUND_ID, "failed", {"error_code": 131047, "error_message": "Re-engagement message"}),
    )
    receipts = []
    for wa_message_id, status, extra in steps:
        body = json.dumps({"delivery_receipt": True, "wa_message_id": wa_message_id, "status": status, **extra})
        applied = await apply_whatsapp_delivery_receipt(server.persistence, AGENT_ID, body.encode())
        replies = await memory.get_history_raw(filters={"actor": "assistant"})
        receipts.append(
            {
                "wa_message_id": wa_message_id,
                "status": status,
                "applied": applied,
                "delivery": [(e.data or {}).get("delivery") for e in replies],
            }
        )
    return {"receipts": receipts}


async def _public_chat_reply(on, send, memory, analytics, settings):
    yield send.run_start()
    yield send.text("Olá! Recebi sua mensagem.")
    yield send.run_end()


GUEST_CONNECT = Connect(
    connection_type="guest",
    core_user=User(user_id="guest:abc123"),
    frame_user=User(user_id="", user_name="Carlos Lima", user_email="carlos@example.com"),
)
PUBLIC_USER = User(user_id="user-42", user_name="Paula Reis", user_email="paula@example.com", identity_ids=["idp-paula"])
PUBLIC_AUTHENTICATED_CONNECT = Connect(
    connection_type="public_authenticated",
    core_user=PUBLIC_USER,
    frame_user=User(user_id="", user_name="Paula Reis", user_email="paula@example.com"),
)

SERVER_SCENARIOS: dict[str, Scenario] = {
    "server_dispatch": Scenario(handler=_echo, incoming=ScenarioInput(flow="script", script=_server_dispatch)),
    "server_ingest": Scenario(handler=_echo, incoming=ScenarioInput(flow="script", script=_server_ingest)),
    "repository_crud": Scenario(handler=_echo, incoming=ScenarioInput(flow="script", script=_repository_crud)),
    "memory_by_thread_id": Scenario(handler=_photo_reply, incoming=ScenarioInput(flow="script", script=_memory_by_thread_id)),
    "chat_upload": Scenario(handler=_upload_reader, incoming=ScenarioInput(flow="script", script=_chat_upload)),
    "whatsapp_text": Scenario(
        handler=_whatsapp_text,
        incoming=ScenarioInput(
            turns=(Turn(message="Oi, quero saber do meu pedido"),),
            user=WHATSAPP_USER,
            channel="whatsapp",
            channel_meta=WHATSAPP_CHANNEL_META,
            config=WHATSAPP_CONFIG,
            service_secrets=WHATSAPP_SECRETS,
        ),
    ),
    "whatsapp_receipt": Scenario(
        handler=_echo,
        incoming=ScenarioInput(flow="script", script=_whatsapp_receipt, config=WHATSAPP_CONFIG, service_secrets=WHATSAPP_SECRETS),
    ),
    "public_chat_guest": Scenario(
        handler=_public_chat_reply,
        incoming=ScenarioInput(
            flow="websocket",
            connect=GUEST_CONNECT,
            turns=(
                Turn(
                    message="Olá, quero um orçamento",
                    metadata={
                        "guest_name": "Carlos Lima",
                        "guest_email": "carlos@example.com",
                        "guest_phone": "+5511977776666",
                        **PUBLIC_CHAT_CHANNEL,
                    },
                ),
                Turn(message="Pode me ligar?", metadata=PUBLIC_CHAT_CHANNEL),
            ),
            observe=frozenset({"threads"}),
        ),
    ),
    "public_chat_authenticated": Scenario(
        handler=_public_chat_reply,
        incoming=ScenarioInput(
            flow="websocket",
            connect=PUBLIC_AUTHENTICATED_CONNECT,
            turns=(
                Turn(message="Oi, sou cliente", metadata=PUBLIC_CHAT_CHANNEL),
                Turn(message="Obrigada", metadata=PUBLIC_CHAT_CHANNEL),
            ),
            observe=frozenset({"threads"}),
        ),
    ),
}
