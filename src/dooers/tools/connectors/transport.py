"""HTTP transport to ``dooers-connector-service``.

Auth: core service token (audience ``connector-service``) minted with the agent runtime API key.
Agent, workspace, thread and channel come from the active turn, never from the LLM.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from dooers.agents.server.observability.service_token import (
    RUNTIME_API_KEY_SECRET_NAME,
    ServiceTokenClient,
    core_base_url,
    resolve_runtime_api_key,
)
from dooers.tools.connectors import presence
from dooers.tools.connectors.errors import ConnectorError
from dooers.tools.connectors.runtime import TurnContext, current_turn

_AUDIENCE = "connector-service"
_SCOPES = ("connectors:read", "connectors:execute")
_token_client: ServiceTokenClient | None = None


def reset_transport_state() -> None:
    """Drop module-level caches (service token client, connector_session cache). For tests."""
    global _token_client
    _token_client = None
    presence.reset_presence_cache()


def _service_token_client() -> ServiceTokenClient:
    global _token_client
    if _token_client is None:
        _token_client = ServiceTokenClient(core_base_url=core_base_url())
    return _token_client


def _base_url() -> str:
    value = (os.environ.get("DOOERS_CONNECTOR_SERVICE_URL") or "").strip().rstrip("/")
    if not value:
        raise ConnectorError("DOOERS_CONNECTOR_SERVICE_URL não configurada")
    return value


def _require_turn() -> TurnContext:
    turn = current_turn()
    if turn is None:
        raise ConnectorError("connectors precisa de um turno ativo do handler")
    return turn


async def _service_token(turn: TurnContext) -> str:
    runtime_api_key = await resolve_runtime_api_key(turn.persistence, turn.agent_id)
    if not runtime_api_key:
        raise ConnectorError(
            f"Chave de runtime ausente: defina AGENT_SEED_SECRET ou aguarde {RUNTIME_API_KEY_SECRET_NAME} via settings.seed",
            status="denied",
        )
    token = await _service_token_client().get_token(
        agent_id=turn.agent_id,
        workspace_id=turn.workspace_id,
        runtime_api_key=runtime_api_key,
        audience=_AUDIENCE,
        scopes=_SCOPES,
    )
    if not token:
        raise ConnectorError("Não foi possível obter o token de serviço para os conectores", status="denied")
    return token


class ConnectorTransport:
    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 60.0,
        on_behalf_of: str | None = None,
    ) -> dict[str, Any]:
        base = _base_url()
        turn = _require_turn()
        token = await _service_token(turn)
        # The payload goes first: the turn's context, the presence session and the person are never overridable.
        body = {
            **payload,
            "context": {
                "agent_id": turn.agent_id,
                "workspace_id": turn.workspace_id,
                "thread_id": turn.thread_id,
                "channel": turn.channel,
            },
            "connector_session": await presence.connector_session(turn),
            "on_behalf_of": on_behalf_of,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{base}{path}",
                    content=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Authorization": f"Bearer {token}",
                    },
                )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Falha de rede ao chamar o serviço de conectores: {type(exc).__name__}", status="provider_error") from exc
        if response.status_code in (401, 403):
            raise ConnectorError(f"Serviço de conectores recusou {path} ({response.status_code})", status="denied")
        if not response.is_success:
            raise ConnectorError(
                f"Serviço de conectores respondeu {response.status_code} em {path}: {response.text[:300]}",
                status="provider_error",
            )
        try:
            return response.json() if response.content else {}
        except ValueError as exc:
            raise ConnectorError("Resposta inválida do serviço de conectores", status="provider_error") from exc
