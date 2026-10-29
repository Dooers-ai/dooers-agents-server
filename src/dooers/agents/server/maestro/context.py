"""Contexto aditivo e escopo privado por instância destinatária."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from weakref import WeakKeyDictionary

from dooers.agents.server.protocol.models import TextPart, User
from dooers.tools.whatsapp.runtime import (
    bind_handler_context,
    bind_handler_scope_guard,
    reset_handler_context,
    reset_handler_scope_guard,
)

from .client import MaestroContextError
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


async def _incoming(
    memory: MaestroMemory, snapshot: dict[str, Any], cancellation: MaestroCancellation, private_resume=None, checkpoint_state=None
) -> AgentIncoming:
    from dooers.agents.server.handlers.context import AgentContext
    from dooers.agents.server.handlers.incoming import AgentIncoming

    envelope = memory._envelope
    trigger = snapshot["trigger_event"]
    origin = snapshot.get("invocation_event") or trigger
    resume = envelope.get("resume")
    form_data, form_event_id = None, None
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
    context = AgentContext(
        thread_id=envelope["thread_id"],
        agent_id=envelope["instance_agent_id"],
        event_id=origin["event_id"],
        organization_id=envelope["organization_id"],
        workspace_id=envelope["workspace_id"],
        channel="maestro",
        user=User(user_id=actor["principal_id"], user_name=actor["display_name"]) if actor["kind"] == "human" else None,
        maestro=metadata,
    )
    parts = [] if resume else origin["payload"]["content"]
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
            hydrated.append(artifact.content_part())
        else:
            raise MaestroContextError("UNSUPPORTED_CAPABILITY")
        if total > memory._client.config.max_hydrated_bytes:
            hydrated.clear()
            raise MaestroContextError("CONTEXT_LIMIT")
    if sum(len(part["text"].encode("utf-8")) for part in parts if part["type"] == "text") > memory._client.config.max_content_bytes:
        raise MaestroContextError("CONTEXT_LIMIT")
    return AgentIncoming(
        "\n".join(part["text"] for part in parts if part["type"] == "text"),
        hydrated,
        context,
        form_data=form_data,
        form_event_id=form_event_id,
        _bind_runtime_context=False,
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
                resources = await private_resolver(scope)
        except Exception:
            raise MaestroContextError() from None
        assert_options_current()
        if not isinstance(resources, MaestroPrivateResources) or resources.scope != scope:
            raise MaestroContextError("ACCESS_REVOKED")
        # Recursos privados podem exigir I/O; revalidar acesso remoto antes de expor input.
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
        active = True
        try:
            assert_options_current()
            incoming.bind_runtime_context()
            tokens = bind_handler_context(persistence=resources.persistence, agent_id=scope.instance_agent_id)
            guard_token = bind_handler_scope_guard(lambda: active and asyncio.current_task() is task and not cancellation.cancelled)
            _active[task] = incoming.context.maestro
            settings = TurnSettings(resources.settings, selected_options) if selected_options else resources.settings
            yield MaestroHandlerInput(incoming, memory, resources.analytics, settings)
        finally:
            active = False
            try:
                incoming.reset_runtime_context()
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
