"""Opt-in terminal receipt reconciliation; never grants a current execution lease."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from dooers.agents.server.protocol.maestro import MaestroContracts
from dooers.agents.server.protocol.maestro.journal import ExecutionAuthority, JournalScope

from .adapter import MaestroAdmission, MaestroAdmissionError, MaestroConfirmedResult, _epoch
from .callbacks import MaestroCallbackError, MaestroHeartbeatGate
from .client import resolve_instance_credential
from .journal_store import PostgresEffectJournal, canonical
from .security import load_public_keys, strict_json, verify_jwt


class MaestroDurableReplayGate:
    """Normal heartbeat admission or same-execution dual durable receipt proof.

    Heartbeat denial is never authority. Only EXECUTION_TERMINAL attempts the
    read-only journal + current Core/membership-authorized canonical read. No
    invocation is reserved and no context/provider is read on this branch.
    """

    def __init__(self, config, *, journal: PostgresEffectJournal, retry_policy: str, contracts: MaestroContracts | None = None):
        self.config, self.journal, self.retry_policy = config, journal, retry_policy
        self.contracts = contracts or MaestroContracts()
        self.heartbeat = MaestroHeartbeatGate(config, contracts=self.contracts)

    def _current(self, admission: MaestroAdmission) -> float:
        try:
            _, claims = verify_jwt(
                admission.execution_capability,
                typ="maestro-execution-capability+jwt",
                keys=load_public_keys(self.config.trusted_public_keys),
            )
            now = time.time()
            if (
                claims != admission.capability_claims
                or not claims["iat"] <= now < claims["exp"]
                or "delivery.complete" not in claims["operations"]
            ):
                raise ValueError
            e = admission.envelope
            if any(
                e[k] != getattr(self.config, k)
                for k in ("organization_id", "workspace_id", "instance_agent_id", "runtime_id", "callback_base_url")
            ) or now >= _epoch(e["deadline_at"]):
                raise ValueError
            remaining = (
                min(admission.remaining_seconds(), claims["exp"] - now, _epoch(e["deadline_at"]) - now) - self.config.safety_margin_seconds
            )
            if remaining <= 0:
                raise ValueError
            return remaining
        except Exception:
            raise MaestroAdmissionError("UNAUTHENTICATED") from None

    async def _journal_result(self, admission):
        from .journal_runtime import invocation_fingerprint

        e = admission.envelope
        scope = JournalScope(
            **{k: e[k] for k in ("organization_id", "workspace_id", "thread_id", "recipient_participant_id", "instance_agent_id")}
        )
        authority = ExecutionAuthority(e["execution_id"], e["runtime_id"], e["lease_fence"], e["deadline_at"])
        return await self.journal.read_confirmed_invocation(
            scope,
            e["delivery_id"],
            e["phase_id"],
            authority,
            context_fingerprint=invocation_fingerprint(e),
            retry_policy=self.retry_policy,
            contracts=self.contracts,
        )

    async def _remote_result(self, admission: MaestroAdmission) -> dict[str, Any]:
        async with asyncio.timeout(min(self._current(admission), self.config.network_timeout_seconds)):
            credential = await resolve_instance_credential(self.config, admission, self.contracts, ("processing:write",))
            self._current(admission)
            e = admission.envelope
            headers = {
                "Authorization": f"Bearer {credential.token}",
                "Maestro-Execution-Capability": admission.execution_capability,
                "Idempotency-Key": f"complete:{e['delivery_id']}:{e['phase_id']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-store",
            }
            body = canonical({"format_version": "maestro.confirmed-result.local.v1", "lease_fence": e["lease_fence"]})
            transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    f"{self.config.callback_base_url}/executions/{e['execution_id']}/confirmed-result",
                    content=body,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        raise MaestroAdmissionError(
                            {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "FORBIDDEN", 409: "LEASE_STALE", 410: "LEASE_STALE"}.get(
                                response.status_code, "DEPENDENCY_UNAVAILABLE"
                            )
                        )
                    if (
                        response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise MaestroAdmissionError()
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isdecimal() or int(length) > 131072):
                        raise MaestroAdmissionError()
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        raw.extend(chunk)
                        if len(raw) > 131072:
                            raise MaestroAdmissionError()
                    result = self.contracts.validate("Public.CallbackResponse", strict_json(bytes(raw)))
                    self._current(admission)
                    if time.time() >= credential.expires_at:
                        raise MaestroAdmissionError("UNAUTHENTICATED")
                    return result

    async def admit(self, admission: MaestroAdmission):
        try:
            return await self.heartbeat.admit(admission)
        except MaestroCallbackError as error:
            if error.code != "EXECUTION_TERMINAL":
                raise
        # Failure in either independent authority is terminal/fail-closed.
        try:
            self._current(admission)
            before = await self._journal_result(admission)
            current = await self._remote_result(admission)
            after = await self._journal_result(admission)
            self._current(admission)
            if canonical(before.response) != canonical(current) or canonical(after.response) != canonical(current):
                raise MaestroAdmissionError("IDEMPOTENCY_CONFLICT")
            return MaestroConfirmedResult(current, confirmed=True)
        except MaestroAdmissionError:
            raise
        except Exception:
            raise MaestroAdmissionError() from None
