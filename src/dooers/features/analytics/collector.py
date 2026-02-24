from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from .models import AnalyticsBatch, AnalyticsEvent, AnalyticsEventPayload

if TYPE_CHECKING:
    from dooers.registry import ConnectionRegistry

logger = logging.getLogger(__name__)


class AnalyticsCollector:
    def __init__(
        self,
        webhook_url: str,
        registry: ConnectionRegistry,
        subscriptions: dict[str, set[str]],
        batch_size: int = 10,
        flush_interval: float = 5.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._registry = registry
        self._subscriptions = subscriptions
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: list[AnalyticsEventPayload] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        try:
            await self._flush()
        finally:
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None

    async def track(
        self,
        event: str,
        worker_id: str,
        thread_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = AnalyticsEventPayload(
            event=event,
            timestamp=datetime.now(UTC),
            worker_id=worker_id,
            thread_id=thread_id,
            user_id=user_id,
            run_id=run_id,
            event_id=event_id,
            data=data,
        )

        await self._broadcast(worker_id, payload)

        async with self._lock:
            self._buffer.append(payload)
            if len(self._buffer) >= self._batch_size:
                await self._flush_locked()

    async def feedback(
        self,
        feedback_type: str,
        target_type: str,
        target_id: str,
        worker_id: str,
        thread_id: str | None = None,
        user_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        event = AnalyticsEvent.FEEDBACK_LIKE if feedback_type == "like" else AnalyticsEvent.FEEDBACK_DISLIKE
        await self.track(
            event=event.value,
            worker_id=worker_id,
            thread_id=thread_id,
            user_id=user_id,
            data={
                "target_type": target_type,
                "target_id": target_id,
                "reason": reason,
            },
        )

    async def _broadcast(self, worker_id: str, payload: AnalyticsEventPayload) -> None:
        from dooers.protocol.frames import S2C_AnalyticsEvent

        subscriber_ws_ids = self._subscriptions.get(worker_id, set())
        if not subscriber_ws_ids:
            logger.debug(
                "[workers] analytics broadcast skipped — no subscribers for worker %s (subscriptions: %s)",
                worker_id,
                list(self._subscriptions.keys()),
            )
            return

        message = S2C_AnalyticsEvent(
            id=str(uuid4()),
            payload=payload,
        )
        message_json = message.model_dump_json()

        connections = self._registry.get_connections(worker_id)
        logger.debug(
            "[workers] analytics broadcast: event=%s, worker=%s, subscribers=%d, connections=%d",
            payload.event,
            worker_id,
            len(subscriber_ws_ids),
            len(connections),
        )
        sent = 0
        for ws in connections:
            try:
                await ws.send_text(message_json)
                sent += 1
            except Exception as e:
                logger.warning("[workers] failed to send analytics event to subscriber: %s", e)
        logger.debug("[workers] analytics broadcast sent to %d/%d connections", sent, len(connections))

    async def _flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return

        events_by_worker: dict[str, list[AnalyticsEventPayload]] = {}
        for event in self._buffer:
            if event.worker_id not in events_by_worker:
                events_by_worker[event.worker_id] = []
            events_by_worker[event.worker_id].append(event)

        self._buffer.clear()

        for worker_id, events in events_by_worker.items():
            batch = AnalyticsBatch(
                batch_id=str(uuid4()),
                worker_id=worker_id,
                events=events,
                sent_at=datetime.now(UTC),
            )
            await self._send_to_webhook(batch)

    async def _send_to_webhook(self, batch: AnalyticsBatch) -> None:
        if not self._http_client:
            return

        try:
            response = await self._http_client.post(
                self._webhook_url,
                json=batch.model_dump(mode="json"),
                headers={"Content-Type": "application/json"},
            )
            if response.status_code >= 400:
                logger.warning(
                    "[workers] analytics webhook returned %d: %s",
                    response.status_code,
                    response.text[:200],
                )
        except httpx.RequestError as e:
            logger.warning("[workers] failed to send analytics batch: %s", e)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("[workers] error in analytics flush loop: %s", e)
