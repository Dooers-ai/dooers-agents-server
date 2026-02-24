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

    async def register(self, worker_id: str, ws: WebSocketProtocol) -> None:
        async with self._lock:
            if worker_id not in self._connections:
                self._connections[worker_id] = set()
            self._connections[worker_id].add(ws)

    async def unregister(self, worker_id: str, ws: WebSocketProtocol) -> None:
        async with self._lock:
            if worker_id in self._connections:
                self._connections[worker_id].discard(ws)
                if not self._connections[worker_id]:
                    del self._connections[worker_id]

    def get_connections(self, worker_id: str) -> set[WebSocketProtocol]:
        return self._connections.get(worker_id, set()).copy()

    def get_connection_count(self, worker_id: str) -> int:
        return len(self._connections.get(worker_id, set()))

    async def broadcast(
        self,
        worker_id: str,
        message: str,
    ) -> int:
        connections = self.get_connections(worker_id)
        if not connections:
            return 0

        results = await asyncio.gather(
            *[self._safe_send(ws, message) for ws in connections],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def broadcast_except(
        self,
        worker_id: str,
        exclude: WebSocketProtocol,
        message: str,
    ) -> int:

        connections = self.get_connections(worker_id)
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
