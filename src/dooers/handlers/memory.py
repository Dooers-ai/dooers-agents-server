from typing import TYPE_CHECKING, Any, Literal

from dooers.protocol.models import ThreadEvent

if TYPE_CHECKING:
    from dooers.persistence.base import EventOrder, Persistence

HistoryFormat = Literal["openai", "anthropic", "google", "cohere", "voyage"]


class WorkerMemory:
    def __init__(self, thread_id: str, persistence: "Persistence"):
        self._thread_id = thread_id
        self._persistence = persistence

    async def get_history_raw(
        self,
        limit: int = 50,
        order: "EventOrder" = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        return await self._persistence.get_events(
            self._thread_id,
            limit=limit,
            order=order,
            filters=filters,
        )

    async def get_history(
        self,
        limit: int = 50,
        format: HistoryFormat = "openai",
        order: "EventOrder" = "desc",
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        raw_limit = limit * 3
        events = await self.get_history_raw(raw_limit, order=order, filters=filters)

        if order == "desc":
            events.reverse()

        messages: list[dict[str, Any]] = []
        for event in events:
            if event.type != "message" or not event.content:
                continue

            text = " ".join(part.text for part in event.content if hasattr(part, "text"))
            if not text:
                continue

            messages.append(self._format_message(text, event.actor == "user", format))

        if order == "desc":
            return messages[-limit:]
        return messages[:limit]

    def _format_message(
        self,
        text: str,
        is_user: bool,
        format: HistoryFormat,
    ) -> dict[str, Any]:
        match format:
            case "openai" | "voyage":
                return {
                    "role": "user" if is_user else "assistant",
                    "content": text,
                }
            case "anthropic":
                return {
                    "role": "user" if is_user else "assistant",
                    "content": text,
                }
            case "google":
                return {
                    "role": "user" if is_user else "model",
                    "parts": [{"text": text}],
                }
            case "cohere":
                return {
                    "role": "USER" if is_user else "CHATBOT",
                    "message": text,
                }
            case _:
                return {
                    "role": "user" if is_user else "assistant",
                    "content": text,
                }
