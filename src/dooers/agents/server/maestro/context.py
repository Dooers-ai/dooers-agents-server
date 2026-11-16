"""Contexto aditivo e escopo privado por instância destinatária."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Mapping, Sequence
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
    WorkspaceQueueMember,
)
from dooers.tools.whatsapp.runtime import (
    bind_handler_context,
    bind_handler_scope_guard,
    reset_handler_context,
    reset_handler_scope_guard,
)

from .client import MaestroContextError
from .diagnostics import diagnostic_stage
from .dispatch_registry import MARKER_KEY
from .legacy_model import production_user
from .memory import MaestroMemory
from .window import MaestroContextWindow

if TYPE_CHECKING:
    from dooers.agents.server.handlers.incoming import AgentIncoming

    from .persistence import MaestroAgentMemory


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
    #: Deprecated. The workspace queue catalog now comes from the delivery context (``workspace``: the core's
    #: catalog, as the legacy path gets it from Core). This field is read only when the Maestro sends no
    #: ``workspace`` (an older Maestro): ``WorkspaceQueueCatalog`` items; empty by default.
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
    actor: dict[str, Any] = field(repr=False)  # carries the person's e-mail and identity ids
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
    #: Slugs of the workspace queue catalog (production ``allowed_queues``); empty: no catalog.
    allowed_queues: tuple[str, ...] = ()
    #: ``client_event_id`` of the trigger event (the SDK event id of an agent publication: ``DispatchStream.event_id``), if it has one.
    trigger_client_event_id: str | None = None
    #: Fingerprint of the handler a ``dispatch`` call used, when the trigger event carries one (``dispatch_registry``).
    dispatch_handler: str | None = None

    @property
    def original_trigger_event_id(self) -> str:
        return self.trigger_event_id


@dataclass(frozen=True)
class MaestroHandlerInput:
    on: AgentIncoming = field(repr=False)
    #: The production ``AgentMemory`` over the delivery context (``MaestroMemory`` stays internal).
    memory: MaestroAgentMemory
    analytics: Any = field(repr=False)
    settings: Any = field(repr=False)
    #: The production content-policy denial text for this message (``AgentConfig.allowed_content_types``): the
    #: executor answers with it and does not run the handler. ``None``: no policy, or an allowed message.
    content_policy_denial: str | None = field(default=None, repr=False)


_active: WeakKeyDictionary[asyncio.Task, MaestroExecutionContext] = WeakKeyDictionary()


def current_maestro_context() -> MaestroExecutionContext | None:
    task = asyncio.current_task()
    return _active.get(task) if task is not None else None


#: ``on.context.channel`` of a thread without a channel (web chat), as in production.
DEFAULT_CHANNEL = "dooers-platform"

#: ``actor["channel"]`` (the wire's own short name) to production's ``Thread.channel``: a public-chat
#: visitor's wire channel is ``"public_chat"``, but production's thread channel is
#: ``"dooers-public-chat"`` (plan 6 Task 5 brief, correction 1) — WhatsApp's wire name already is
#: production's own, so it maps to itself.
_EXTERNAL_CHANNEL = {"whatsapp": "whatsapp", "public_chat": "dooers-public-chat"}


def _production_message(content: list[Any]) -> str:
    """``on.message`` by production's own rule, ``HandlerPipeline._extract_message`` (it does not use ``self``)."""
    from dooers.agents.server.handlers.pipeline import HandlerPipeline

    return HandlerPipeline._extract_message(None, content)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def thread_from_view(view: dict[str, Any]) -> dict[str, Any]:
    """Title, creation time, queue and channel from the ``thread`` view of the delivery context.

    The view is the thread as it was at the context cutoff (``Public.ContextThread``): production
    reads the same values from the thread row. An empty title is ``None``.
    """
    return {
        "title": view["title"] or None,
        "created_at": _timestamp(view["created_at"]),
        "queue": view["queue"],
        "channel": view["channel"],
        "channel_meta": deepcopy(view["channel_meta"]),
        # The key the caller chose for the thread (``Public.ContextThread.external_key``); ``None`` when there is
        # none and for a public chat thread (its key is the Maestro's own). An older Maestro sends none.
        "external_key": view.get("external_key"),
    }


