"""Intenções textuais estáveis; generator emitir não significa evento persistido."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from dooers.agents.server.handlers.send import AgentEvent, AgentSend

from .callbacks import MaestroCallbackClient, MaestroCallbackError


class MaestroSend(AgentSend):
    """Superfície do handler Maestro. Recursos não textuais falham no sink."""

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
        """Explicit Maestro suspension; legacy AgentSend.form is unchanged.

        In this opt-in surface, elements are canonical fields and message is title.
        Labels/size remain legacy-only; no silent conversion of callback controls.
        """
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

    def update_user_event(self, event_id: str, content: list[dict]) -> AgentEvent:
        raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")

    def update_thread(self, *, title: str | None = None) -> AgentEvent:
        raise MaestroCallbackError("FORBIDDEN")

    def add_participant(self, user_id: str, **kwargs: Any) -> AgentEvent:
        raise MaestroCallbackError("FORBIDDEN")


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
    """Uma mensagem completa por send.text, nunca concatenação/stream implícito."""

    def __init__(self, client: MaestroCallbackClient, *, checkpoints=None, journal_execution=None) -> None:
        self._client = client
        self._checkpoints, self._journal = checkpoints, journal_execution
        self.suspended = None
        self._task = asyncio.current_task()
        self._intents: list[MaestroOutputIntent | None] = []
        self._streams: list[Any] = []
        self._stream_last_sent = 0.0
        self._receipts: dict[int, dict[str, Any]] = {}
        self._silent: str | None = None
        self._finished = False
        self._failed = False

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(receipt["event"]["event_id"] for receipt in self._receipts.values())

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
        if len(self._intents) >= 98 or sum(not stream.closed for stream in self._streams) >= 16:
            raise MaestroCallbackError("QUOTA_EXCEEDED")
        ordinal = len(self._intents)
        self._intents.append(None)
        stream = MaestroTextStream(self, ordinal)
        self._streams.append(stream)
        return stream

    def prepare_text(self, text: str, ordinal: int, handoff: str | None = None) -> MaestroOutputIntent:
        if not isinstance(text, str) or not text.strip():
            raise MaestroCallbackError("INVALID_REQUEST")
        try:
            if len(text.encode("utf-8")) > self._client.config.max_output_bytes:
                raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
        except UnicodeError:
            raise MaestroCallbackError("INVALID_REQUEST") from None
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
            if event.send_type == "text":
                if set(event.data) - {"text", "author"} or event.data.get("author") is not None:
                    raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
                if len(self._intents) >= 98:
                    raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
                intent = self.prepare_text(event.data.get("text"), len(self._intents))
                self._intents.append(intent)
                await self.persist_with_retry(intent)
            elif event.send_type == "maestro.handoff":
                if set(event.data) != {"participant_id", "text"} or len(self._intents) >= 98:
                    raise MaestroCallbackError("INVALID_REQUEST")
                intent = self.prepare_text(event.data["text"], len(self._intents), event.data["participant_id"])
                self._intents.append(intent)
                await self.persist_with_retry(intent)
            elif event.send_type == "maestro.form.suspend":
                if self._checkpoints is None or self._journal is None or set(event.data) != {"interaction", "checkpoint"}:
                    raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
                if any(intent is None for intent in self._intents) or len(self._intents) >= 98:
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
                if any(intent is None for intent in self._intents) or len(self._intents) >= 98:
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

                if set(event.data) != {"data", "filename", "mime_type"} or len(self._intents) >= 98:
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
                if self._intents or set(event.data) != {"reason_code"}:
                    raise MaestroCallbackError("INVALID_REQUEST")
                reason = event.data["reason_code"]
                if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason):
                    raise MaestroCallbackError("INVALID_REQUEST")
                self._silent = reason
            elif event.send_type == "run_start":
                if set(event.data) != {"agent_id"} or event.data["agent_id"] not in {None, self._client.config.instance_agent_id}:
                    raise MaestroCallbackError("FORBIDDEN")
                await self._client.heartbeat("run_started")
            elif event.send_type == "run_end":
                if set(event.data) != {"status", "error"} or event.data["status"] not in {"succeeded", "failed"}:
                    raise MaestroCallbackError("INVALID_REQUEST")
                if event.data["status"] == "failed":
                    raise MaestroCallbackError("INVALID_REQUEST")
                await self._client.heartbeat("run_finished")
            else:
                raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        except Exception:
            self._failed = True
            raise

    def normal_outcome(self) -> tuple[str, str]:
        self._active()
        if any(intent is None for intent in self._intents):
            raise MaestroCallbackError("INVALID_REQUEST")
        if self._intents and len(self._receipts) != len(self._intents):
            raise MaestroCallbackError(unknown=True)
        return ("responded", "responded") if self._receipts else ("silent", self._silent or "implicit_empty")
