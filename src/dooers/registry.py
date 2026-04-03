from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    pass


class WebSocketProtocol(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def receive_text(self) -> str: ...


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocketProtocol]] = {}
        self._lock = asyncio.Lock()

    async def register(self, agent_id: str, ws: WebSocketProtocol) -> None:
        async with self._lock:
            if agent_id not in self._connections:
                self._connections[agent_id] = set()
            self._connections[agent_id].add(ws)

    async def unregister(self, agent_id: str, ws: WebSocketProtocol) -> None:
        async with self._lock:
            if agent_id in self._connections:
                self._connections[agent_id].discard(ws)
                if not self._connections[agent_id]:
                    del self._connections[agent_id]

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
