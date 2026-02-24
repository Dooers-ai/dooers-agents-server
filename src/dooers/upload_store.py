from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger("workers")

DEFAULT_MAX_SIZE = 25 * 1024 * 1024  # 25MB
DEFAULT_TTL = 300  # 5 minutes
PRUNE_INTERVAL = 60  # seconds


@dataclass
class UploadEntry:
    data: bytes
    mime_type: str
    filename: str
    size_bytes: int
    created_at: float


class UploadStore:
    def __init__(self, max_size: int = DEFAULT_MAX_SIZE, ttl: int = DEFAULT_TTL) -> None:
        self._store: dict[str, UploadEntry] = {}
        self._max_size = max_size
        self._ttl = ttl
        self._prune_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._prune_task is None:
            self._prune_task = asyncio.create_task(self._prune_loop())

    async def stop(self) -> None:
        if self._prune_task is not None:
            self._prune_task.cancel()
            self._prune_task = None

    def store(self, data: bytes, filename: str, mime_type: str) -> str:
        if len(data) > self._max_size:
            raise ValueError(f"File exceeds maximum size of {self._max_size} bytes")

        ref_id = str(uuid.uuid4())
        self._store[ref_id] = UploadEntry(
            data=data,
            mime_type=mime_type,
            filename=filename,
            size_bytes=len(data),
            created_at=time.time(),
        )
        return ref_id

    def consume(self, ref_id: str) -> UploadEntry | None:
        return self._store.pop(ref_id, None)

    def _prune_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.created_at > self._ttl]
        for k in expired:
            del self._store[k]

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_INTERVAL)
            try:
                self._prune_expired()
            except Exception:
                logger.exception("upload store prune failed")
