"""Contexto aditivo e escopo privado por instância destinatária."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from weakref import WeakKeyDictionary

from dooers.agents.server.protocol.models import (
    AudioPart,
    ContactPart,
    DocumentPart,
    ImagePart,
    TextPart,
    User,
    WorkspaceQueueCatalog,
)
from dooers.tools.whatsapp.runtime import (
    bind_handler_context,
    bind_handler_scope_guard,
    reset_handler_context,
    reset_handler_scope_guard,
)

from .client import MaestroContextError
from .diagnostics import diagnostic_stage
from .memory import MaestroMemory

if TYPE_CHECKING:
    from dooers.agents.server.handlers.incoming import AgentIncoming


@dataclass(frozen=True)
class MaestroPrivateScope:
    organization_id: str
    workspace_id: str | None
    instance_agent_id: str


@dataclass(frozen=True)
class MaestroPrivateResources:
    """Port confiável: settings/analytics/segredos/banco pertencem a scope.

    O operador deve resolver stores por esta identidade lógica, nunca por sender,
    runtime_id ou infraestrutura compartilhada. O SDK não torna um store arbitrário
    seguro só por rotulá-lo. Não é persistência de histórico Maestro.
    """

    scope: MaestroPrivateScope
    persistence: Any = field(repr=False)
    settings: Any = field(repr=False)
    analytics: Any = field(repr=False)
    #: Workspace queue catalog for ``on.context.queues``, as the legacy path gets it from Core
    #: (``WorkspaceQueueCatalog`` items). Core has no route an agent can call for it, so the
    #: operator supplies it; empty by default.
    queues: Sequence[WorkspaceQueueCatalog] = field(default=(), repr=False)


class MaestroPrivateResolver(Protocol):
    async def __call__(self, scope: MaestroPrivateScope) -> MaestroPrivateResources: ...


class MaestroCancellation:
    """Sinal cooperativo local; cancelamento/fence canônico permanece no Maestro."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True)
class MaestroExecutionContext:
    delivery_id: str
    phase_id: str
    execution_id: str
    attempt_number: int
    phase_retry_number: int
    lease_fence: str
    recipient_participant_id: str
    instance_agent_id: str
    runtime_id: str
    trigger_event_id: str
    trigger_sequence: str
    context_cutoff_sequence: str
    origin_event_id: str
    origin_sequence: str
    actor: dict[str, Any]
    causation_event_id: str | None
    root_event_id: str
    causal_depth: int
    delivery_expires_at: str
    deadline_at: str
    resume: dict[str, Any] | None = field(repr=False)
    cancellation: MaestroCancellation = field(repr=False)
    checkpoint: dict[str, Any] | None = field(default=None, repr=False)
    tool_approval: dict[str, Any] | None = field(default=None, repr=False)
    mode: Literal["maestro"] = "maestro"
    # Estes limites não existem no wire atual; não inventar orçamento restante.
    causal_budget_remaining: int | None = None

    @property
    def original_trigger_event_id(self) -> str:
        return self.trigger_event_id


@dataclass(frozen=True)
class MaestroHandlerInput:
    on: AgentIncoming = field(repr=False)
    memory: MaestroMemory
    analytics: Any = field(repr=False)
    settings: Any = field(repr=False)


_active: WeakKeyDictionary[asyncio.Task, MaestroExecutionContext] = WeakKeyDictionary()


def current_maestro_context() -> MaestroExecutionContext | None:
    task = asyncio.current_task()
    return _active.get(task) if task is not None else None


#: ``on.context.channel`` of a thread without a channel (web chat), as in production.
DEFAULT_CHANNEL = "dooers-platform"


def _production_message(content: list[Any]) -> str:
    """``HandlerPipeline._extract_message`` of production: texts and contact cards, else media markers."""
    texts = []
    for part in content:
        if isinstance(part, TextPart):
            texts.append(part.text)
        elif isinstance(part, ContactPart):
            line = part.display_name.strip() or "Contact"
            texts.append(f"{line} [contact card]")
    joined = " ".join(t for t in texts if t and str(t).strip()).strip()
    if joined:
        return joined
    fallback: list[str] = []
    for part in content:
        if isinstance(part, ImagePart):
            fallback.append("[image]")
        elif isinstance(part, AudioPart):
            fallback.append("[audio]")
        elif isinstance(part, DocumentPart):
            fallback.append(f"[document: {part.filename or 'file'}]")
    return " ".join(fallback)


