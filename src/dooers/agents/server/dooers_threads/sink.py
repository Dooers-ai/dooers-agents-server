"""Intenções textuais estáveis; generator emitir não significa evento persistido."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.thread_queue import resolve_thread_queue

from .callbacks import THREAD_OPERATION_OUTPUTS, DooersThreadsCallbackClient, DooersThreadsCallbackError
from .client import DooersThreadsContextError
from .media_fetch import MediaFetchError, decode_data_url, fetch_media, is_data_url
from .outputs import (
    MAX_PHASE_OUTPUTS,
    VISIBLE_REPLY_OUTPUTS,
    DooersThreadsOutput,
    RunTracker,
    UnsupportedAgentEvent,
    clean_json,
    clean_text,
    media_link_of,
    media_output,
    message_content_bytes,
    output_for,
)

logger = logging.getLogger(__name__)

#: ``send_type`` de produção que viram uma saída direta (``outputs.output_for``).
_DIRECT_SENDS = frozenset(
    {
        "text",
        "contact",
        "template",
        "reasoning",
        "tool_call",
        "tool_result",
        "tool_transaction",
        "form",
        "chart",
        "event_update",
        "thread_update",
        "participant_add",
    }
)
#: Mídia por URL: download, upload e parte ``attachment`` (ou ``media_link``).
_MEDIA_SENDS = frozenset({"image", "audio", "document"})
#: Os mesmos ``send_type`` aparecem em ``send.whatsapp.*`` com ``data["whatsapp"]`` (plano 5).
_MESSAGE_SENDS = frozenset({"text", "contact", "template"}) | _MEDIA_SENDS
_MEDIA_TIMEOUT_SECONDS = 30.0
#: Refusals that do not stop the handler. Production ``send.update_thread``/``send.add_participant``
#: never fail the handler (``pipeline.py``), so any refusal ``callbacks`` marks for them is tolerated
#: (``THREAD_OPERATION_OUTPUTS``). A ``run_start``/``run_end`` for another agent in a thread with
#: several agents is not an error either: ``run.update`` tolerates only 403 ``multi_agent_thread``.
#: ``send.update_user_event`` (``message.update``) on a missing event only logs a warning in production
#: (``pipeline.py``) and any agent may edit any event: Dooers Threads' 404 ``NOT_FOUND`` and 403
#: ``multi_agent_thread`` are tolerated too. Its 422 (an attachment Dooers Threads refuses) is not.
_TOLERATED_RUN_REFUSALS = frozenset({"multi_agent_thread"})
_TOLERATED_MESSAGE_UPDATE_REFUSALS = frozenset({"multi_agent_thread", "not_found"})


def _tolerated(output_type: str, refusal: str | None) -> bool:
    if refusal is None:
        return False
    return (
        output_type in THREAD_OPERATION_OUTPUTS
        or (output_type == "run.update" and refusal in _TOLERATED_RUN_REFUSALS)
        or (output_type == "message.update" and refusal in _TOLERATED_MESSAGE_UPDATE_REFUSALS)
    )


def _artifact_filename(name: str) -> str:
    """Nome aceito pelo contrato (sem ``/``, ``\\`` e controles; até 255 unidades UTF-16)."""
    name = re.sub(r"[/\\\x00-\x1f\x7f]", "_", name).strip() or "file"
    while len(name.encode("utf-16-le", "surrogatepass")) > 510:
        name = name[:-1]
    return name


class DooersThreadsSend(AgentSend):
    """Superfície do handler Dooers Threads: a de produção mais os extras do Dooers Threads.

    ``update_user_event``, ``update_thread`` e ``add_participant`` são os de
    ``AgentSend``. ``send.whatsapp.*`` vai pelo WhatsApp como produção (plano 5, Tarefa 7).
    """

    def __init__(self, sink: DooersThreadsEventSink | None = None) -> None:
        super().__init__()
        self._sink = sink

    def reply(self, text: str) -> AgentEvent:
        """Publish without invoking another agent in routing v1."""
        return self.text(text)

    def handoff(self, participant_id: str, text: str) -> AgentEvent:
        """Request one explicit, authorized invocation within the current causal budget."""
        return AgentEvent("dooers_threads.handoff", {"participant_id": participant_id, "text": text})

    def form(
        self,
        message: str,
        elements: list[dict],
        submit_label: str = "Send",
        cancel_label: str = "Cancel",
        size: str = "medium",
        *,
        suspension=None,
    ) -> AgentEvent:
        """Without ``suspension``: the production form (a ``form.create`` output).

        With ``suspension``: explicit Dooers Threads suspension. In that opt-in surface,
        elements are canonical fields and message is title. Labels/size remain
        legacy-only; no silent conversion of callback controls.
        """
        if suspension is None:
            return AgentSend.form(self, message, elements, submit_label, cancel_label, size)
        from .interactions import DooersThreadsFormSuspension

        if not isinstance(suspension, DooersThreadsFormSuspension) or self._sink is None:
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        self._sink._active()
        if (
            self._sink._checkpoints is None
            or self._sink._journal is None
            or ("delivery.suspend" not in self._sink._client._claims["operations"])
        ):
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        if (submit_label, cancel_label, size) != ("Send", "Cancel", "medium"):
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        interaction = {
            "kind": "form",
            "schema_version": 1,
            "title": message,
            "fields": deepcopy(elements),
            "expires_at": suspension.expires_at,
            "eligible_participant_ids": list(suspension.eligible_participant_ids),
            "actions": ["submit", "cancel"],
        }
        self._sink._client._contracts.validate(
            "Public.SuspendRequest",
            {
                "lease_fence": self._sink._client._envelope["lease_fence"],
                "output_id": f"output:{len(self._sink._intents)}",
                "checkpoint_ref": "checkpoint:v1:00000000-0000-4000-8000-000000000000",
                "interaction": interaction,
            },
        )
        # Validate private schema BEFORE constructing/publishing any impossible form.
        self._sink._checkpoints._state(suspension.checkpoint)
        return AgentEvent("dooers_threads.form.suspend", {"interaction": interaction, "checkpoint": suspension.checkpoint})

    def tool_approval(self, tool, *, arguments, checkpoint, expires_at, eligible_participant_ids, ordinal):
        """Request is private-bound before any interface is emitted. No legacy override."""
        from .approvals import DooersThreadsApprovalRuntime

        runtime = getattr(self._sink._journal, "approvals", None) if self._sink else None
        if not isinstance(runtime, DooersThreadsApprovalRuntime):
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        self._sink._active()
        data = runtime.request(
            self._sink._journal,
            tool,
            arguments=arguments,
            checkpoint=checkpoint,
            expires_at=expires_at,
            eligible_participant_ids=eligible_participant_ids,
            ordinal=ordinal,
        )
        return AgentEvent("dooers_threads.approval.suspend", data)

    def approval_result(self, outcome):
        if self._sink is None:
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        self._sink._active()
        issued = getattr(self._sink._journal, "_approval_outcomes", {}).get(id(outcome))
        if not issued or issued[0] is not outcome:
            raise DooersThreadsCallbackError("FORBIDDEN")
        return AgentEvent("dooers_threads.approval.result", {"outcome": outcome})

    def open_text(self):
        """Abre intenção explícita; finish é obrigatório para persistir o texto."""
        if self._sink is None:
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        return self._sink.open_text()

    def artifact(self, data: bytes, *, filename: str, mime_type: str) -> AgentEvent:
        """Private bytes go to authorized upload, then one canonical attachment output."""
        return AgentEvent("dooers_threads.artifact", {"data": data, "filename": filename, "mime_type": mime_type})

    def transcription(self, text: str) -> AgentEvent:
        """Append-only derivation: normal agent output causally linked to its trigger."""
        return self.text(f"[Audio transcription]\n{text}")

    def silence(self, reason_code: str | None = None) -> AgentEvent:
        if reason_code is not None and (not isinstance(reason_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason_code)):
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        return AgentEvent("silence", {"reason_code": reason_code or "explicit_silent"})


@dataclass(frozen=True)
class DooersThreadsOutputIntent:
    """Bytes imutáveis da saída; hash local não substitui hash/ledger Dooers Threads."""

    ordinal: int
    body: bytes = field(repr=False)
    sha256: str

    @property
    def output(self) -> dict[str, Any]:
        return json.loads(self.body)


class DooersThreadsEventSink:
    """Uma saída por chamada ``send.*`` de produção, na ordem do handler.

    Uma mensagem completa por send.text, nunca concatenação/stream implícito.
    """

    def __init__(self, client: DooersThreadsCallbackClient, *, checkpoints=None, journal_execution=None) -> None:
        self._client = client
        self._checkpoints, self._journal = checkpoints, journal_execution
        self.suspended = None
        self._task = asyncio.current_task()
        self._intents: list[DooersThreadsOutputIntent | None] = []
        self._streams: list[Any] = []
        self._stream_last_sent = 0.0
        self._receipts: dict[int, dict[str, Any]] = {}
        #: artifact_id -> the URL ``send.image``/``audio``/``document`` received, for each file uploaded.
        self._uploaded_urls: dict[str, str] = {}
        self._silent: str | None = None
        #: Slugs of the workspace queue catalog (production ``allowed_queues``): see :meth:`bind_queues`.
        self._allowed_queues: tuple[str, ...] = ()
        self._finished = False
        self._failed = False
        self._runs = RunTracker()
        #: Thread's ``channel``/``channel_meta`` and ``agent_id`` (an ``AgentContext``-shaped object), the
        #: outbound callback and the agent API: see :meth:`bind_whatsapp_route` (plano 5, Tarefa 7).
        self._whatsapp_context: Any = None
        self._whatsapp_outbound: Any = None
        self._whatsapp_api: Any = None

    def bind_queues(self, allowed_queues: tuple[str, ...]) -> None:
        """The workspace catalog slugs (``DooersThreadsExecutionContext.allowed_queues``) that ``send.update_thread`` checks."""
        self._allowed_queues = tuple(allowed_queues)

    def bind_whatsapp_route(self, context: Any, *, outbound: Any, agent_api: Any) -> None:
        """``dooers_threads_handler_context`` binds this once per execution (``context.py``).

        ``context`` is the handler's own ``on.context`` (an ``AgentContext``): it has the same
        ``channel``/``channel_meta``/``user.user_id``/``agent_id`` production's own ``HandlerContext``
        has, so ``HandlerPipeline._resolve_whatsapp_event`` works on it unchanged. ``outbound`` is
        ``create_dooers_whatsapp_outbound(resources.persistence)`` (production's own factory);
        ``agent_api`` is a ``DooersThreadsAgentApi`` for ``record_channel_delivery``.
        """
        self._whatsapp_context = context
        self._whatsapp_outbound = outbound
        self._whatsapp_api = agent_api

    def _resolve_whatsapp_route(self, event: AgentEvent) -> AgentEvent:
        """F6: production's own ``_resolve_whatsapp_event`` (static, no Dooers Threads-specific logic)."""
        if self._whatsapp_context is None:
            return event
        from dooers.agents.server.handlers.pipeline import HandlerPipeline

        return HandlerPipeline._resolve_whatsapp_event(event, self._whatsapp_context)

    async def _dispatch_whatsapp(self, event: AgentEvent, ordinal: int) -> None:
        """``pipeline.py`` ``_dispatch_whatsapp_outbound``/``_record_whatsapp_delivery``: same outbound call and

        the same initial ``record_channel_delivery`` write production makes on the thread event. ``ordinal`` is
        the Dooers Threads output position this message was persisted at (before this call): its ``event_id`` is
        the delivery's target. A silent skip (no route, no outbound bound, no dict outcome, or a status
        outside ``{"accepted", "failed"}``) matches production exactly.
        """
        if self._whatsapp_outbound is None or self._whatsapp_api is None:
            return
        if not event.data.get("whatsapp"):
            return
        receipt = self._receipts.get(ordinal)
        if receipt is None:
            return
        outcome = await self._whatsapp_outbound(event, self._whatsapp_context)
        if not isinstance(outcome, dict):
            return
        status = str(outcome.get("status") or "").strip()
        if status not in {"accepted", "failed"}:
            return
        provider_message_id = str(outcome.get("wa_message_id") or "").strip() or None
        error_code = outcome.get("error_code")
        message = str(outcome.get("error_message") or "").strip()
        await self._whatsapp_api.record_channel_delivery(
            thread_id=self._client._envelope["thread_id"],
            event_id=receipt["event"]["event_id"],
            status=status,
            provider_message_id=provider_message_id,
            error=message[:500] if message else None,
            error_code=error_code,
        )

    def _production_thread_update(self, event: AgentEvent) -> AgentEvent | None:
        """``pipeline.py`` ``thread_update``: the queue goes through ``resolve_thread_queue``.

        Strip and lower case; a malformed slug, a slug outside the catalog (when there is one) and any
        queue on a personal thread are refused with a warning, the title still applies and the queue
        stays as it is. ``None`` when nothing is left to send (the update had only the refused queue).
        """
        if "queue" not in event.data:
            return event
        data = dict(event.data)
        try:
            resolved = resolve_thread_queue(
                data["queue"], workspace_id=self._client._envelope["workspace_id"], allowed=self._allowed_queues
            )
            # "" clears the queue: ``_publish`` sends it as ``queue: null`` (``output_for`` drops a ``None``).
            data["queue"] = "" if resolved is None else resolved
        except ValueError:
            logger.warning("[agents] thread_update rejected queue=%s thread=%s", data["queue"], self._client._envelope["thread_id"])
            del data["queue"]
            if not data:
                return None
        return AgentEvent("thread_update", data)

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(receipt["event"]["event_id"] for receipt in self._receipts.values())

    @property
    def published_events(self) -> list[dict[str, Any]]:
        """The events Dooers Threads stored for the accepted outputs (the echoes), in Dooers Threads order, once each.

        Production stores each yielded event before the handler reads again; the handler's
        memory adds these to the execution snapshot (read-your-writes). The order is the
        Dooers Threads sequence, not the ordinal: a stream keeps its ordinal while it is open, and the
        outputs persisted meanwhile come before its final message. Copies.
        """
        events = {receipt["event"]["event_id"]: receipt["event"] for receipt in self._receipts.values()}
        ordered = sorted(events.values(), key=lambda event: (len(event["sequence"]), event["sequence"]))
        return [deepcopy(event) for event in ordered]

    @property
    def published_media_urls(self) -> dict[str, str]:
        """The URL each uploaded file came from (production keeps it in the stored part)."""
        return dict(self._uploaded_urls)

    @property
    def intents(self) -> tuple[DooersThreadsOutputIntent, ...]:
        return tuple(intent for intent in self._intents if intent is not None)

    def close(self) -> None:
        self._finished = True
        for stream in self._streams:
            stream.close()

    def _active(self) -> None:
        if self._finished or self._failed or asyncio.current_task() is not self._task:
            raise DooersThreadsCallbackError("LEASE_STALE")
        self._client._check_current()

    def open_text(self):
        from .streaming import DooersThreadsTextStream

        self._active()
        if "stream.delta" not in self._client.config.product_capabilities or "delivery.respond" not in self._client._claims["operations"]:
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        if self._silent is not None:
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        if len(self._intents) >= MAX_PHASE_OUTPUTS or sum(not stream.closed for stream in self._streams) >= 16:
            raise DooersThreadsCallbackError("QUOTA_EXCEEDED")
        ordinal = len(self._intents)
        self._intents.append(None)
        stream = DooersThreadsTextStream(self, ordinal)
        self._streams.append(stream)
        return stream

    def _check_message_size(self, payload: dict[str, Any]) -> None:
        """The one local size check of a message (and of a message edit), counted like Dooers Threads counts it."""
        try:
            too_large = message_content_bytes(payload) > self._client.config.max_output_bytes
        except (ValueError, TypeError, AttributeError, KeyError):  # not a string, not UTF-8 (lone surrogate), not an object
            raise DooersThreadsCallbackError("INVALID_REQUEST") from None
        if too_large:
            raise DooersThreadsCallbackError("PAYLOAD_TOO_LARGE")

    def prepare_text(self, text: str, ordinal: int, handoff: str | None = None) -> DooersThreadsOutputIntent:
        if not isinstance(text, str):
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        text = clean_text(text)
        if not text.strip():
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        self._check_message_size({"content": [{"type": "text", "text": text}]})
        output = {
            "output_id": f"output:{ordinal}",
            "type": "message.create",
            "payload": {
                "content": [{"type": "text", "text": text}],
                "mentions": [],
                **({"handoff": {"recipients": [handoff]}} if handoff is not None else {}),
            },
        }
        self._client._contracts.validate("Public.OutputRequest", {**output, "lease_fence": self._client._envelope["lease_fence"]})
        raw = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return DooersThreadsOutputIntent(ordinal, raw, hashlib.sha256(raw).hexdigest())

    async def persist_with_retry(self, intent: DooersThreadsOutputIntent) -> dict[str, Any]:
        try:
            return await self.persist(intent)
        except DooersThreadsCallbackError as error:
            if not error.unknown:
                raise
            return await self.persist(intent)

    async def persist(self, intent: DooersThreadsOutputIntent) -> dict[str, Any]:
        """Retry explícito da mesma intenção. Não aloca ordinal nem altera bytes."""
        self._active()
        if (
            not isinstance(intent, DooersThreadsOutputIntent)
            or not 0 <= intent.ordinal < len(self._intents)
            or self._intents[intent.ordinal] != intent
        ):
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        receipt = await self._client.output(intent.output, intent.ordinal)
        previous = self._receipts.get(intent.ordinal)
        if previous and previous["event"]["event_id"] != receipt["event"]["event_id"]:
            self._failed = True
            raise DooersThreadsCallbackError(unknown=True)
        self._receipts[intent.ordinal] = deepcopy(receipt)
        return receipt

    async def accept(self, event: AgentEvent) -> None:
        self._active()
        try:
            if not isinstance(event, AgentEvent) or not isinstance(event.data, dict):
                raise DooersThreadsCallbackError("INVALID_REQUEST")
            if self._silent is not None and event.send_type != "run_end":
                raise DooersThreadsCallbackError("INVALID_REQUEST")
            if event.send_type in _MESSAGE_SENDS:
                # F6 (plano 5): toda mensagem comum de uma thread de WhatsApp vai pelo WhatsApp,
                # não só ``send.whatsapp.*`` (que já chega com ``data["whatsapp"]`` preenchido).
                event = self._resolve_whatsapp_route(event)
            if event.send_type in _DIRECT_SENDS:
                ordinal = len(self._intents)
                await self._publish(event)
                if event.send_type in _MESSAGE_SENDS:
                    await self._dispatch_whatsapp(event, ordinal)
            elif event.send_type in _MEDIA_SENDS:
                ordinal = len(self._intents)
                await self._publish_media(event)
                await self._dispatch_whatsapp(event, ordinal)
            elif event.send_type == "dooers_threads.handoff":
                if set(event.data) != {"participant_id", "text"} or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                intent = self.prepare_text(event.data["text"], len(self._intents), event.data["participant_id"])
                self._intents.append(intent)
                await self.persist_with_retry(intent)
            elif event.send_type == "dooers_threads.form.suspend":
                if self._checkpoints is None or self._journal is None or set(event.data) != {"interaction", "checkpoint"}:
                    raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
                if any(intent is None for intent in self._intents) or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                descriptor = await self._checkpoints.save(self._journal, event.data["checkpoint"], len(self._intents))
                # Unknown ACK propagates; the executor never proceeds or completes.
                self.suspended = await self._client.suspend(event.data["interaction"], descriptor["checkpoint_ref"], len(self._intents))
                self.close()
            elif event.send_type == "dooers_threads.approval.suspend":
                runtime = getattr(self._journal, "approvals", None)
                if runtime is None or self._checkpoints is None or set(event.data) != {"interaction", "approval", "checkpoint"}:
                    raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
                runtime.current(self._journal)
                from .approval_journal import digest

                checkpoint = event.data["checkpoint"]
                expected = getattr(self._journal, "_approval_requests", {}).get(id(event.data))
                if expected != digest(
                    {
                        "interaction": event.data["interaction"],
                        "approval": event.data["approval"],
                        "checkpoint_state": checkpoint.state,
                        "schema": checkpoint.schema,
                        "version": checkpoint.version,
                    }
                ):
                    raise DooersThreadsCallbackError("FORBIDDEN")
                if any(intent is None for intent in self._intents) or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                descriptor = await self._checkpoints.save(self._journal, event.data["checkpoint"], len(self._intents))
                runtime.current(self._journal)
                self.suspended = await self._client.approval_suspend(
                    event.data["interaction"], event.data["approval"], descriptor["checkpoint_ref"], len(self._intents)
                )
                self.close()
            elif event.send_type == "dooers_threads.approval.result":
                issued = getattr(self._journal, "_approval_outcomes", {}).get(id(event.data.get("outcome")))
                if set(event.data) != {"outcome"} or not issued or issued[0] is not event.data["outcome"]:
                    raise DooersThreadsCallbackError("FORBIDDEN")
                outcome, context, tool, _intent = issued
                runtime = self._journal.approvals
                await runtime.gate(self._journal, context, tool)
                # Projection is generated from the immutable allowlisted public
                # definition, never provider bytes. Reentry/retry emits same bytes.
                text = "Concluído: " + context["approval_snapshot"]["public_interaction"]["definition"]["action_description"]
                prior = getattr(self, "_approval_outputs", {})
                intent = prior.get(outcome.effect_key)
                if intent is None:
                    ordinal = len(self._intents)
                    intent = self.prepare_text(text, ordinal)
                    self._intents.append(intent)
                    prior[outcome.effect_key] = intent
                    self._approval_outputs = prior
                await runtime.gate(self._journal, context, tool)
                await self.persist_with_retry(intent)
            elif event.send_type == "dooers_threads.artifact":
                from .artifacts import DooersThreadsArtifactsClient

                if set(event.data) != {"data", "filename", "mime_type"} or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                ordinal = len(self._intents)
                attachment = await DooersThreadsArtifactsClient(self._client).upload(**event.data, ordinal=ordinal)
                output = {"output_id": f"output:{ordinal}", "type": "message.create", "payload": {"content": [attachment], "mentions": []}}
                self._client._contracts.validate("Public.OutputRequest", {**output, "lease_fence": self._client._envelope["lease_fence"]})
                raw = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                intent = DooersThreadsOutputIntent(ordinal, raw, hashlib.sha256(raw).hexdigest())
                self._intents.append(intent)
                await self.persist_with_retry(intent)
            elif event.send_type == "silence":
                # Valid until a visible reply is recorded: Dooers Threads refuses silent with one. Run, reasoning and
                # tool outputs recorded before it stay, and the phase completes silent with this reason.
                # An open stream (a ``None`` placeholder) will publish a message: no silence with it.
                if self._visible_reply() or None in self._intents or set(event.data) != {"reason_code"}:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                reason = event.data["reason_code"]
                if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason):
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                self._silent = reason
            elif event.send_type == "run_start":
                if set(event.data) != {"agent_id"}:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                await self._client.heartbeat("run_started")
                await self._publish(event)
            elif event.send_type == "run_end":
                if set(event.data) != {"status", "error"} or event.data["status"] not in {"succeeded", "failed"}:
                    raise DooersThreadsCallbackError("INVALID_REQUEST")
                await self._client.heartbeat("run_finished")
                await self._publish(event)  # failed is a run status, not a phase failure
            else:
                raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY")
        except Exception:
            self._failed = True
            raise

    async def report_handler_failure(self, text: str) -> None:
        """Production's error path for a handler exception, in the thread (``pipeline.py``), best effort.

        The error text as a message of the current run, then that run ends as failed (``run_end`` with the same text
        as ``error``). With no current run, only the message. Each step is independent: one that Dooers Threads refuses (too
        large, a rule) or that the lost authority stops is logged and skipped. The executor still completes the
        phase as ``failed/handler_error``.
        """
        failing_run = self._runs.current
        steps = [AgentEvent("text", {"text": text, "author": None})]
        if failing_run is not None:
            steps.append(AgentEvent("run_end", {"status": "failed", "error": text}))
        for step in steps:
            try:
                self._active()
                await self._publish(step)
            except Exception as error:
                logger.warning(
                    "dooers_threads handler failure: send.%s not published (%s)",
                    step.send_type,
                    getattr(error, "code", type(error).__name__),
                )

    def _output_intent(self, output: DooersThreadsOutput, ordinal: int) -> DooersThreadsOutputIntent:
        # Like production's persistence: no NUL, no lone surrogate (Dooers Threads refuses both with a 422).
        value = {"output_id": f"output:{ordinal}", "type": output.type, "payload": clean_json(output.payload)}
        if output.type in {"message.create", "message.update"}:
            self._check_message_size(value["payload"])
        try:
            self._client._contracts.validate("Public.OutputRequest", {**value, "lease_fence": self._client._envelope["lease_fence"]})
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError):  # DooersThreadsContractError is a ValueError
            raise DooersThreadsCallbackError("INVALID_REQUEST") from None
        return DooersThreadsOutputIntent(ordinal, raw, hashlib.sha256(raw).hexdigest())

    async def _persist_output(self, output: DooersThreadsOutput, ordinal: int) -> bool:
        """Persist one output. ``False`` when Dooers Threads refused it as a tolerated rule."""
        if ordinal >= MAX_PHASE_OUTPUTS:
            raise DooersThreadsCallbackError("PAYLOAD_TOO_LARGE")
        intent = self._output_intent(output, ordinal)
        self._intents.append(intent)
        try:
            await self.persist_with_retry(intent)
        except DooersThreadsCallbackError as error:
            if error.unknown or not _tolerated(output.type, error.rule_refusal):
                raise
            # Dooers Threads refused it as a rule (nothing recorded): the handler continues, as in
            # production, and the ordinal is free for the next output.
            self._intents.pop()
            logger.warning("dooers_threads %s refused (reason=%s); the handler continues", output.type, error.rule_refusal)
            return False
        return True

    async def _publish(self, event: AgentEvent) -> None:
        """Saída de produção (índice 4.4); ``None`` não envia nada e não consome ordinal."""
        if event.send_type == "participant_add":
            user = event.data.get("user")
            if not isinstance(user, dict):
                raise DooersThreadsCallbackError("INVALID_REQUEST")
            if not user.get("user_id"):
                # Production: no user_id (e-mail only or empty) adds nobody and never fails.
                logger.warning("dooers_threads send.add_participant without user_id: skipped")
                return
        if event.send_type == "run_start" and event.data.get("agent_id") == self._client.config.instance_agent_id:
            # Production passes its own id (pipeline.py); Dooers Threads records the executing agent anyway.
            event = AgentEvent("run_start", {**event.data, "agent_id": None})
        if event.send_type == "thread_update":
            event = self._production_thread_update(event)
            if event is None:
                return
        ordinal = len(self._intents)
        current_run = self._runs.current
        try:
            output = output_for(event, ordinal, runs=self._runs)
        except UnsupportedAgentEvent:
            raise DooersThreadsCallbackError("UNSUPPORTED_CAPABILITY") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise DooersThreadsCallbackError("INVALID_REQUEST") from None
        if output is None:
            return
        if output.type == "thread.update" and output.payload.get("queue") == "":
            output.payload["queue"] = None  # Dooers Threads stores `queue || null`; production clears the queue
        if not await self._persist_output(output, ordinal):
            self._runs.restore(current_run)  # the run changes only when Dooers Threads accepts run.update

    async def _publish_media(self, event: AgentEvent) -> None:
        """``send.image``/``audio``/``document``: a URL vira artefato do Dooers Threads.

        Produção guarda só a URL e nunca falha por causa dela. Aqui o SDK baixa a URL
        (``fetch_media``; uma URL ``data:`` é decodificada no local) e envia o arquivo
        (``upload``). Se o download falha, o tipo sai da política de produção ou o
        Dooers Threads recusa o arquivo, a mensagem leva a parte ``media_link`` com a URL
        original e os campos da mídia, como a produção os guarda (índice 4.3), e o
        handler continua. Qualquer texto de URL vale, como em produção.
        """
        ordinal = len(self._intents)
        if ordinal >= MAX_PHASE_OUTPUTS:
            raise DooersThreadsCallbackError("PAYLOAD_TOO_LARGE")
        if not isinstance(event.data.get("url"), str):
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        part = await self._media_part(event, ordinal)
        await self._persist_output(media_output(event, part, runs=self._runs), ordinal)

    async def _media_part(self, event: AgentEvent, ordinal: int) -> dict[str, Any]:
        from .artifacts import DooersThreadsArtifactsClient, media_url_upload_allowed

        data = event.data
        extra = {key: data[key] for key in ("alt", "duration") if data.get(key) is not None}
        limits = {
            "max_bytes": self._client.config.max_artifact_bytes,
            "hint_mime": data.get("mime_type"),
            "hint_filename": data.get("filename"),
        }
        try:
            if is_data_url(data["url"]):
                fetched = decode_data_url(data["url"], **limits)
            else:
                fetched = await fetch_media(data["url"], timeout_s=min(_MEDIA_TIMEOUT_SECONDS, self._client._check_current()), **limits)
        except MediaFetchError as error:
            return self._media_link(event, error.reason)
        self._active()
        filename = _artifact_filename(fetched.filename)
        if not media_url_upload_allowed(self._client.config, fetched.mime_type, filename):
            return self._media_link(event, "policy")
        try:
            attachment = await DooersThreadsArtifactsClient(self._client).upload(
                fetched.data, filename=filename, mime_type=fetched.mime_type, ordinal=ordinal
            )
        except DooersThreadsContextError as error:
            # Refused, timed out or unavailable: production never fails on the file. Lost access
            # (ACCESS_REVOKED) or an invalid answer (CONTEXT_INVALID) still stop the run.
            reason = {"CONTEXT_LIMIT": "upload_rejected", "CONTEXT_UNAVAILABLE": "upload_unavailable"}.get(error.code)
            if reason is None:
                raise
            return self._media_link(event, reason)
        self._uploaded_urls[attachment["artifact_id"]] = data["url"]
        return {**attachment, **extra}

    def _media_link(self, event: AgentEvent, reason: str) -> dict[str, Any]:
        # Only the reason code: the URL may carry a token or the file (diagnostics.py rule). Production keeps any URL
        # text the handler gave, so the media_link keeps it too, whatever its scheme.
        logger.warning("dooers_threads send.%s: media URL not uploaded (reason=%s); publishing media_link", event.send_type, reason)
        return media_link_of(event.send_type, event.data)

    def _visible_reply(self) -> bool:
        return any(intent is not None and intent.output["type"] in VISIBLE_REPLY_OUTPUTS for intent in self._intents)

    def normal_outcome(self) -> tuple[str, str]:
        self._active()
        if any(intent is None for intent in self._intents):
            raise DooersThreadsCallbackError("INVALID_REQUEST")
        if self._intents and len(self._receipts) != len(self._intents):
            raise DooersThreadsCallbackError(unknown=True)
        # Dooers Threads: responded needs a visible reply in the phase; silent refuses one (M07-005/006).
        return ("responded", "responded") if self._visible_reply() else ("silent", self._silent or "implicit_empty")
