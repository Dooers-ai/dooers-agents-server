from typing import TYPE_CHECKING, Any, Literal

from dooers.protocol.models import ThreadEvent

if TYPE_CHECKING:
    from dooers.persistence.base import EventOrder, Persistence

HistoryFormat = Literal["openai", "anthropic", "google", "cohere", "voyage"]


class AgentMemory:
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

            text = self._extract_text_with_media(event.content)
            if not text:
                continue

            messages.append(self._format_message(text, event.actor == "user", format))

        if order == "desc":
            return messages[-limit:]
        return messages[:limit]

    @staticmethod
    def _extract_text_with_media(content: list) -> str:
        """Extract text from content parts, adding indicators for media parts."""
        segments: list[str] = []
        for part in content:
            # Handle both Pydantic models and raw dicts (from DB deserialization)
            if isinstance(part, dict):
                p_type = part.get("type")
                if p_type == "text" and part.get("text"):
                    segments.append(part["text"])
                elif p_type == "audio":
                    fname = part.get("filename") or "audio"
                    mime = part.get("mime_type") or ""
                    dur = part.get("duration")
                    dur_str = f", {dur}s" if dur else ""
                    segments.append(f"[audio: {fname}{', ' + mime if mime else ''}{dur_str}]")
                elif p_type == "image":
                    fname = part.get("filename") or "image"
                    segments.append(f"[image: {fname}]")
                elif p_type == "document":
                    fname = part.get("filename") or "document"
                    segments.append(f"[document: {fname}]")
            else:
                if hasattr(part, "text") and part.text:
                    segments.append(part.text)
                elif hasattr(part, "type"):
                    if part.type == "audio":
                        fname = getattr(part, "filename", None) or "audio"
                        mime = getattr(part, "mime_type", None) or ""
                        dur = getattr(part, "duration", None)
                        dur_str = f", {dur}s" if dur else ""
                        segments.append(f"[audio: {fname}{', ' + mime if mime else ''}{dur_str}]")
                    elif part.type == "image":
                        fname = getattr(part, "filename", None) or "image"
                        segments.append(f"[image: {fname}]")
                    elif part.type == "document":
                        fname = getattr(part, "filename", None) or "document"
                        segments.append(f"[document: {fname}]")
        return " ".join(segments)

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
