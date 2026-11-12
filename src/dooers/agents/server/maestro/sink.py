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
from urllib.parse import urlsplit

from dooers.agents.server.handlers.send import AgentEvent, AgentSend

from .callbacks import THREAD_OPERATION_OUTPUTS, MaestroCallbackClient, MaestroCallbackError
from .client import MaestroContextError
from .media_fetch import MediaFetchError, decode_data_url, fetch_media, is_data_url
from .outputs import (
    MAX_PHASE_OUTPUTS,
    VISIBLE_REPLY_OUTPUTS,
    MaestroOutput,
    RunTracker,
    UnsupportedAgentEvent,
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
_TOLERATED_RUN_REFUSALS = frozenset({"multi_agent_thread"})


def _tolerated(output_type: str, refusal: str | None) -> bool:
    if refusal is None:
        return False
    return output_type in THREAD_OPERATION_OUTPUTS or (output_type == "run.update" and refusal in _TOLERATED_RUN_REFUSALS)


def _artifact_filename(name: str) -> str:
    """Nome aceito pelo contrato (sem ``/``, ``\\`` e controles; até 255 unidades UTF-16)."""
    name = re.sub(r"[/\\\x00-\x1f\x7f]", "_", name).strip() or "file"
    while len(name.encode("utf-16-le", "surrogatepass")) > 510:
        name = name[:-1]
    return name


class MaestroSend(AgentSend):
    """Superfície do handler Maestro: a de produção mais os extras do Maestro.

    ``update_user_event``, ``update_thread`` e ``add_participant`` são os de
    ``AgentSend``. ``send.whatsapp.*`` falha no sink (plano 5).
    """

    def __init__(self, sink: MaestroEventSink | None = None) -> None:
        super().__init__()
        self._sink = sink

    def reply(self, text: str) -> AgentEvent:
        """Publish without invoking another agent in routing v1."""
        return self.text(text)

    def handoff(self, participant_id: str, text: str) -> AgentEvent:
        """Request one explicit, authorized invocation within the current causal budget."""
        return AgentEvent("maestro.handoff", {"participant_id": participant_id, "text": text})

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

        With ``suspension``: explicit Maestro suspension. In that opt-in surface,
        elements are canonical fields and message is title. Labels/size remain
        legacy-only; no silent conversion of callback controls.
        """
        if suspension is None:
            return AgentSend.form(self, message, elements, submit_label, cancel_label, size)
        from .interactions import MaestroFormSuspension

        if not isinstance(suspension, MaestroFormSuspension) or self._sink is None:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        self._sink._active()
        if (
            self._sink._checkpoints is None
            or self._sink._journal is None
            or ("delivery.suspend" not in self._sink._client._claims["operations"])
        ):
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        if (submit_label, cancel_label, size) != ("Send", "Cancel", "medium"):
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
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
        return AgentEvent("maestro.form.suspend", {"interaction": interaction, "checkpoint": suspension.checkpoint})

    def tool_approval(self, tool, *, arguments, checkpoint, expires_at, eligible_participant_ids, ordinal):
        """Request is private-bound before any interface is emitted. No legacy override."""
        from .approvals import MaestroApprovalRuntime

        runtime = getattr(self._sink._journal, "approvals", None) if self._sink else None
        if not isinstance(runtime, MaestroApprovalRuntime):
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
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
        return AgentEvent("maestro.approval.suspend", data)

    def approval_result(self, outcome):
        if self._sink is None:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        self._sink._active()
        issued = getattr(self._sink._journal, "_approval_outcomes", {}).get(id(outcome))
        if not issued or issued[0] is not outcome:
            raise MaestroCallbackError("FORBIDDEN")
        return AgentEvent("maestro.approval.result", {"outcome": outcome})

    def open_text(self):
        """Abre intenção explícita; finish é obrigatório para persistir o texto."""
        if self._sink is None:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        return self._sink.open_text()

    def artifact(self, data: bytes, *, filename: str, mime_type: str) -> AgentEvent:
        """Private bytes go to authorized upload, then one canonical attachment output."""
        return AgentEvent("maestro.artifact", {"data": data, "filename": filename, "mime_type": mime_type})

    def transcription(self, text: str) -> AgentEvent:
        """Append-only derivation: normal agent output causally linked to its trigger."""
        return self.text(f"[Audio transcription]\n{text}")

    def silence(self, reason_code: str | None = None) -> AgentEvent:
        if reason_code is not None and (not isinstance(reason_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason_code)):
            raise MaestroCallbackError("INVALID_REQUEST")
        return AgentEvent("silence", {"reason_code": reason_code or "explicit_silent"})


@dataclass(frozen=True)
class MaestroOutputIntent:
    """Bytes imutáveis da saída; hash local não substitui hash/ledger Maestro."""

    ordinal: int
    body: bytes = field(repr=False)
    sha256: str

    @property
    def output(self) -> dict[str, Any]:
        return json.loads(self.body)


class MaestroEventSink:
    """Uma saída por chamada ``send.*`` de produção, na ordem do handler.

    Uma mensagem completa por send.text, nunca concatenação/stream implícito.
    """

    def __init__(self, client: MaestroCallbackClient, *, checkpoints=None, journal_execution=None) -> None:
        self._client = client
        self._checkpoints, self._journal = checkpoints, journal_execution
        self.suspended = None
        self._task = asyncio.current_task()
        self._intents: list[MaestroOutputIntent | None] = []
        self._streams: list[Any] = []
        self._stream_last_sent = 0.0
        self._receipts: dict[int, dict[str, Any]] = {}
        #: artifact_id -> the URL ``send.image``/``audio``/``document`` received, for each file uploaded.
        self._uploaded_urls: dict[str, str] = {}
        self._silent: str | None = None
        self._finished = False
        self._failed = False
        self._runs = RunTracker()

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(receipt["event"]["event_id"] for receipt in self._receipts.values())

    @property
    def published_events(self) -> list[dict[str, Any]]:
        """The events Maestro stored for the accepted outputs (the echoes), in output order.

        Production stores each yielded event before the handler reads again; the handler's
        memory adds these to the execution snapshot (read-your-writes). Copies.
        """
        return [deepcopy(self._receipts[ordinal]["event"]) for ordinal in sorted(self._receipts)]

    @property
    def published_media_urls(self) -> dict[str, str]:
        """The URL each uploaded file came from (production keeps it in the stored part)."""
        return dict(self._uploaded_urls)

    @property
    def intents(self) -> tuple[MaestroOutputIntent, ...]:
        return tuple(intent for intent in self._intents if intent is not None)

    def close(self) -> None:
        self._finished = True
        for stream in self._streams:
            stream.close()

    def _active(self) -> None:
        if self._finished or self._failed or asyncio.current_task() is not self._task:
            raise MaestroCallbackError("LEASE_STALE")
        self._client._check_current()

    def open_text(self):
        from .streaming import MaestroTextStream

        self._active()
        if "stream.delta" not in self._client.config.product_capabilities or "delivery.respond" not in self._client._claims["operations"]:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        if self._silent is not None:
            raise MaestroCallbackError("INVALID_REQUEST")
        if len(self._intents) >= MAX_PHASE_OUTPUTS or sum(not stream.closed for stream in self._streams) >= 16:
            raise MaestroCallbackError("QUOTA_EXCEEDED")
        ordinal = len(self._intents)
        self._intents.append(None)
        stream = MaestroTextStream(self, ordinal)
        self._streams.append(stream)
        return stream

    def _check_message_size(self, payload: dict[str, Any]) -> None:
        """The one local size check of a message (and of a message edit), counted like Maestro counts it."""
        try:
            too_large = message_content_bytes(payload) > self._client.config.max_output_bytes
        except (ValueError, TypeError, AttributeError, KeyError):  # not a string, not UTF-8 (lone surrogate), not an object
            raise MaestroCallbackError("INVALID_REQUEST") from None
        if too_large:
            raise MaestroCallbackError("PAYLOAD_TOO_LARGE")

    def prepare_text(self, text: str, ordinal: int, handoff: str | None = None) -> MaestroOutputIntent:
        if not isinstance(text, str) or not text.strip():
            raise MaestroCallbackError("INVALID_REQUEST")
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
        return MaestroOutputIntent(ordinal, raw, hashlib.sha256(raw).hexdigest())

    async def persist_with_retry(self, intent: MaestroOutputIntent) -> dict[str, Any]:
        try:
            return await self.persist(intent)
        except MaestroCallbackError as error:
            if not error.unknown:
                raise
            return await self.persist(intent)

    async def persist(self, intent: MaestroOutputIntent) -> dict[str, Any]:
        """Retry explícito da mesma intenção. Não aloca ordinal nem altera bytes."""
        self._active()
        if (
            not isinstance(intent, MaestroOutputIntent)
            or not 0 <= intent.ordinal < len(self._intents)
            or self._intents[intent.ordinal] != intent
        ):
            raise MaestroCallbackError("INVALID_REQUEST")
        receipt = await self._client.output(intent.output, intent.ordinal)
        previous = self._receipts.get(intent.ordinal)
        if previous and previous["event"]["event_id"] != receipt["event"]["event_id"]:
            self._failed = True
            raise MaestroCallbackError(unknown=True)
        self._receipts[intent.ordinal] = deepcopy(receipt)
        return receipt

    async def accept(self, event: AgentEvent) -> None:
        self._active()
        try:
            if not isinstance(event, AgentEvent) or not isinstance(event.data, dict):
                raise MaestroCallbackError("INVALID_REQUEST")
            if self._silent is not None and event.send_type != "run_end":
                raise MaestroCallbackError("INVALID_REQUEST")
            if event.send_type in _MESSAGE_SENDS and "whatsapp" in event.data:
                raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")  # send.whatsapp.*: plano 5
            if event.send_type in _DIRECT_SENDS:
                await self._publish(event)
            elif event.send_type in _MEDIA_SENDS:
                await self._publish_media(event)
            elif event.send_type == "maestro.handoff":
                if set(event.data) != {"participant_id", "text"} or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise MaestroCallbackError("INVALID_REQUEST")
                intent = self.prepare_text(event.data["text"], len(self._intents), event.data["participant_id"])
                self._intents.append(intent)
                await self.persist_with_retry(intent)
            elif event.send_type == "maestro.form.suspend":
                if self._checkpoints is None or self._journal is None or set(event.data) != {"interaction", "checkpoint"}:
                    raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
                if any(intent is None for intent in self._intents) or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise MaestroCallbackError("INVALID_REQUEST")
                descriptor = await self._checkpoints.save(self._journal, event.data["checkpoint"], len(self._intents))
                # Unknown ACK propagates; the executor never proceeds or completes.
                self.suspended = await self._client.suspend(event.data["interaction"], descriptor["checkpoint_ref"], len(self._intents))
                self.close()
            elif event.send_type == "maestro.approval.suspend":
                runtime = getattr(self._journal, "approvals", None)
                if runtime is None or self._checkpoints is None or set(event.data) != {"interaction", "approval", "checkpoint"}:
                    raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
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
                    raise MaestroCallbackError("FORBIDDEN")
                if any(intent is None for intent in self._intents) or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise MaestroCallbackError("INVALID_REQUEST")
                descriptor = await self._checkpoints.save(self._journal, event.data["checkpoint"], len(self._intents))
                runtime.current(self._journal)
                self.suspended = await self._client.approval_suspend(
                    event.data["interaction"], event.data["approval"], descriptor["checkpoint_ref"], len(self._intents)
                )
                self.close()
            elif event.send_type == "maestro.approval.result":
                issued = getattr(self._journal, "_approval_outcomes", {}).get(id(event.data.get("outcome")))
                if set(event.data) != {"outcome"} or not issued or issued[0] is not event.data["outcome"]:
                    raise MaestroCallbackError("FORBIDDEN")
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
            elif event.send_type == "maestro.artifact":
                from .artifacts import MaestroArtifactsClient

                if set(event.data) != {"data", "filename", "mime_type"} or len(self._intents) >= MAX_PHASE_OUTPUTS:
                    raise MaestroCallbackError("INVALID_REQUEST")
                ordinal = len(self._intents)
                attachment = await MaestroArtifactsClient(self._client).upload(**event.data, ordinal=ordinal)
                output = {"output_id": f"output:{ordinal}", "type": "message.create", "payload": {"content": [attachment], "mentions": []}}
                self._client._contracts.validate("Public.OutputRequest", {**output, "lease_fence": self._client._envelope["lease_fence"]})
                raw = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                intent = MaestroOutputIntent(ordinal, raw, hashlib.sha256(raw).hexdigest())
                self._intents.append(intent)
                await self.persist_with_retry(intent)
            elif event.send_type == "silence":
                # Valid until a visible reply is recorded: Maestro refuses silent with one. Run, reasoning and
                # tool outputs recorded before it stay, and the phase completes silent with this reason.
                # An open stream (a ``None`` placeholder) will publish a message: no silence with it.
                if self._visible_reply() or None in self._intents or set(event.data) != {"reason_code"}:
                    raise MaestroCallbackError("INVALID_REQUEST")
                reason = event.data["reason_code"]
                if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason):
                    raise MaestroCallbackError("INVALID_REQUEST")
                self._silent = reason
            elif event.send_type == "run_start":
                if set(event.data) != {"agent_id"}:
                    raise MaestroCallbackError("INVALID_REQUEST")
                await self._client.heartbeat("run_started")
                await self._publish(event)
            elif event.send_type == "run_end":
                if set(event.data) != {"status", "error"} or event.data["status"] not in {"succeeded", "failed"}:
                    raise MaestroCallbackError("INVALID_REQUEST")
                await self._client.heartbeat("run_finished")
                await self._publish(event)  # failed is a run status, not a phase failure
            else:
                raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        except Exception:
            self._failed = True
            raise

    def _output_intent(self, output: MaestroOutput, ordinal: int) -> MaestroOutputIntent:
        value = {"output_id": f"output:{ordinal}", "type": output.type, "payload": output.payload}
        if output.type in {"message.create", "message.update"}:
            self._check_message_size(output.payload)
        try:
            self._client._contracts.validate("Public.OutputRequest", {**value, "lease_fence": self._client._envelope["lease_fence"]})
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError):  # MaestroContractError is a ValueError
            raise MaestroCallbackError("INVALID_REQUEST") from None
        return MaestroOutputIntent(ordinal, raw, hashlib.sha256(raw).hexdigest())

    async def _persist_output(self, output: MaestroOutput, ordinal: int) -> bool:
        """Persist one output. ``False`` when Maestro refused it as a tolerated rule."""
        if ordinal >= MAX_PHASE_OUTPUTS:
            raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
        intent = self._output_intent(output, ordinal)
        self._intents.append(intent)
        try:
            await self.persist_with_retry(intent)
        except MaestroCallbackError as error:
            if error.unknown or not _tolerated(output.type, error.rule_refusal):
                raise
            # Maestro refused it as a rule (nothing recorded): the handler continues, as in
            # production, and the ordinal is free for the next output.
            self._intents.pop()
            logger.warning("maestro %s refused (reason=%s); the handler continues", output.type, error.rule_refusal)
            return False
        return True

    async def _publish(self, event: AgentEvent) -> None:
        """Saída de produção (índice 4.4); ``None`` não envia nada e não consome ordinal."""
        if event.send_type == "participant_add":
            user = event.data.get("user")
            if not isinstance(user, dict):
                raise MaestroCallbackError("INVALID_REQUEST")
            if not user.get("user_id"):
                # Production: no user_id (e-mail only or empty) adds nobody and never fails.
                logger.warning("maestro send.add_participant without user_id: skipped")
                return
        if event.send_type == "run_start" and event.data.get("agent_id") == self._client.config.instance_agent_id:
            # Production passes its own id (pipeline.py); Maestro records the executing agent anyway.
            event = AgentEvent("run_start", {**event.data, "agent_id": None})
        ordinal = len(self._intents)
        current_run = self._runs.current
        try:
            output = output_for(event, ordinal, runs=self._runs)
        except UnsupportedAgentEvent:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MaestroCallbackError("INVALID_REQUEST") from None
        if output is None:
            return
        if output.type == "thread.update" and output.payload.get("queue") == "":
            output.payload["queue"] = None  # Maestro stores `queue || null`; production clears the queue
        if not await self._persist_output(output, ordinal):
            self._runs.restore(current_run)  # the run changes only when Maestro accepts run.update

    async def _publish_media(self, event: AgentEvent) -> None:
        """``send.image``/``audio``/``document``: a URL vira artefato do Maestro.

        Produção guarda só a URL e nunca falha por causa dela. Aqui o SDK baixa a URL
        (``fetch_media``; uma URL ``data:`` é decodificada no local) e envia o arquivo
        (``upload``). Se o download falha, o tipo sai da política de produção ou o
        Maestro recusa o arquivo, a mensagem leva a parte ``media_link`` com a URL
        original (índice 4.3) e o handler continua. O ``media_link`` só aceita
        ``http``/``https``: com outra URL (``data:`` incluída), esses casos falham a
        execução (``INVALID_REQUEST``).
        """
        ordinal = len(self._intents)
        if ordinal >= MAX_PHASE_OUTPUTS:
            raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
        if not isinstance(event.data.get("url"), str):
            raise MaestroCallbackError("INVALID_REQUEST")
        part = await self._media_part(event, ordinal)
        await self._persist_output(media_output(event, part, runs=self._runs), ordinal)

    async def _media_part(self, event: AgentEvent, ordinal: int) -> dict[str, Any]:
        from .artifacts import MaestroArtifactsClient, artifact_type_allowed

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
        if not artifact_type_allowed(self._client.config, fetched.mime_type, filename):
            return self._media_link(event, "policy")
        try:
            attachment = await MaestroArtifactsClient(self._client).upload(
                fetched.data, filename=filename, mime_type=fetched.mime_type, ordinal=ordinal
            )
        except MaestroContextError as error:
            # Refused, timed out or unavailable: production never fails on the file. Lost access
            # (ACCESS_REVOKED) or an invalid answer (CONTEXT_INVALID) still stop the run.
            reason = {"CONTEXT_LIMIT": "upload_rejected", "CONTEXT_UNAVAILABLE": "upload_unavailable"}.get(error.code)
            if reason is None:
                raise
            return self._media_link(event, reason)
        self._uploaded_urls[attachment["artifact_id"]] = data["url"]
        return {**attachment, **extra}

    def _media_link(self, event: AgentEvent, reason: str) -> dict[str, Any]:
        # Only the reason code: the URL may carry a token or the file (diagnostics.py rule).
        try:
            scheme = urlsplit(event.data["url"]).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme not in {"http", "https"}:
            logger.warning("maestro send.%s: media URL not uploaded (reason=%s); no media_link for this scheme", event.send_type, reason)
            raise MaestroCallbackError("INVALID_REQUEST")
        logger.warning("maestro send.%s: media URL not uploaded (reason=%s); publishing media_link", event.send_type, reason)
        keys = ("mime_type", "filename", "alt", "duration")
        return {"type": "media_link", "kind": event.send_type, "url": event.data["url"]} | {
            key: event.data[key] for key in keys if event.data.get(key) is not None
        }

    def _visible_reply(self) -> bool:
        return any(intent is not None and intent.output["type"] in VISIBLE_REPLY_OUTPUTS for intent in self._intents)

    def normal_outcome(self) -> tuple[str, str]:
        self._active()
        if any(intent is None for intent in self._intents):
            raise MaestroCallbackError("INVALID_REQUEST")
        if self._intents and len(self._receipts) != len(self._intents):
            raise MaestroCallbackError(unknown=True)
        # Maestro: responded needs a visible reply in the phase; silent refuses one (M07-005/006).
        return ("responded", "responded") if self._visible_reply() else ("silent", self._silent or "implicit_empty")