def _thread_at_cutoff(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Title, creation time, queue and channel of the thread up to the context cutoff.

    ``thread.created`` carries the thread view; each later ``thread.updated`` carries the
    current title and queue. A thread without ``thread.created`` (one an agent created, with
    no owner) keeps the production defaults.
    """
    thread: dict[str, Any] = {"title": None, "created_at": None, "queue": None, "channel": None, "channel_meta": None}
    for event in events:
        if event["type"] == "thread.created":
            view = event["payload"]["thread"]
            thread.update(
                title=view["title"] or None,
                created_at=datetime.fromisoformat(view["created_at"].replace("Z", "+00:00")),
                queue=view.get("queue"),
                channel=view.get("channel"),
                channel_meta=deepcopy(view.get("channel_meta")),
            )
        elif event["type"] == "thread.updated":
            thread["title"] = event["payload"]["title"] or None
            if "queue" in event["payload"]:
                thread["queue"] = event["payload"]["queue"]
    return thread


def _user(actor: dict[str, Any], participants: list[dict[str, Any]]) -> User:
    """The production ``User`` of the author: a person, a channel contact or ``User(user_id="")``.

    E-mail and identity IDs come from the delivery context (actor, else its participant).
    They exist only in the agent's context, never in what browsers receive.
    """
    if actor["kind"] == "human":
        participant = next((p for p in participants if p["participant_id"] == actor["participant_id"]), {})
        return User(
            user_id=actor["principal_id"],
            user_name=actor["display_name"],
            user_email=actor["email"] if "email" in actor else participant.get("email"),
            identity_ids=list(actor["identity_ids"] if "identity_ids" in actor else participant.get("identity_ids", [])),
        )
    if actor["kind"] == "external":
        # As production does with a WhatsApp contact: the user id is the contact (e.g. +55...).
        return User(user_id=actor["external_id"], user_name=actor.get("display_name"))
    return User(user_id="")


def _url_part(part: dict[str, Any]) -> AudioPart | ImagePart | DocumentPart:
    """``media_link``: the production URL-only part of the same kind (no bytes)."""
    url, mime_type = part["url"], part.get("mime_type")
    if part["kind"] == "audio":
        return AudioPart(
            data=b"",
            mime_type=mime_type or "application/octet-stream",
            duration=part.get("duration"),
            filename=part.get("filename"),
            url=url,
        )
    if part["kind"] == "image":
        return ImagePart(data=b"", mime_type=mime_type or "image/*", filename=part.get("filename"), url=url)
    return DocumentPart(
        data=b"", mime_type=mime_type or "application/octet-stream", filename=part.get("filename") or "file", size_bytes=0, url=url
    )


def _form_response(origin: dict[str, Any], snapshot: dict[str, Any], envelope: dict[str, Any]) -> tuple[dict, bool, str]:
    """``form_data``, ``form_cancelled`` and ``form_event_id`` of a ``form.responded`` trigger."""
    payload = origin["payload"]
    forms = [
        event
        for event in snapshot["events"]
        if event["event_id"] == payload["form_event_id"]
        and event["type"] == "form.created"
        and event["actor"]["kind"] == "agent"
        and event["actor"]["instance_agent_id"] == envelope["instance_agent_id"]
    ]
    if len(forms) != 1:
        raise MaestroContextError("CONTEXT_INVALID")
    return deepcopy(payload["values"]), payload["cancelled"], payload["form_event_id"]


async def _incoming(
    memory: MaestroMemory, snapshot: dict[str, Any], cancellation: MaestroCancellation, private_resume=None, checkpoint_state=None
) -> AgentIncoming:
    from dooers.agents.server.handlers.context import AgentContext
    from dooers.agents.server.handlers.incoming import AgentIncoming

    envelope = memory._envelope
    trigger = snapshot["trigger_event"]
    origin = snapshot.get("invocation_event") or trigger
    resume = envelope.get("resume")
    form_data, form_cancelled, form_event_id = None, False, None
    if resume:
        candidates = [event for event in snapshot["events"] if event["event_id"] == resume["response_event_id"]]
        if len(candidates) != 1:
            raise MaestroContextError("CONTEXT_INVALID")
        origin = candidates[0]
        payload = origin["payload"]
        if (
            origin["type"] != "interaction.responded"
            or origin["sequence"] != resume["response_sequence"]
            or origin["actor"]["kind"] != "human"
            or payload["interaction_id"] != resume["interaction_id"]
            or payload["delivery_id"] != envelope["delivery_id"]
            or payload["phase_id"] != envelope["phase_id"]
            or payload["action"] != ("submit" if resume["kind"] == "form" else "approve")
        ):
            raise MaestroContextError("CONTEXT_INVALID")
        form_data = deepcopy(payload["shared_data"]) if resume["kind"] == "form" else None
        if private_resume is not None:
            from .interactions import validate_private_context

            private_resume = validate_private_context(private_resume, envelope)
            if resume["kind"] == "form":
                form_data = deepcopy(private_resume["response_data"])
                form_event_id = private_resume["form_event_id"]
        # O wire tem interaction_id, não ID legado do evento form original.
        # Não preencher form_event_id com um ID de entidade de outro tipo.
    elif origin["type"] == "form.responded":
        # Production form (send.form): the response is a new delivery to the form's author.
        # A cancel invokes the handler too, as production does.
        form_data, form_cancelled, form_event_id = _form_response(origin, snapshot, envelope)
    actor = origin["actor"]
    metadata = MaestroExecutionContext(
        **{
            key: envelope[key]
            for key in (
                "delivery_id",
                "phase_id",
                "execution_id",
                "attempt_number",
                "phase_retry_number",
                "lease_fence",
                "recipient_participant_id",
                "instance_agent_id",
                "runtime_id",
                "trigger_event_id",
                "trigger_sequence",
                "context_cutoff_sequence",
                "root_event_id",
                "causal_depth",
                "delivery_expires_at",
                "deadline_at",
            )
        },
        origin_event_id=origin["event_id"],
        origin_sequence=origin["sequence"],
        actor=deepcopy(actor),
        causation_event_id=origin["causation_event_id"],
        resume=deepcopy(resume),
        cancellation=cancellation,
        checkpoint=deepcopy(checkpoint_state),
        tool_approval=(
            {
                "interaction_id": resume["interaction_id"],
                "decision_event_id": resume["response_event_id"],
                "arguments_hash": private_resume["response_data"]["arguments_hash"],
                "state": "approved",
            }
            if resume and resume["kind"] == "tool_approval" and private_resume
            else None
        ),
    )
    thread = _thread_at_cutoff(snapshot["events"])
    channel = thread["channel"] or DEFAULT_CHANNEL
    if actor["kind"] == "external" and channel == DEFAULT_CHANNEL:
        # A channel contact is never a platform user: no RAG on_behalf (production: channel "whatsapp").
        channel = actor["channel"]
    context = AgentContext(
        thread_id=envelope["thread_id"],
        agent_id=envelope["instance_agent_id"],
        event_id=origin["event_id"],
        organization_id=envelope["organization_id"],
        workspace_id=envelope["workspace_id"] or "",
        channel=channel,
        channel_meta=thread["channel_meta"],
        user=_user(actor, snapshot["participants"]),
        thread_title=thread["title"],
        thread_created_at=thread["created_at"],
        queue=thread["queue"],
    )
    # Set before AgentIncoming is built: a Maestro context binds RAG only in maestro_handler_context.
    context.maestro = metadata
    if resume:
        parts = []
    elif origin["type"] == "form.responded":
        # Production builds the handler content of a form.response from its empty message.
        parts = [{"type": "text", "text": ""}]
    else:
        parts = origin["payload"]["content"]
    from .artifacts import MaestroArtifactsClient
    from .memory import MaestroArtifactReference

    hydrated = []
    total = 0
    for part in parts:
        if part["type"] == "text":
            total += len(part["text"].encode("utf-8"))
            hydrated.append(TextPart(text=part["text"]))
        elif part["type"] == "attachment":
            ref = MaestroArtifactReference(
                part["artifact_id"],
                origin["event_id"],
                origin["sequence"],
                deepcopy(actor),
                part.get("filename"),
                part.get("mime_type"),
                part.get("size_bytes"),
            )
            artifact = await MaestroArtifactsClient(memory._client).hydrate(
                ref, maximum_bytes=min(memory._client.config.max_artifact_bytes, memory._client.config.max_hydrated_bytes - total)
            )
            total += len(artifact.data)
            hydrated.append(artifact.content_part(await memory._links.url_for(part["artifact_id"]), duration=part.get("duration")))
        elif part["type"] == "media_link":
            hydrated.append(_url_part(part))
        elif part["type"] == "contact":
            hydrated.append(
                ContactPart(display_name=part["display_name"].strip(), vcard=part.get("vcard"), phones=deepcopy(part.get("phones", [])))
            )
        else:
            raise MaestroContextError("UNSUPPORTED_CAPABILITY")
        if total > memory._client.config.max_hydrated_bytes:
            hydrated.clear()
            raise MaestroContextError("CONTEXT_LIMIT")
    if sum(len(part["text"].encode("utf-8")) for part in parts if part["type"] == "text") > memory._client.config.max_content_bytes:
        raise MaestroContextError("CONTEXT_LIMIT")
    return AgentIncoming(
        _production_message(hydrated),
        hydrated,
        context,
        form_data=form_data,
        form_cancelled=form_cancelled,
        form_event_id=form_event_id,
    )


@asynccontextmanager
async def maestro_handler_context(
    memory: MaestroMemory,
    private_resolver: MaestroPrivateResolver,
    *,
    cancellation: MaestroCancellation | None = None,
    private_resume=None,
    checkpoint_state=None,
    options_resolver=None,
) -> AsyncIterator[MaestroHandlerInput]:
    """Prepara on/memory/analytics/settings; E05-08 fornece send e roda handler.

    A assinatura handler(on, send, memory, analytics, settings) continua intacta.
    Use async with dentro da task que efetivamente consome o generator. Nenhum
    contexto RAG/Maestro é herdado automaticamente por tasks filhas.
    """
    task = asyncio.current_task()
    if task is None:
        raise MaestroContextError()
    cancellation = cancellation or MaestroCancellation()
    try:
        if cancellation.cancelled:
            raise MaestroContextError("CONTEXT_EXPIRED")
        memory._client.bind_cancellation(lambda: cancellation.cancelled)
        with diagnostic_stage("context.snapshot", memory._envelope):
            snapshot = await memory.snapshot()
        from .recipient_options import TurnSettings, prepare_recipient_options

        selected_options, assert_options_current = prepare_recipient_options(
            snapshot.get("invocation_event") or snapshot["trigger_event"], memory._envelope, options_resolver
        )
        incoming = await _incoming(memory, snapshot, cancellation, private_resume, checkpoint_state)
        assert_options_current()
        if selected_options:
            from dooers.agents.server.protocol.models import ChatContext

            incoming.context.chat_context = ChatContext(**selected_options)
        envelope = memory._envelope
        scope = MaestroPrivateScope(envelope["organization_id"], envelope["workspace_id"], envelope["instance_agent_id"])
        try:
            async with asyncio.timeout(memory._client._check_current()):
                with diagnostic_stage("context.private", memory._envelope):
                    resources = await private_resolver(scope)
        except Exception:
            raise MaestroContextError() from None
        assert_options_current()
        if not isinstance(resources, MaestroPrivateResources) or resources.scope != scope:
            raise MaestroContextError("ACCESS_REVOKED")
        if isinstance(resources.queues, (str, bytes)) or not all(isinstance(q, WorkspaceQueueCatalog) for q in resources.queues):
            raise MaestroContextError("CONTEXT_INVALID")
        # A copy per execution, as production copies the handshake catalog (``list(context.queues)``).
        incoming.context.queues = [queue.model_copy(deep=True) for queue in resources.queues]
        # Recursos privados podem exigir I/O; revalidar acesso remoto antes de expor input.
        with diagnostic_stage("context.revalidate", memory._envelope):
            await memory.get_thread()
        if private_resume is not None:
            from .interactions import private_context_bytes

            if private_context_bytes(await memory._client.private_resume_context()) != private_context_bytes(private_resume):
                raise MaestroContextError("ACCESS_REVOKED")
        memory._client._check_current()
        assert_options_current()
        previous = _active.get(task)
        tokens = None
        guard_token = None
        rag_token = None
        active = True
        try:
            assert_options_current()
            rag_token = incoming.bind_runtime_context()
            tokens = bind_handler_context(persistence=resources.persistence, agent_id=scope.instance_agent_id)
            guard_token = bind_handler_scope_guard(lambda: active and asyncio.current_task() is task and not cancellation.cancelled)
            _active[task] = incoming.context.maestro
            settings = TurnSettings(resources.settings, selected_options) if selected_options else resources.settings
            yield MaestroHandlerInput(incoming, memory, resources.analytics, settings)
        finally:
            active = False
            try:
                incoming.reset_runtime_context(rag_token)
            finally:
                if guard_token is not None:
                    reset_handler_scope_guard(guard_token)
                if tokens is not None:
                    reset_handler_context(tokens)
                if previous is None:
                    _active.pop(task, None)
                else:
                    _active[task] = previous
    finally:
        cancellation.cancel()
        memory._client.close()
