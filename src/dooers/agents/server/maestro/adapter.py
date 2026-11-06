"""Admissão HTTP separada de histórico, contexto e executor legado."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from dooers.agents.server.protocol.maestro.contracts import MaestroContractError, MaestroContracts

from .config import MaestroConfig
from .diagnostics import diagnostic_stage
from .security import load_public_keys, renewed_capability, strict_json, verify_jwt


class MaestroAdmissionError(Exception):
    """Falha fechada; detalhes privados nunca entram na resposta HTTP."""

    def __init__(self, code: str = "DEPENDENCY_UNAVAILABLE") -> None:
        allowed = {
            "UNAUTHENTICATED",
            "FORBIDDEN",
            "INVALID_REQUEST",
            "PAYLOAD_TOO_LARGE",
            "RATE_LIMITED",
            "QUOTA_EXCEEDED",
            "DEPENDENCY_UNAVAILABLE",
            "LEASE_STALE",
            "EXECUTION_TERMINAL",
            "IDEMPOTENCY_CONFLICT",
            "UNSUPPORTED_CAPABILITY",
        }
        self.code = code if code in allowed else "DEPENDENCY_UNAVAILABLE"
        super().__init__(self.code)


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class MaestroAdmission:
    """Dispatch autenticado, ainda SEM autoridade de lease corrente.

    Só adapter constrói para os ports confiáveis. Envelope/claims são devolvidos
    por cópia; capability é sensível e nunca deve ser persistida ou registrada.
    """

    _envelope: dict[str, Any] = field(repr=False)
    _capability_claims: dict[str, Any] = field(repr=False)
    execution_capability: str = field(repr=False)
    deadline_monotonic: float

    @property
    def envelope(self) -> dict[str, Any]:
        return deepcopy(self._envelope)

    @property
    def capability_claims(self) -> dict[str, Any]:
        return deepcopy(self._capability_claims)

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())


@dataclass(frozen=True)
class MaestroCurrentLease:
    """Snapshot retornado pelo port confiável após consulta autenticada atual.

    Não receber este tipo do HTTP. O port verifica TODOS os bindings do admission,
    core/membership/cancelamento e fence no Maestro. O snapshot não autoriza writes
    futuros: E05-08 revalida cada callback e mantém heartbeat.
    """

    execution_id: str
    lease_fence: str
    lease_until: str
    execution_capability: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class MaestroConfirmedResult:
    """Confirmação interna do Maestro, nunca simples retorno do handler.

    `confirmed` só é True após resposta autenticada de complete/suspend ou leitura
    autenticada do resultado durável da mesma execução. ACK perdido/timeout sem
    reconciliação não é confirmação. Tipagem/schema não provam commit: o port é
    dependência confiável do operador e deve demonstrar essa condição em E05-08.
    """

    response: dict[str, Any] = field(repr=False)
    confirmed: bool = False


class MaestroLeaseGate(Protocol):
    async def admit(self, admission: MaestroAdmission) -> MaestroCurrentLease | MaestroConfirmedResult:
        """Autoriza lease atual ou reconcilia duplicata terminal, antes de contexto."""
        ...


class MaestroExecutor(Protocol):
    async def execute(self, admission: MaestroAdmission, lease: MaestroCurrentLease) -> MaestroConfirmedResult:
        """Executa inline com callbacks autorizados; só retorna commit confirmado."""
        ...


class MaestroDeliveryAdapter:
    """ASGI independente. Nenhum pipeline, DB, handler ou cliente implícito.

    Pode ser montado no prefixo do agente; `runtime_endpoint` mantém o caminho
    externo completo. `Host`/forwarded headers não escolhem audience/callback.
    Ports ausentes falham fechado. Todas as tarefas terminam antes da resposta.
    """

    def __init__(
        self, config: MaestroConfig, *, lease_gate: MaestroLeaseGate | None = None, executor: MaestroExecutor | None = None
    ) -> None:
        if not isinstance(config, MaestroConfig):
            raise ValueError("MAESTRO_CONFIG_REQUIRED")
        config.validate_startup()
        self.config = config
        self._contracts = MaestroContracts()
        self._keys = load_public_keys(config.trusted_public_keys)
        self._lease_gate = lease_gate
        self._executor = executor
        self._active = 0
        self._executing: set[tuple[str, str]] = set()

    @property
    def capabilities(self) -> tuple[str, ...]:
        # E05-06 é admissão. Configurar ports não comprova suporte end-to-end.
        return ()

    def authenticate(self, body: bytes, headers: Mapping[str, str]) -> MaestroAdmission:
        """Entrada já persistida, autenticada sem obter contexto/conteúdo privado."""
        if len(body) > self.config.max_dispatch_bytes:
            raise MaestroAdmissionError("PAYLOAD_TOO_LARGE")
        try:
            self._contracts.validate("Delivery.DispatchHeaders", dict(headers))
            dh, dc = verify_jwt(headers["Authorization"][7:], typ="maestro-dispatch+jwt", keys=self._keys)
            ch, cc = verify_jwt(headers["Maestro-Execution-Capability"], typ="maestro-execution-capability+jwt", keys=self._keys)
            envelope = strict_json(body)
            now = time.time()
            self._contracts.validate(
                "Delivery.DispatchBinding",
                {
                    "envelope": envelope,
                    "dispatch_header": dh,
                    "capability_header": ch,
                    "trusted_key_ids": list(self._keys),
                    "dispatch_claims": dc,
                    "capability_claims": cc,
                    "configured_runtime_endpoint": self.config.runtime_endpoint,
                    "configured_callback_base_url": self.config.callback_base_url,
                    "authorized_instance_agent_id": self.config.instance_agent_id,
                    "authorized_runtime_id": self.config.runtime_id,
                    "now": datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                },
            )
            if envelope["organization_id"] != self.config.organization_id or envelope["workspace_id"] != self.config.workspace_id:
                raise ValueError
            remaining = _epoch(envelope["deadline_at"]) - now
            if not 0 < remaining <= self.config.execution_deadline_seconds:
                raise ValueError
            # Nenhuma dependência recebe JWT de dispatch. Somente a capability
            # própria é transportada para callbacks futuros com token de instância.
            return MaestroAdmission(deepcopy(envelope), deepcopy(cc), headers["Maestro-Execution-Capability"], time.monotonic() + remaining)
        except Exception:
            raise MaestroAdmissionError("UNAUTHENTICATED") from None

    async def _execute(self, admission: MaestroAdmission) -> dict[str, Any]:
        if self._lease_gate is None or self._executor is None:
            raise MaestroAdmissionError()
        remaining = admission.remaining_seconds()
        if remaining <= self.config.completion_budget_seconds + self.config.safety_margin_seconds:
            raise MaestroAdmissionError("LEASE_STALE")
        async with asyncio.timeout(remaining):
            async with asyncio.timeout(min(self.config.admission_timeout_seconds, remaining)):
                with diagnostic_stage("admission.lease", admission.envelope):
                    lease = await self._lease_gate.admit(admission)
            envelope = admission.envelope
            if isinstance(lease, MaestroConfirmedResult):
                result = lease
            else:
                if not isinstance(lease, MaestroCurrentLease):
                    raise MaestroAdmissionError()
                try:
                    self._contracts.validate("Public.HeartbeatResponse", {"lease_until": lease.lease_until})
                    deadline = _epoch(envelope["deadline_at"])
                    if (
                        lease.execution_id != envelope["execution_id"]
                        or lease.lease_fence != envelope["lease_fence"]
                        or not time.time() + 20 <= _epoch(lease.lease_until) <= deadline
                    ):
                        raise ValueError
                except (MaestroContractError, ValueError, TypeError, OverflowError):
                    raise MaestroAdmissionError("LEASE_STALE") from None
                if admission.remaining_seconds() <= self.config.completion_budget_seconds + self.config.safety_margin_seconds:
                    raise MaestroAdmissionError("LEASE_STALE")
                try:
                    claims = admission.capability_claims
                    if lease.execution_capability is not None:
                        claims = renewed_capability(lease.execution_capability, claims, keys=self._keys, contracts=self._contracts)
                    if time.time() >= claims["exp"]:
                        raise ValueError
                except Exception:
                    raise MaestroAdmissionError("UNAUTHENTICATED") from None
                result = await self._executor.execute(admission, lease)
            if not isinstance(result, MaestroConfirmedResult) or result.confirmed is not True:
                raise MaestroAdmissionError()
            response = self._contracts.validate("Public.CallbackResponse", result.response)
            if response["delivery_id"] != envelope["delivery_id"] or response["execution_id"] != envelope["execution_id"]:
                raise MaestroAdmissionError()
            # Mensagem ou suspensão exige evidência de evento; tipo do evento e
            # atomicidade são responsabilidade do callback autenticado, não do DTO.
            if response["state"] in {"responded", "waiting_input"} and not response["result_event_ids"]:
                raise MaestroAdmissionError()
            if admission.remaining_seconds() <= 0:
                raise MaestroAdmissionError("LEASE_STALE")
            return response

    async def _run_connected(self, admission: MaestroAdmission, receive: Callable[[], Awaitable[dict]]) -> dict[str, Any]:
        async def disconnected() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                # Não aceitar corpo extra depois do fim declarado.
                if message.get("body"):
                    raise MaestroAdmissionError("INVALID_REQUEST")

        execution = asyncio.create_task(self._execute(admission))
        watcher = asyncio.create_task(disconnected())
        try:
            done, _pending = await asyncio.wait({execution, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                watcher.result()
                raise MaestroAdmissionError("DEPENDENCY_UNAVAILABLE")
            return execution.result()
        finally:
            # Nenhum worker em RAM sobrevive ao retorno HTTP, mesmo em falha.
            for task in (execution, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, watcher, return_exceptions=True)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            raise ValueError("MAESTRO_HTTP_ONLY")
        status = 200
        response: dict[str, Any]
        acquired = False
        try:
            path = scope.get("path", "")
            root = scope.get("root_path", "")
            full_path = path if path.startswith(root + "/") else root + path
            if full_path != self.config.delivery_path or scope.get("query_string"):
                raise MaestroAdmissionError("INVALID_REQUEST")
            if scope.get("method") != "POST":
                raise MaestroAdmissionError("INVALID_REQUEST")
            raw_headers = scope.get("headers", [])
            selected: dict[str, str] = {}
            names = {
                b"authorization": "Authorization",
                b"maestro-execution-capability": "Maestro-Execution-Capability",
                b"content-type": "Content-Type",
                b"content-length": "Content-Length",
            }
            for name, value in raw_headers:
                name = name.lower()
                if name in {b"origin", b"cookie", b"content-encoding"}:
                    raise MaestroAdmissionError("FORBIDDEN")
                if name in names:
                    key = names[name]
                    if key in selected or len(value) > 16400:
                        raise MaestroAdmissionError("INVALID_REQUEST")
                    selected[key] = value.decode("ascii")
            declared = selected.pop("Content-Length", None)
            if declared is not None:
                if not declared.isdecimal() or len(declared) > 10:
                    raise MaestroAdmissionError("INVALID_REQUEST")
                if int(declared) > self.config.max_dispatch_bytes:
                    raise MaestroAdmissionError("PAYLOAD_TOO_LARGE")
            if self._active >= self.config.max_concurrency:
                raise MaestroAdmissionError("RATE_LIMITED")
            self._active += 1
            acquired = True
            async with asyncio.timeout(self.config.request_timeout_seconds):
                data = bytearray()
                async with asyncio.timeout(self.config.network_timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] != "http.request":
                            raise MaestroAdmissionError("INVALID_REQUEST")
                        data.extend(message.get("body", b""))
                        if len(data) > self.config.max_dispatch_bytes:
                            raise MaestroAdmissionError("PAYLOAD_TOO_LARGE")
                        if not message.get("more_body", False):
                            break
                if declared is not None and len(data) != int(declared):
                    raise MaestroAdmissionError("INVALID_REQUEST")
                admission = self.authenticate(bytes(data), selected)
                key = (admission.envelope["execution_id"], admission.envelope["lease_fence"])
                if key in self._executing:
                    raise MaestroAdmissionError("LEASE_STALE")
                self._executing.add(key)
                try:
                    response = await self._run_connected(admission, receive)
                finally:
                    self._executing.discard(key)
        except MaestroAdmissionError as error:
            status, response = self._error(error.code)
        except (MaestroContractError, TimeoutError):
            status, response = self._error("DEPENDENCY_UNAVAILABLE")
        except Exception:
            status, response = self._error("DEPENDENCY_UNAVAILABLE")
        finally:
            if acquired:
                self._active -= 1
        import json

        body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        headers = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if status == 429:
            headers.append((b"retry-after", b"1"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _error(code: str) -> tuple[int, dict[str, Any]]:
        status = {
            "UNAUTHENTICATED": 401,
            "FORBIDDEN": 403,
            "INVALID_REQUEST": 400,
            "PAYLOAD_TOO_LARGE": 413,
            "RATE_LIMITED": 429,
            "QUOTA_EXCEEDED": 429,
            "LEASE_STALE": 409,
            "EXECUTION_TERMINAL": 409,
            "IDEMPOTENCY_CONFLICT": 409,
            "UNSUPPORTED_CAPABILITY": 422,
        }.get(code, 503)
        return status, {
            "error": {
                "code": code,
                "message": "Maestro delivery request rejected.",
                "retryable": status in {429, 503},
                "request_id": str(uuid4()),
            }
        }
