"""Saída incremental local: preview descartável, final no sink durável existente."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from typing import TYPE_CHECKING

from .callbacks import MaestroCallbackError

if TYPE_CHECKING:
    from .sink import MaestroEventSink


class MaestroTextStream:
    """Handle confinado à task do handler, com backpressure e sem timer próprio.

    delta coalesce até 16 KiB; produção contínua faz flush após 50 ms. flush/finish
    aguardam a janela compartilhada da execução. Texto integral tem o limite do
    output durável; nunca se reconstrói final a partir de ACKs de chunks.
    """

    def __init__(self, sink: MaestroEventSink, ordinal: int) -> None:
        self._sink, self.ordinal = sink, ordinal
        self.output_id = f"output:{ordinal}"
        self._text = ""
        self._pending = ""
        self._bytes = 0
        self._index = 0
        self._preview = True
        self._finishing = False
        self._receipt = None
        self.closed = False

    def _active(self) -> None:
        self._sink._active()
        if self.closed or self._finishing:
            raise MaestroCallbackError("EXECUTION_TERMINAL")

    def close(self) -> None:
        self.closed = True
        self._text = self._pending = ""
        self._bytes = 0

    async def delta(self, text: str) -> None:
        self._active()
        if not isinstance(text, str) or not text:
            raise MaestroCallbackError("INVALID_REQUEST")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            raise MaestroCallbackError("INVALID_REQUEST") from None
        if self._bytes + size > self._sink._client.config.max_output_bytes:
            raise MaestroCallbackError("PAYLOAD_TOO_LARGE")
        if sum(stream._bytes for stream in self._sink._streams) + size > 262144:
            raise MaestroCallbackError("QUOTA_EXCEEDED")
        self._text += text
        self._bytes += size
        if not self._preview:
            return
        # UTF-8 boundaries imply the UTF-16 schema limit as well. Never place
        # the whole caller string in an unbounded pending queue.
        pending_bytes = len(self._pending.encode("utf-8"))
        for character in text:
            character_bytes = len(character.encode("utf-8"))
            if pending_bytes + character_bytes > 16384:
                await self.flush()
                pending_bytes = 0
            if not self._preview:
                return
            self._pending += character
            pending_bytes += character_bytes
        if time.monotonic() - self._sink._stream_last_sent >= 0.05:
            await self.flush()

    async def flush(self) -> None:
        self._active()
        if not self._pending or not self._preview:
            return
        if self._index >= 256:
            self._preview = False
            self._pending = ""
            return
        delay = self._sink._stream_last_sent + 0.05 - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._active()
        delta = self._pending
        # Assign an index only to a coalesced request. Unknown ACK is not a
        # reason to replay generation or turn a partial into a final message.
        self._sink._stream_last_sent = time.monotonic()
        try:
            await self._sink._client.stream(self.output_id, self._index, delta)
        except MaestroCallbackError as error:
            if not error.unknown or error.code != "DEPENDENCY_UNAVAILABLE":
                self.close()
                raise
            self._preview = False
        self._index += 1
        self._pending = ""

    async def finish(self) -> dict:
        self._sink._active()
        if self._receipt is not None:
            return deepcopy(self._receipt)
        if self.closed:
            raise MaestroCallbackError("EXECUTION_TERMINAL")
        if not self._finishing:
            # Validate before flushing so empty/whitespace cannot fake a final.
            intent = self._sink.prepare_text(self._text, self.ordinal)
            await self.flush()
            self._sink._intents[self.ordinal] = intent
            self._finishing = True
        intent = self._sink._intents[self.ordinal]
        assert intent is not None
        self._receipt = await self._sink.persist_with_retry(intent)
        self.close()
        return deepcopy(self._receipt)
