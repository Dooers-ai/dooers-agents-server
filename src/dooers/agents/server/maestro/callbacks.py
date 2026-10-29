"""Callbacks locais autorizados: ACK remoto é condição para confirmar resultado."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from dooers.agents.server.protocol.maestro import MaestroContractError, MaestroContracts

from .adapter import MaestroAdmission, MaestroAdmissionError, MaestroConfirmedResult, MaestroCurrentLease, _epoch
from .client import MaestroContextClient, MaestroContextError
from .config import MaestroConfig
from .security import strict_json


class MaestroCallbackError(MaestroAdmissionError):
    """Falha sanitizada; unknown preserva incerteza de commit, não permite sucesso."""

    def __init__(self, code: str = "DEPENDENCY_UNAVAILABLE", *, unknown: bool = False) -> None:
        super().__init__(code)
        self.unknown = unknown


class MaestroCallbackClient(MaestroContextClient):
    """Mesmo estado de capability/lease para memória, heartbeat e sink.

    Serializa mutações locais. Autorização/fence e idempotência são do Maestro,
    nunca deste lock. Não possui consulta de resultado terminal nem retry oculto.
    """

    def __init__(self, *args: Any, _gate_pending: bool = False, **kwargs: Any) -> None:
        self._gate_pending = _gate_pending
        super().__init__(*args, **kwargs)
        self._writes = asyncio.Lock()
        self._stream_last_sent = 0.0

    @property
    def current_lease(self) -> MaestroCurrentLease:
        return MaestroCurrentLease(
            self._lease.execution_id,
            self._lease.lease_fence,
            self._lease.lease_until,
            self._capability if self._capability != self.admission.execution_capability else None,
        )

    async def _post(self, action: str, value: dict[str, Any], *, key: str | None = None) -> dict[str, Any]:
        operation, scopes, request_schema, response_schema = {
            "heartbeat": ("delivery.heartbeat", ("processing:write",), "Public.HeartbeatRequest", "Public.HeartbeatResponse"),
            "stream": ("delivery.respond", ("responses:write",), "Public.StreamRequest", "Public.StreamResponse"),
            "outputs": ("delivery.respond", ("responses:write",), "Public.OutputRequest", "Public.PersistedResponse"),
            "suspend": ("delivery.suspend", ("processing:write", "responses:write"), "Public.SuspendRequest", "Public.CallbackResponse"),
            "tool-approval-suspend": ("delivery.suspend", ("processing:write", "responses:write"), None, "Public.CallbackResponse"),
            "complete": ("delivery.complete", ("processing:write",), "Public.CompleteRequest", "Public.CallbackResponse"),
        }[action]
        async with self._writes:
            sent = False
            try:
                if request_schema is None:
                    from .approvals import validate_approval_suspend

                    validate_approval_suspend(value, self._envelope)
                else:
                    self._contracts.validate(request_schema, value)
                self._check_current(reserve_completion=False)
                if operation not in self._claims["operations"]:
                    raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
                remaining = self.admission.remaining_seconds() - self.config.safety_margin_seconds
                if action != "heartbeat":
                    remaining = min(remaining, self._check_current(reserve_completion=False))
                async with asyncio.timeout(min(remaining, self.config.network_timeout_seconds)):
                    credential = await self._credential(scopes)
                    self._check_current(reserve_completion=False)
                    headers = {
                        "Authorization": f"Bearer {credential.token}",
                        "Maestro-Execution-Capability": self._capability,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-store",
                    }
                    if key is not None:
                        headers["Idempotency-Key"] = key
                    raw_request = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                    if len(raw_request) > self.config.max_output_bytes + 4096:
                        raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
                    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
                        if action == "stream":
                            delay = self._stream_last_sent + 0.05 - time.monotonic()
                            if delay > 0:
                                await asyncio.sleep(delay)
                            self._check_current(reserve_completion=False)
                            self._stream_last_sent = time.monotonic()
                        sent = True
                        url = f"{self.config.callback_base_url}/executions/{self._envelope['execution_id']}/{action}"
                        async with client.stream("POST", url, content=raw_request, headers=headers) as response:
                            if (
                                response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
                                or response.headers.get("content-encoding", "identity") != "identity"
                            ):
                                raise MaestroCallbackError(unknown=True)
                            length = response.headers.get("content-length")
                            if length is not None and (not length.isdecimal() or int(length) > self.max_page_bytes):
                                raise MaestroCallbackError(unknown=True)
                            raw = bytearray()
                            async for chunk in response.aiter_raw():
                                raw.extend(chunk)
                                if len(raw) > self.max_page_bytes:
                                    raise MaestroCallbackError(unknown=True)
                            if response.status_code != 200:
                                error_code = {
                                    401: "UNAUTHENTICATED",
                                    403: "FORBIDDEN",
                                    404: "FORBIDDEN",
                                    409: "LEASE_STALE",
                                    410: "LEASE_STALE",
                                    400: "INVALID_REQUEST",
                                    413: "PAYLOAD_TOO_LARGE",
                                    422: "UNSUPPORTED_CAPABILITY",
                                    429: "RATE_LIMITED",
                                }.get(response.status_code, "DEPENDENCY_UNAVAILABLE")
                                if response.status_code in {401, 403, 404, 409, 410}:
                                    self._closed = True
                                if response.status_code in {409, 422, 429}:
                                    try:
                                        error = self._contracts.validate("Public.Error", strict_json(bytes(raw)))["error"]["code"]
                                        if (
                                            response.status_code == 409
                                            and error in {"LEASE_STALE", "EXECUTION_TERMINAL", "IDEMPOTENCY_CONFLICT"}
                                        ) or (response.status_code in {422, 429} and error == "QUOTA_EXCEEDED"):
                                            error_code = error
                                    except Exception:
                                        pass
                                raise MaestroCallbackError(error_code, unknown=response.status_code >= 500)
                            result = self._contracts.validate(response_schema, strict_json(bytes(raw)))
                            if time.time() >= credential.expires_at:
                                raise MaestroCallbackError("UNAUTHENTICATED", unknown=True)
                            if action == "heartbeat":
                                # ACK pode atravessar exp antiga. Nova assinatura e lease
                                # são verificadas antes de serem expostas à memória.
                                old = self._capability, self._claims, self._lease
                                try:
                                    renewal = response.headers.get("Maestro-Execution-Capability")
                                    if renewal is not None:
                                        self.update_capability(renewal)
                                    lease = MaestroCurrentLease(
                                        self._envelope["execution_id"], self._envelope["lease_fence"], result["lease_until"]
                                    )
                                    if self._gate_pending:
                                        if not time.time() < _epoch(lease.lease_until) <= _epoch(self._envelope["deadline_at"]):
                                            raise ValueError
                                        self._lease = lease
                                        self._gate_pending = False
                                    else:
                                        self.update_lease(lease)
                                    self._check_current(reserve_completion=False)
                                except Exception:
                                    self._capability, self._claims, self._lease = old
                                    self._closed = True
                                    raise MaestroCallbackError("UNAUTHENTICATED") from None
                            else:
                                self._check_current(reserve_completion=False)
                            return result
            except MaestroCallbackError:
                raise
            except MaestroContextError:
                self._closed = True
                raise MaestroCallbackError("LEASE_STALE", unknown=sent) from None
            except (MaestroContractError, ValueError, TypeError, KeyError):
                raise MaestroCallbackError("INVALID_REQUEST", unknown=sent) from None
            except Exception:
                raise MaestroCallbackError(unknown=sent) from None

    async def heartbeat(self, progress_code: str = "running") -> MaestroCurrentLease:
        if progress_code not in {"admitted", "running", "run_started", "run_finished", "completing"}:
            raise MaestroCallbackError("INVALID_REQUEST")
        await self._post("heartbeat", {"lease_fence": self._envelope["lease_fence"], "progress_code": progress_code})
        return self.current_lease

    async def stream(self, output_id: str, chunk_index: int, delta: str) -> dict[str, Any]:
        if "stream.delta" not in self.config.product_capabilities:
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        try:
            if len(delta.encode("utf-8")) > 16384:
                raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
        except (UnicodeError, AttributeError):
            raise MaestroCallbackError("INVALID_REQUEST") from None
        result = await self._post(
            "stream",
            {"lease_fence": self._envelope["lease_fence"], "output_id": output_id, "chunk_index": chunk_index, "delta": delta},
        )

        if result["output_id"] != output_id or result["chunk_index"] != chunk_index:
            raise MaestroCallbackError(unknown=True)
        return result

    async def output(self, output: dict[str, Any], ordinal: int) -> dict[str, Any]:
        envelope = self._envelope
        key = f"output:{envelope['delivery_id']}:{envelope['phase_id']}:{ordinal}"
        value = {**output, "lease_fence": envelope["lease_fence"]}
        result = await self._post("outputs", value, key=key)
        event = result["event"]
        actor = event["actor"]
        payload = event["payload"]
        if (
            event["thread_id"] != envelope["thread_id"]
            or event["type"] != "message.created"
            or actor["kind"] != "agent"
            or actor["instance_agent_id"] != envelope["instance_agent_id"]
            or actor["principal_id"] != envelope["instance_agent_id"]
            or actor["participant_id"] != envelope["recipient_participant_id"]
            or event["root_event_id"] != envelope["root_event_id"]
            or event["causation_event_id"] != envelope["trigger_event_id"]
            or event.get("delivery_id") != envelope["delivery_id"]
            or event.get("output_id") != output["output_id"]
            or payload.get("output_id") != output["output_id"]
            or payload["content"] != output["payload"]["content"]
            or payload["mentions"] != output["payload"]["mentions"]
            or (
                output["payload"].get("handoff") is not None
                and (
                    payload.get("routing_decision", {}).get("mode") != "direct"
                    or payload.get("routing_decision", {}).get("recipients") != output["payload"]["handoff"]["recipients"]
                )
            )
        ):
            self._closed = True
            raise MaestroCallbackError(unknown=True)
        return result

    async def suspend(self, interaction: dict[str, Any], checkpoint_ref: str, ordinal: int) -> MaestroConfirmedResult:
        e = self._envelope
        response = await self._post(
            "suspend",
            {
                "lease_fence": e["lease_fence"],
                "output_id": f"output:{ordinal}",
                "checkpoint_ref": checkpoint_ref,
                "interaction": interaction,
            },
            key=f"suspend:{e['delivery_id']}:{e['phase_id']}",
        )
        if (
            response["delivery_id"] != e["delivery_id"]
            or response["execution_id"] != e["execution_id"]
            or (response["state"] != "waiting_input" or not response["result_event_ids"])
        ):
            raise MaestroCallbackError(unknown=True)
        return MaestroConfirmedResult(response, confirmed=True)

    async def approval_suspend(self, interaction, private, checkpoint_ref, ordinal):
        e = self._envelope
        response = await self._post(
            "tool-approval-suspend",
            {
                "lease_fence": e["lease_fence"],
                "output_id": f"output:{ordinal}",
                "checkpoint_ref": checkpoint_ref,
                "interaction": interaction,
                "approval": private,
            },
            key=f"suspend:{e['delivery_id']}:{e['phase_id']}",
        )
        if (
            response["delivery_id"] != e["delivery_id"]
            or response["execution_id"] != e["execution_id"]
            or response["state"] != "waiting_input"
            or not response["result_event_ids"]
        ):
            raise MaestroCallbackError(unknown=True)
        return MaestroConfirmedResult(response, confirmed=True)

    async def complete(self, outcome: str, reason_code: str, event_ids: tuple[str, ...] = ()) -> MaestroConfirmedResult:
        envelope = self._envelope
        response = await self._post(
            "complete",
            {"lease_fence": envelope["lease_fence"], "outcome": outcome, "reason_code": reason_code},
            key=f"complete:{envelope['delivery_id']}:{envelope['phase_id']}",
        )
        if (
            response["delivery_id"] != envelope["delivery_id"]
            or response["execution_id"] != envelope["execution_id"]
            or response["state"] != outcome
            or not set(event_ids) <= set(response["result_event_ids"])
            or (outcome == "responded" and not event_ids)
        ):
            raise MaestroCallbackError(unknown=True)
        return MaestroConfirmedResult(response, confirmed=True)


class MaestroHeartbeatGate:
    """Valida lease atual via callback antes do handler; não reconcilia terminais.

    O prazo do dispatch é somente limite provisório de rede. O snapshot autorizado
    só existe depois do ACK de heartbeat; nenhuma saída/contexto usa a hipótese.
    """

    def __init__(self, config: MaestroConfig, *, contracts: MaestroContracts | None = None) -> None:
        self.config = config
        self._contracts = contracts or MaestroContracts()

    async def admit(self, admission: MaestroAdmission) -> MaestroCurrentLease:
        envelope = admission.envelope
        provisional = MaestroCurrentLease(envelope["execution_id"], envelope["lease_fence"], envelope["deadline_at"])
        client = MaestroCallbackClient(self.config, admission, provisional, contracts=self._contracts, _gate_pending=True)
        try:
            return await client.heartbeat("admitted")
        finally:
            client.close()
