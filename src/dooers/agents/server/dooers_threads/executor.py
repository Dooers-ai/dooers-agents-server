"""Execução textual inline; nenhuma tarefa SDK sobrevive à requisição de entrega."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from dooers.agents.server.features.analytics.models import AnalyticsEvent
from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.protocol.dooers_threads import DooersThreadsContracts, default_contracts

from .adapter import (
    DooersThreadsAdmission,
    DooersThreadsAdmissionError,
    DooersThreadsConfirmedResult,
    DooersThreadsCurrentLease,
    DooersThreadsDeliveryAdapter,
)
from .callbacks import DooersThreadsCallbackClient, DooersThreadsCallbackError, DooersThreadsHeartbeatGate
from .client import DooersThreadsContextError
from .commercial import DooersThreadsCommercialBlocked
from .config import DooersThreadsConfig, DooersThreadsConfigurationError
from .context import DooersThreadsCancellation, DooersThreadsPrivateResolver, dooers_threads_handler_context
from .diagnostics import diagnostic_stage
from .dispatch_registry import DISPATCHES, DispatchEntry, DispatchHandlerUnavailable, handler_fingerprint, recorded
from .memory import DooersThreadsMemory
from .sink import DooersThreadsEventSink, DooersThreadsSend

logger = logging.getLogger(__name__)

#: ``AgentConfig.assistant_name`` default.
DEFAULT_ASSISTANT_NAME = "Assistant"
#: Upper bound for the analytics call that reports a handler failure (it never delays the completion more).
_FAILURE_ANALYTICS_SECONDS = 5.0
#: Context errors that mean the execution authority is gone; any other context error in the handler is its failure.
_LOST_AUTHORITY = frozenset({"ACCESS_REVOKED", "CONTEXT_EXPIRED"})
#: Context errors with their own delivery error code, as before the handler (``_context_failure``).
_OWN_CODE = frozenset({"UNSUPPORTED_CAPABILITY", "CONTEXT_LIMIT"})


@dataclass(frozen=True)
class DooersThreadsContentPolicy:
    """``AgentConfig.allowed_content_types`` and ``content_policy_denial_message`` (``handlers/content_policy.py``)."""

    allowed: frozenset[str] | None
    denial_message: str | None


def dooers_threads_content_policy(config: Any) -> DooersThreadsContentPolicy | None:
    """The content policy of a Dooers Threads-mode ``AgentConfig`` (as ``AgentServer`` reads it); none for a ``DooersThreadsConfig``."""
    from dooers.agents.server.config import AgentConfig
    from dooers.agents.server.handlers.content_policy import normalize_allowed_content_types

    if not isinstance(config, AgentConfig):
        return None
    allowed = normalize_allowed_content_types(config.allowed_content_types)
    if allowed is None:
        return None
    return DooersThreadsContentPolicy(allowed, (config.content_policy_denial_message or "").strip() or None)


def dooers_threads_settings(config: Any, assistant_name: str | None = None) -> tuple[DooersThreadsConfig, str]:
    """The ``DooersThreadsConfig`` and the assistant name of an adapter factory's ``config``.

    ``config`` is a ``DooersThreadsConfig``, or the ``AgentConfig`` of a Dooers Threads-mode agent
    (``thread_management="dooers_threads"``): then its ``dooers_threads`` and its ``assistant_name``, the author
    production writes for the agent. An explicit ``assistant_name`` wins; the default is
    production's ``"Assistant"``.
    """
    from dooers.agents.server.config import AgentConfig

    if isinstance(config, AgentConfig):
        if config.thread_management != "dooers_threads" or config.dooers_threads is None:
            raise DooersThreadsConfigurationError("DOOERS_THREADS_CONFIG_REQUIRED")
        return config.dooers_threads, assistant_name if assistant_name is not None else config.assistant_name
    return config, assistant_name if assistant_name is not None else DEFAULT_ASSISTANT_NAME


def _context_failure(error: DooersThreadsContextError) -> DooersThreadsCallbackError:
    """The delivery error of a context error: its own code for a missing link route and a too large context.

    ``UNSUPPORTED_CAPABILITY`` (422): no link route. ``CONTEXT_LIMIT`` (413 ``PAYLOAD_TOO_LARGE``;
    Dooers Threads records ``runtime_rejected_payload_too_large``): the context passed the page or byte
    limit. Anything else: the authority was lost (``LEASE_STALE``).
    """
    code = {"UNSUPPORTED_CAPABILITY": "UNSUPPORTED_CAPABILITY", "CONTEXT_LIMIT": "PAYLOAD_TOO_LARGE"}.get(error.code, "LEASE_STALE")
    return DooersThreadsCallbackError(code)


class DooersThreadsTextExecutor:
    """Consome handler(on, send, memory, analytics, settings) na mesma task.

    Heartbeat/IO são auxiliares dentro da requisição e encerrados em finally.
    Falha de complete nunca vira confirmação sintética nem retry do handler.
    Efeitos externos e tasks criadas pelo código do usuário são cooperativos;
    a autoridade de fence dos callbacks continua sendo o serviço Dooers Threads.
    """

    def __init__(
        self,
        config: DooersThreadsConfig,
        handler: Callable[..., Any],
        private_resolver: DooersThreadsPrivateResolver,
        *,
        contracts: DooersThreadsContracts | None = None,
        journal=None,
        retry_policy: str = "unsafe",
        checkpoints=None,
        approvals=None,
        options_resolver=None,
        assistant_name: str = DEFAULT_ASSISTANT_NAME,
        content_policy: DooersThreadsContentPolicy | None = None,
    ) -> None:
        if not callable(handler) or not callable(private_resolver):
            raise ValueError("DOOERS_THREADS_HANDLER_REQUIRED")
        self.config, self.handler, self.private_resolver = config, handler, private_resolver
        # ``AgentServer.dispatch`` in this process compares its handler with this one (``dispatch_registry``).
        self._fingerprint = handler_fingerprint(handler)
        DISPATCHES.mount(config.instance_agent_id, handler)
        #: ``AgentConfig.assistant_name``: the author production writes for the agent's events.
        self.assistant_name = assistant_name
        #: ``AgentConfig.allowed_content_types``: a disallowed message gets production's denial run, not the handler.
        self.content_policy = content_policy
        self._contracts = contracts or default_contracts()
        self.journal, self.retry_policy, self.checkpoints, self.approvals = journal, retry_policy, checkpoints, approvals
        if options_resolver is not None and (not callable(options_resolver) or inspect.iscoroutinefunction(options_resolver)):
            raise ValueError("DOOERS_THREADS_OPTIONS_REGISTRATION_INVALID")
        self.options_resolver = options_resolver

    async def execute(self, admission: DooersThreadsAdmission, lease: DooersThreadsCurrentLease) -> DooersThreadsConfirmedResult:
        client = DooersThreadsCallbackClient(self.config, admission, lease, contracts=self._contracts)
        from .journal_runtime import DooersThreadsEffectUnknown, JournalExecution, bind_effects

        journal_execution = JournalExecution(self.journal, client, admission, self.retry_policy) if self.journal else None
        if journal_execution:
            journal_execution.approvals = self.approvals
            try:
                with diagnostic_stage("journal.reserve", admission.envelope):
                    await journal_execution.reserve()
            except Exception:
                client.close()
                raise DooersThreadsCallbackError("DEPENDENCY_UNAVAILABLE") from None
        if (admission.envelope.get("resume") or {}).get("kind") == "tool_approval" and self.approvals is None:
            client.close()
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        private_resume = None
        checkpoint_state = None
        if admission.envelope.get("resume") and self.checkpoints is not None:
            try:
                private_resume = await client.private_resume_context()
                checkpoint_state = await self.checkpoints.load(journal_execution, private_resume)
            except BaseException:
                client.close()
                raise
        elif admission.envelope.get("resume") and self.journal:
            client.close()
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        memory = DooersThreadsMemory(client)
        cancellation = DooersThreadsCancellation()
        sink = DooersThreadsEventSink(client, checkpoints=self.checkpoints, journal_execution=journal_execution)
        owner = asyncio.current_task()
        assert owner is not None
        renewal_failure: list[Exception] = []
        internal_cancel_issued = False

        async def renew() -> None:
            nonlocal internal_cancel_issued
            try:
                while True:
                    await asyncio.sleep(self.config.heartbeat_seconds)
                    if journal_execution:
                        await journal_execution.renew()
                    else:
                        await client.heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                renewal_failure.append(error)
                cancellation.cancel()
                if owner.cancelling() == 0:
                    internal_cancel_issued = True
                    owner.cancel()

        heartbeat = asyncio.create_task(renew(), name="dooers-threads-heartbeat")

        async def stop_heartbeat() -> None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if renewal_failure:
                raise DooersThreadsCallbackError("LEASE_STALE")

        try:
            async with dooers_threads_handler_context(
                memory,
                self.private_resolver,
                cancellation=cancellation,
                private_resume=private_resume,
                checkpoint_state=checkpoint_state,
                options_resolver=self.options_resolver,
                assistant_name=self.assistant_name,
                published=sink,
                content_policy=self.content_policy,
            ) as context:
                sink.bind_queues(context.on.context.dooers_threads.allowed_queues)
                outcome, reason = "failed", "handler_error"
                failure: Exception | None = None
                # Only the delivery budget expiring is handler_timeout; a TimeoutError the handler itself raises
                # (e.g. its own asyncio.wait_for) is a handler failure, as in production.
                budget_spent = False
                budget_timer: asyncio.Timeout | None = None
                try:
                    budget = admission.remaining_seconds() - self.config.completion_budget_seconds - self.config.safety_margin_seconds
                    if budget <= 0:
                        budget_spent = True
                        raise TimeoutError
                    budget_timer = asyncio.timeout(budget)
                    async with budget_timer:
                        with bind_effects(journal_execution) if journal_execution else nullcontext():
                            # A ``dispatch`` call of this process: its handler runs and the events it yields are recorded (F5, F14).
                            dooers_threads = context.on.context.dooers_threads
                            entry = DISPATCHES.claim(dooers_threads.trigger_client_event_id, admission.envelope["execution_id"])
                            if context.attachment_failure is not None:
                                # F5c: an attachment the handler never received. Production fails the handler and
                                # shows the error text in the thread; never a silent runtime_rejected.
                                raise context.attachment_failure
                            if context.content_policy_denial:
                                result = self._denial_events(context)
                            else:
                                handler = self._handler_for(entry, dooers_threads.dispatch_handler)
                                # The production AgentMemory over the delivery context (DooersThreadsAgentMemory).
                                result = handler(context.on, DooersThreadsSend(sink), context.memory, context.analytics, context.settings)
                            if entry is not None and hasattr(result, "__aiter__"):
                                result = recorded(entry, admission.envelope["execution_id"], result)
                            if hasattr(result, "__aiter__"):
                                suspension_requested = False
                                try:
                                    async for event in result:
                                        if getattr(event, "send_type", None) in {
                                            "dooers_threads.form.suspend",
                                            "dooers_threads.approval.suspend",
                                        }:
                                            suspension_requested = True
                                            await stop_heartbeat()
                                        await sink.accept(event)
                                        if sink.suspended is not None:
                                            cancellation.cancel()
                                            client.close()
                                            if journal_execution:
                                                journal_execution.active = False
                                            break
                                finally:
                                    if suspension_requested:
                                        cancellation.cancel()
                                        client.close()
                                        if journal_execution:
                                            journal_execution.active = False
                                    close = getattr(result, "aclose", None)
                                    if close is not None:
                                        await close()
                            elif inspect.isawaitable(result):
                                if await result is not None:
                                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                            else:
                                raise DooersThreadsCallbackError("INVALID_REQUEST")
                            if sink.suspended is None:
                                outcome, reason = sink.normal_outcome()
                except DooersThreadsCommercialBlocked as error:
                    outcome, reason = "failed", error.reason_code
                except DooersThreadsEffectUnknown:
                    outcome, reason = "failed", "manual_review"
                except TimeoutError as error:
                    if budget_spent or (budget_timer is not None and budget_timer.expired()):
                        outcome, reason = "failed", "handler_timeout"
                    else:
                        outcome, reason, failure = "failed", "handler_error", error
                except DooersThreadsCallbackError as error:
                    # Unknown output/commit e perda de autoridade pertencem à
                    # recuperação durável; não sobrescrever com outra intenção.
                    if error.unknown or error.code in {
                        "LEASE_STALE",
                        "UNAUTHENTICATED",
                        "FORBIDDEN",
                        "DEPENDENCY_UNAVAILABLE",
                        "EXECUTION_TERMINAL",
                        "IDEMPOTENCY_CONFLICT",
                        "RATE_LIMITED",
                        "QUOTA_EXCEEDED",
                    }:
                        raise
                    outcome, reason = (
                        "failed",
                        "unsupported_capability" if error.code == "UNSUPPORTED_CAPABILITY" else "handler_contract_error",
                    )
                except DooersThreadsContextError as error:
                    if error.code in _LOST_AUTHORITY | _OWN_CODE:
                        # Lost authority; or the same codes as before the handler: no link route, a context over the limit.
                        raise _context_failure(error) from None
                    # A read that failed (unavailable, invalid) is the handler's failure, as in production: one
                    # terminal state, no retry of a handler that may have published outputs.
                    outcome, reason, failure = "failed", "handler_error", error
                except Exception as error:
                    outcome, reason, failure = "failed", "handler_error", error
                if failure is not None:
                    await self._report_failure(failure, sink, context, admission)
                await stop_heartbeat()
                sink.close()
                if sink.suspended is not None:
                    if journal_execution:
                        await journal_execution.confirm(sink.suspended)
                    return sink.suspended
                # Só ACK atual e validado de complete produz confirmed=True.
                async with asyncio.timeout(
                    min(self.config.completion_budget_seconds, admission.remaining_seconds() - self.config.safety_margin_seconds)
                ):
                    confirmed = await client.complete(outcome, reason, sink.event_ids)
                    if journal_execution:
                        await journal_execution.confirm(confirmed)
                    return confirmed
        except asyncio.CancelledError:
            if internal_cancel_issued:
                # Cancelamento interno é consumido somente após fechar todos os
                # recursos. Cancelamento externo do ASGI continua propagado.
                remaining_cancellations = owner.uncancel()
                if remaining_cancellations == 0:
                    raise DooersThreadsCallbackError("LEASE_STALE") from None
            raise
        except DooersThreadsAdmissionError:
            raise
        except DooersThreadsContextError as error:
            raise _context_failure(error) from None
        except Exception:
            raise DooersThreadsCallbackError() from None
        finally:
            cancellation.cancel()
            sink.close()
            client.close()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    def _handler_for(self, entry: DispatchEntry | None, marker: str | None) -> Callable[..., Any]:
        """The handler of a delivery: the one of the ``dispatch`` call of this process, else the mounted one (F5).

        A trigger event that carries the fingerprint of a handler this process neither has an entry for nor mounts was dispatched
        for another process: running the mounted handler would answer with the wrong one, so the execution fails (visibly).
        """
        if entry is not None:
            return entry.handler
        if marker is not None and marker != self._fingerprint:
            raise DispatchHandlerUnavailable(marker)
        return self.handler

    async def _denial_events(self, context):
        """``pipeline.py`` for a disallowed content type: the run, the denial as the assistant, the run ends failed."""
        send = AgentSend()
        yield send.run_start(agent_id=context.on.context.agent_id)
        yield send.text(context.content_policy_denial, author=self.assistant_name)
        yield send.run_end(status="failed", error="content_policy")

    async def _report_failure(self, error: Exception, sink: DooersThreadsEventSink, context, admission: DooersThreadsAdmission) -> None:
        """The production error path (``pipeline.py``) for an exception of the handler, best effort.

        Logs the traceback, tracks ``error.occurred``, writes the error text as a message of the current run and ends
        that run as failed. Dooers Threads has no ``system`` actor: the message is the agent's. A step that fails (too large,
        a refusal, lost authority) never hides the failure: ``complete`` still reports it.
        """
        logger.error("[agents] handler error: %s", error, exc_info=error)
        track = getattr(context.analytics, "track", None)
        if callable(track):
            try:
                budget = admission.remaining_seconds() - self.config.completion_budget_seconds - self.config.safety_margin_seconds
                async with asyncio.timeout(max(0.0, min(_FAILURE_ANALYTICS_SECONDS, budget))):
                    tracked = track(AnalyticsEvent.ERROR_OCCURRED.value, {"error": str(error), "type": type(error).__name__})
                    if inspect.isawaitable(tracked):
                        await tracked
            except Exception as analytics_error:
                logger.warning("dooers_threads handler failure: analytics track failed (%s)", type(analytics_error).__name__)
        await sink.report_handler_failure(str(error) or type(error).__name__)


def create_text_delivery_adapter(
    config: DooersThreadsConfig | Any,
    handler: Callable[..., Any],
    private_resolver: DooersThreadsPrivateResolver,
    *,
    assistant_name: str | None = None,
) -> DooersThreadsDeliveryAdapter:
    """Composição explícita local; capacidades globais continuam sem anúncio C2.

    ``config`` é o ``DooersThreadsConfig`` ou o ``AgentConfig`` do agente no modo Dooers Threads: com ele, o
    ``assistant_name`` do ``AgentConfig`` chega ao autor dos eventos do agente
    (``dooers_threads_settings``).
    """
    content_policy = dooers_threads_content_policy(config)
    config, assistant_name = dooers_threads_settings(config, assistant_name)
    contracts = default_contracts()
    return DooersThreadsDeliveryAdapter(
        config,
        lease_gate=DooersThreadsHeartbeatGate(config, contracts=contracts),
        executor=DooersThreadsTextExecutor(
            config,
            handler,
            private_resolver,
            contracts=contracts,
            assistant_name=assistant_name,
            content_policy=content_policy,
        ),
    )
