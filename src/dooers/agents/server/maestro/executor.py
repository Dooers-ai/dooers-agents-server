"""Execução textual inline; nenhuma tarefa SDK sobrevive à requisição de entrega."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from dooers.agents.server.protocol.maestro import MaestroContracts

from .adapter import MaestroAdmission, MaestroAdmissionError, MaestroConfirmedResult, MaestroCurrentLease, MaestroDeliveryAdapter
from .callbacks import MaestroCallbackClient, MaestroCallbackError, MaestroHeartbeatGate
from .client import MaestroContextError
from .commercial import MaestroCommercialBlocked
from .config import MaestroConfig
from .context import MaestroCancellation, MaestroPrivateResolver, maestro_handler_context
from .memory import MaestroMemory
from .sink import MaestroEventSink, MaestroSend


class MaestroTextExecutor:
    """Consome handler(on, send, memory, analytics, settings) na mesma task.

    Heartbeat/IO são auxiliares dentro da requisição e encerrados em finally.
    Falha de complete nunca vira confirmação sintética nem retry do handler.
    Efeitos externos e tasks criadas pelo código do usuário são cooperativos;
    a autoridade de fence dos callbacks continua sendo o serviço Maestro.
    """

    def __init__(
        self,
        config: MaestroConfig,
        handler: Callable[..., Any],
        private_resolver: MaestroPrivateResolver,
        *,
        contracts: MaestroContracts | None = None,
        journal=None,
        retry_policy: str = "unsafe",
        checkpoints=None,
        approvals=None,
        options_resolver=None,
    ) -> None:
        if not callable(handler) or not callable(private_resolver):
            raise ValueError("MAESTRO_HANDLER_REQUIRED")
        self.config, self.handler, self.private_resolver = config, handler, private_resolver
        self._contracts = contracts or MaestroContracts()
        self.journal, self.retry_policy, self.checkpoints, self.approvals = journal, retry_policy, checkpoints, approvals
        if options_resolver is not None and (not callable(options_resolver) or inspect.iscoroutinefunction(options_resolver)):
            raise ValueError("MAESTRO_OPTIONS_REGISTRATION_INVALID")
        self.options_resolver = options_resolver

    async def execute(self, admission: MaestroAdmission, lease: MaestroCurrentLease) -> MaestroConfirmedResult:
        client = MaestroCallbackClient(self.config, admission, lease, contracts=self._contracts)
        from .journal_runtime import JournalExecution, MaestroEffectUnknown, bind_effects

        journal_execution = JournalExecution(self.journal, client, admission, self.retry_policy) if self.journal else None
        if journal_execution:
            journal_execution.approvals = self.approvals
            try:
                await journal_execution.reserve()
            except Exception:
                client.close()
                raise MaestroCallbackError("DEPENDENCY_UNAVAILABLE") from None
        if (admission.envelope.get("resume") or {}).get("kind") == "tool_approval" and self.approvals is None:
            client.close()
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
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
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        memory = MaestroMemory(client)
        cancellation = MaestroCancellation()
        sink = MaestroEventSink(client, checkpoints=self.checkpoints, journal_execution=journal_execution)
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

        heartbeat = asyncio.create_task(renew(), name="maestro-heartbeat")

        async def stop_heartbeat() -> None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if renewal_failure:
                raise MaestroCallbackError("LEASE_STALE")

        try:
            async with maestro_handler_context(
                memory,
                self.private_resolver,
                cancellation=cancellation,
                private_resume=private_resume,
                checkpoint_state=checkpoint_state,
                options_resolver=self.options_resolver,
            ) as context:
                outcome, reason = "failed", "handler_error"
                try:
                    budget = admission.remaining_seconds() - self.config.completion_budget_seconds - self.config.safety_margin_seconds
                    if budget <= 0:
                        raise TimeoutError
                    async with asyncio.timeout(budget):
                        with bind_effects(journal_execution) if journal_execution else nullcontext():
                            result = self.handler(context.on, MaestroSend(sink), memory, context.analytics, context.settings)
                            if hasattr(result, "__aiter__"):
                                suspension_requested = False
                                try:
                                    async for event in result:
                                        if getattr(event, "send_type", None) in {"maestro.form.suspend", "maestro.approval.suspend"}:
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
                                    raise MaestroCallbackError("INVALID_REQUEST")
                            else:
                                raise MaestroCallbackError("INVALID_REQUEST")
                            if sink.suspended is None:
                                outcome, reason = sink.normal_outcome()
                except MaestroCommercialBlocked as error:
                    outcome, reason = "failed", error.reason_code
                except MaestroEffectUnknown:
                    outcome, reason = "failed", "manual_review"
                except TimeoutError:
                    outcome, reason = "failed", "handler_timeout"
                except MaestroCallbackError as error:
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
                except MaestroContextError:
                    raise MaestroCallbackError("LEASE_STALE") from None
                except Exception:
                    outcome, reason = "failed", "handler_error"
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
                    raise MaestroCallbackError("LEASE_STALE") from None
            raise
        except MaestroAdmissionError:
            raise
        except MaestroContextError as error:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY" if error.code == "UNSUPPORTED_CAPABILITY" else "LEASE_STALE") from None
        except Exception:
            raise MaestroCallbackError() from None
        finally:
            cancellation.cancel()
            sink.close()
            client.close()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


def create_text_delivery_adapter(
    config: MaestroConfig, handler: Callable[..., Any], private_resolver: MaestroPrivateResolver
) -> MaestroDeliveryAdapter:
    """Composição explícita local; capacidades globais continuam sem anúncio C2."""
    contracts = MaestroContracts()
    return MaestroDeliveryAdapter(
        config,
        lease_gate=MaestroHeartbeatGate(config, contracts=contracts),
        executor=MaestroTextExecutor(config, handler, private_resolver, contracts=contracts),
    )