def caller_thread_id(view: Mapping[str, Any] | None, maestro_thread_id: str) -> str:
    """The thread id a handler sees: the caller's key when the thread has one, else the Maestro id.

    Production shows the handler the id the caller chose (``wa:inst:...``, the ``thread_id`` of ``dispatch``).
    Every call back to the Maestro uses ``maestro_thread_id``; only what the handler reads shows the key.
    """
    return (view or {}).get("external_key") or maestro_thread_id


def thread_at_cutoff(events: list[dict[str, Any]], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Title, creation time, queue and channel of the thread up to the context cutoff.

    ``thread.created`` carries the thread view; each later ``thread.updated`` carries the
    current title and queue. A thread without ``thread.created`` (one an agent created, with
    no owner) keeps the production defaults. ``base`` is the thread already known (``thread_from_view``):
    the events then only add the changes that come after it.
    """
    thread: dict[str, Any] = (
        deepcopy(base) if base is not None else {"title": None, "created_at": None, "queue": None, "channel": None, "channel_meta": None}
    )
    for event in events:
        if event["type"] == "thread.created":
            view = event["payload"]["thread"]
            thread.update(
                title=view["title"] or None,
                created_at=_timestamp(view["created_at"]),
                queue=view.get("queue"),
                channel=view.get("channel"),
                channel_meta=deepcopy(view.get("channel_meta")),
            )
        elif event["type"] == "thread.updated":
            thread["title"] = event["payload"]["title"] or None
            if "queue" in event["payload"]:
                thread["queue"] = event["payload"]["queue"]
    return thread


def workspace_queues(workspace: dict[str, Any]) -> tuple[list[WorkspaceQueueCatalog], tuple[str, ...]]:
    """The queue catalog and the allowed slugs of the ``workspace`` of the delivery context (copies)."""
    queues = [
        WorkspaceQueueCatalog(
            slug=queue["slug"],
            name=queue["name"],
            members=[
                WorkspaceQueueMember(
                    user_id=member["user_id"],
                    user_name=member["user_name"],
                    user_email=member["user_email"],
                    identity_ids=list(member["identity_ids"]),
                    claims=deepcopy(member["claims"]),
                )
                for member in queue["members"]
            ],
        )
        for queue in workspace["queues"]
    ]
    return queues, tuple(workspace["allowed_queues"])


def _user(actor: dict[str, Any], participants: list[dict[str, Any]]) -> User:
    """The production ``User`` of the author: a person, a channel contact or ``User(user_id="")``.

    A person is what production builds from Core's connection context (``auth_validation``): name,
    e-mail, identity IDs and the three roles. They come from the actor, else from its participant
    (``production_user``); a person the core did not describe keeps the ``User`` defaults.
    They exist only in the agent's context, never in what browsers receive.
    """
    if actor["kind"] == "human":
        participant = next((p for p in participants if p["participant_id"] == actor["participant_id"]), None)
        return production_user(actor, participant)
    if actor["kind"] == "external":
        # A WhatsApp contact (production: the number) or a public-chat visitor: the user id is the
        # contact. A public-chat visitor's participant carries the core's merge fields (Task 3b/4b) —
        # legacy_model._external_user does the same core-wins, guest_profile-falls-back merge humans get.
        participant = next((p for p in participants if p.get("participant_id") == actor.get("participant_id")), None)
        return production_user(actor, participant)
    return User(user_id="")


def _url_part(part: dict[str, Any]) -> AudioPart | ImagePart | DocumentPart:
    """``media_link``: the production URL-only part of the same kind (no bytes), with the metadata the agent wrote."""
    url, mime_type = part.get("url"), part.get("mime_type")
    if part["kind"] == "audio":
        return AudioPart(
            data=b"",
            mime_type=mime_type or "application/octet-stream",
            duration=part.get("duration"),
            filename=part.get("filename"),
            url=url,
        )
    if part["kind"] == "image":
        return ImagePart(
            data=b"",
            mime_type=mime_type or "image/*",
            width=part.get("width"),
            height=part.get("height"),
            filename=part.get("filename"),
            url=url,
        )
    return DocumentPart(
        data=b"",
        mime_type=mime_type or "application/octet-stream",
        filename=part.get("filename") or "file",
        size_bytes=part.get("size_bytes") or 0,
        url=url,
    )


async def _form_response(origin: dict[str, Any], window: MaestroContextWindow, envelope: dict[str, Any]) -> tuple[dict, bool, str]:
    """``form_data``, ``form_cancelled`` and ``form_event_id`` of a ``form.responded`` trigger."""
    payload = origin["payload"]
    form = await window.find_event(payload["form_event_id"])
    if (
        form is None
        or form["type"] != "form.created"
        or form["actor"]["kind"] != "agent"
        or form["actor"]["instance_agent_id"] != envelope["instance_agent_id"]
    ):
        raise MaestroContextError("CONTEXT_INVALID")
    return deepcopy(payload["values"]), payload["cancelled"], payload["form_event_id"]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def _incoming(
    memory: MaestroMemory, snapshot: dict[str, Any], cancellation: MaestroCancellation, private_resume=None, checkpoint_state=None
) -> AgentIncoming:
    """``snapshot`` is the context head (``MaestroContextWindow.load``); the window gives the older events it needs."""
    window = memory.window()
    from dooers.agents.server.handlers.context import AgentContext
    from dooers.agents.server.handlers.incoming import AgentIncoming

    envelope = memory._envelope
    trigger = snapshot["trigger_event"]
    origin = snapshot.get("invocation_event") or trigger
    resume = envelope.get("resume")
    form_data, form_cancelled, form_event_id = None, False, None
    if resume:
        origin = await window.find_event(resume["response_event_id"])
        if origin is None:
            raise MaestroContextError("CONTEXT_INVALID")
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
        form_data, form_cancelled, form_event_id = await _form_response(origin, window, envelope)
    actor = origin["actor"]
    # The core's queue catalog of the workspace, as production gets it in the handshake; an older Maestro sends none.
    workspace_catalog, allowed_queues = workspace_queues(snapshot["workspace"]) if "workspace" in snapshot else ([], ())
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
        allowed_queues=allowed_queues,
        trigger_client_event_id=_text(trigger["payload"].get("client_event_id")),
        dispatch_handler=_text((trigger["payload"].get("data") or {}).get(MARKER_KEY)),
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
    # The thread view of the delivery context (production reads the thread row); an older Maestro sends none:
    # then ``thread.created`` and the changes of the window give it.
    thread = thread_from_view(snapshot["thread"]) if "thread" in snapshot else thread_at_cutoff(await window.thread_events())
    channel = thread["channel"] or DEFAULT_CHANNEL
    if actor["kind"] == "external" and channel == DEFAULT_CHANNEL:
        # A channel contact is never a platform user: no RAG on_behalf (production requires channel
        # "dooers-platform" for on_behalf=True — tools/rag/runtime.py). Map the wire's own channel name
        # to production's (public chat: "public_chat" on the wire, "dooers-public-chat" in production).
        channel = _EXTERNAL_CHANNEL.get(actor["channel"], actor["channel"])
    context = AgentContext(
        thread_id=caller_thread_id(snapshot.get("thread"), envelope["thread_id"]),
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
        queues=workspace_catalog,
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


def is_not_a_message(snapshot: dict[str, Any], envelope: dict[str, Any]) -> bool:
    """True when the delivery is not a ``message``: a resume, or a ``form.responded`` (production ``form.response``)."""
    origin = snapshot.get("invocation_event") or snapshot["trigger_event"]
    return bool(envelope.get("resume")) or origin["type"] == "form.responded"


@asynccontextmanager
async def maestro_handler_context(
    memory: MaestroMemory,
    private_resolver: MaestroPrivateResolver,
    *,
    cancellation: MaestroCancellation | None = None,
    private_resume=None,
    checkpoint_state=None,
    options_resolver=None,
    assistant_name: str = "Assistant",
    published=None,
    content_policy=None,
) -> AsyncIterator[MaestroHandlerInput]:
    """Prepara on/memory/analytics/settings; E05-08 fornece send e roda handler.

    A assinatura handler(on, send, memory, analytics, settings) continua intacta.
    ``memory`` é ``MaestroAgentMemory`` (o ``AgentMemory`` de produção) sobre a janela de contexto desta
    execução (``MaestroMemory.window()``: lida do fim da thread); ``assistant_name`` é
    ``AgentConfig.assistant_name``, o autor que produção grava para o agente; ``published`` (o sink
    do executor) dá as saídas já aceitas, que as leituras seguintes veem como produção.
    ``content_policy`` (``MaestroContentPolicy``) é o ``allowed_content_types`` do ``AgentConfig``: para uma mensagem
    (não uma resposta de form nem uma retomada) com tipo não permitido, ``content_policy_denial`` leva o texto de negação.
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
            # The newest page and the thread's first event: the handler's memory reuses the window.
            snapshot = await memory.window().load()
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
        if "workspace" not in snapshot:
            # An older Maestro sends no catalog: the deprecated operator-supplied one, a copy per execution.
            if isinstance(resources.queues, (str, bytes)) or not all(isinstance(q, WorkspaceQueueCatalog) for q in resources.queues):
                raise MaestroContextError("CONTEXT_INVALID")
            incoming.context.queues = [queue.model_copy(deep=True) for queue in resources.queues]
            allowed = tuple(queue.slug for queue in incoming.context.queues if queue.slug)
            incoming.context.maestro = dataclasses.replace(incoming.context.maestro, allowed_queues=allowed)
        # Recursos privados podem exigir I/O; revalidar acesso remoto antes de expor input.
        with diagnostic_stage("context.revalidate", memory._envelope):
            await memory.get_thread()
        if private_resume is not None:
            from .interactions import private_context_bytes

            if private_context_bytes(await memory._client.private_resume_context()) != private_context_bytes(private_resume):
                raise MaestroContextError("ACCESS_REVOKED")
        memory._client._check_current()
        assert_options_current()
        from .persistence import MaestroAgentMemory

        agent_memory = MaestroAgentMemory(memory, assistant_name=assistant_name, published=published, thread_id=incoming.context.thread_id)
        if published is not None:
            # Plano 5, Tarefa 7: o sink manda pelo WhatsApp as mensagens de uma thread de WhatsApp
            # (F6) com os mesmos ``create_dooers_whatsapp_outbound``/``MaestroAgentApi`` de produção.
            bind_whatsapp_route = getattr(published, "bind_whatsapp_route", None)
            if bind_whatsapp_route is not None:
                from dooers.agents.server.features.channels.whatsapp.outbound import (
                    create_dooers_whatsapp_outbound,
                )

                from .agent_api import MaestroAgentApi

                bind_whatsapp_route(
                    incoming.context,
                    outbound=create_dooers_whatsapp_outbound(resources.persistence),
                    agent_api=MaestroAgentApi(memory._client.config, contracts=memory._client._contracts),
                )
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
            denial = None
            if content_policy is not None and not is_not_a_message(snapshot, memory._envelope):
                from dooers.agents.server.handlers.content_policy import format_allowed_content_policy_denial

                denial = format_allowed_content_policy_denial(
                    parts=incoming.content, allowed=content_policy.allowed, message_template=content_policy.denial_message
                )
            yield MaestroHandlerInput(incoming, agent_memory, resources.analytics, settings, denial)
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
