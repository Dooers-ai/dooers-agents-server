from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from dooers.agents.server.protocol.models import User


class WebSocketProtocol(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def receive_text(self) -> str: ...


@dataclass(frozen=True)
class ConnectionSession:
    """Identity used to personalize frames that differ per socket (thread access)."""

    user: User
    workspace_id: str


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocketProtocol]] = {}
        self._sessions: dict[WebSocketProtocol, ConnectionSession] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        agent_id: str,
        ws: WebSocketProtocol,
        *,
        user: User | None = None,
        workspace_id: str = "",
    ) -> None:
        async with self._lock:
            if agent_id not in self._connections:
                self._connections[agent_id] = set()
            self._connections[agent_id].add(ws)
            if user is not None:
                self._sessions[ws] = ConnectionSession(user=user, workspace_id=workspace_id)

    async def unregister(self, agent_id: str, ws: WebSocketProtocol) -> None:
        async with self._lock:
            self._sessions.pop(ws, None)
            if agent_id in self._connections:
                self._connections[agent_id].discard(ws)
                if not self._connections[agent_id]:
                    del self._connections[agent_id]

    def session_for(self, ws: WebSocketProtocol) -> ConnectionSession | None:
        return self._sessions.get(ws)

    def get_connections(self, agent_id: str) -> set[WebSocketProtocol]:
        return self._connections.get(agent_id, set()).copy()

    def get_connection_count(self, agent_id: str) -> int:
        return len(self._connections.get(agent_id, set()))

    async def broadcast(
        self,
        agent_id: str,
        message: str,
    ) -> int:
        connections = self.get_connections(agent_id)
        if not connections:
            return 0

        results = await asyncio.gather(
            *[self._safe_send(ws, message) for ws in connections],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def broadcast_except(
        self,
        agent_id: str,
        exclude: WebSocketProtocol,
        message: str,
    ) -> int:

        connections = self.get_connections(agent_id)
        connections.discard(exclude)
        if not connections:
            return 0

        results = await asyncio.gather(
            *[self._safe_send(ws, message) for ws in connections],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _safe_send(self, ws: WebSocketProtocol, message: str) -> bool:
        try:
            await ws.send_text(message)
            return True
        except Exception:
            return False
