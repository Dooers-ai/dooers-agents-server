from typing import TYPE_CHECKING, Any, Literal

from dooers.protocol.models import ThreadEvent

if TYPE_CHECKING:
    from dooers.persistence.base import EventOrder, Persistence

# openai: plain string per message (legacy).
# langchain: internal wire parts {type: text|image, url?} for LangChain adapters.
# openai_completions: Chat Completions `messages` — content is str or
#   [{"type":"text","text":...},{"type":"image_url","image_url":{"url":...}}].
# openai_responses: Responses / Agents input items — user content is
#   [{"type":"input_text","text":...},{"type":"input_image","image_url":...}],
#   assistant [{"type":"output_text","text":...}].
HistoryFormat = Literal[
    "openai",
    "anthropic",
    "google",
    "cohere",
    "voyage",
    "langchain",
    "openai_completions",
    "openai_responses",
]


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

            if format in ("langchain", "openai_completions", "openai_responses"):
                parts = self._extract_thread_multimodal_parts(event.content)
                if not parts:
                    continue
                is_user = event.actor == "user"
                if format == "langchain":
                    if len(parts) == 1 and parts[0].get("type") == "text":
                        content_lc: str | list[dict[str, Any]] = parts[0].get("text") or ""
                    else:
                        content_lc = parts
                    messages.append(
                        {
                            "role": "user" if is_user else "assistant",
                            "content": content_lc,
                        }
                    )
                elif format == "openai_completions":
                    messages.append(self._message_openai_completions(parts, is_user))
                else:
                    messages.append(self._message_openai_responses(parts, is_user))
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
            case "langchain" | "openai_completions" | "openai_responses":
                raise ValueError(
                    f"format {format!r} is handled in get_history before _format_message"
                )
            case _:
                return {
                    "role": "user" if is_user else "assistant",
                    "content": text,
                }

    @staticmethod
    def _fetchable_image_url(url: str | None) -> str | None:
        """HTTP(S) URLs the LLM can fetch; excludes gs:// and other non-URL schemes."""
        if not url or not isinstance(url, str):
            return None
        u = url.strip()
        if not u:
            return None
        low = u.lower()
        if low.startswith("https://") or low.startswith("http://"):
            return u
        return None

    @classmethod
    def _merge_adjacent_text_parts(cls, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not parts:
            return []
        out: list[dict[str, Any]] = []
        for p in parts:
            if p.get("type") == "text" and out and out[-1].get("type") == "text":
                a = (out[-1].get("text") or "").strip()
                b = (p.get("text") or "").strip()
                out[-1]["text"] = f"{a}\n{b}".strip() if a and b else (a or b)
            else:
                out.append(dict(p))
        return out

    def _message_openai_completions(
        self, parts: list[dict[str, Any]], is_user: bool
    ) -> dict[str, Any]:
        """Chat Completions API: https://platform.openai.com/docs/api-reference/chat/create"""
        if not is_user:
            texts = [p["text"] for p in parts if p.get("type") == "text"]
            body = "\n".join(t for t in texts if (t or "").strip()).strip()
            return {"role": "assistant", "content": body}

        blocks: list[dict[str, Any]] = []
        for p in parts:
            if p.get("type") == "text":
                t = (p.get("text") or "").strip()
                if t:
                    blocks.append({"type": "text", "text": t})
            elif p.get("type") == "image":
                fname = p.get("filename") or "image"
                u = self._fetchable_image_url(p.get("url"))
                if u:
                    blocks.append({"type": "image_url", "image_url": {"url": u}})
                else:
                    blocks.append({"type": "text", "text": f"[image: {fname}]"})
            elif p.get("type") == "audio":
                fname = p.get("filename") or "audio"
                dur = p.get("duration")
                dur_s = f" ({dur}s)" if dur is not None else ""
                u = self._fetchable_image_url(p.get("url"))
                if u:
                    blocks.append({"type": "text", "text": f"[audio: {fname}{dur_s}] {u}"})
                else:
                    blocks.append({"type": "text", "text": f"[audio: {fname}{dur_s}]"})
            elif p.get("type") == "document":
                fname = p.get("filename") or "document"
                u = self._fetchable_image_url(p.get("url"))
                if u:
                    blocks.append({"type": "text", "text": f"[document: {fname}] {u}"})
                else:
                    blocks.append({"type": "text", "text": f"[document: {fname}]"})

        if len(blocks) == 1 and blocks[0].get("type") == "text":
            return {"role": "user", "content": blocks[0]["text"]}
        return {"role": "user", "content": blocks}

    def _message_openai_responses(
        self, parts: list[dict[str, Any]], is_user: bool
    ) -> dict[str, Any]:
        """Responses API input items (user) / output_text (assistant)."""
        if not is_user:
            texts = [(p.get("text") or "").strip() for p in parts if p.get("type") == "text"]
            body = "\n".join(t for t in texts if t).strip()
            return {"role": "assistant", "content": [{"type": "output_text", "text": body}]}

        items: list[dict[str, Any]] = []
        for p in parts:
            if p.get("type") == "text":
                t = (p.get("text") or "").strip()
                if t:
                    items.append({"type": "input_text", "text": t})
            elif p.get("type") == "image":
                fname = p.get("filename") or "image"
                u = self._fetchable_image_url(p.get("url"))
                if u:
                    items.append({"type": "input_image", "image_url": u})
                else:
                    items.append({"type": "input_text", "text": f"[image: {fname}]"})
            elif p.get("type") == "audio":
                fname = p.get("filename") or "audio"
                dur = p.get("duration")
                dur_s = f" ({dur}s)" if dur is not None else ""
                u = self._fetchable_image_url(p.get("url"))
                if u:
                    items.append({"type": "input_text", "text": f"[audio: {fname}{dur_s}] {u}"})
                else:
                    items.append({"type": "input_text", "text": f"[audio: {fname}{dur_s}]"})
            elif p.get("type") == "document":
                fname = p.get("filename") or "document"
                u = self._fetchable_image_url(p.get("url"))
                if u:
                    items.append({"type": "input_text", "text": f"[document: {fname}] {u}"})
                else:
                    items.append({"type": "input_text", "text": f"[document: {fname}]"})
        return {"role": "user", "content": items}

    @classmethod
    def _extract_thread_multimodal_parts(cls, content: list) -> list[dict[str, Any]]:
        """Ordered wire parts: text, images, audio, documents (HTTP(S) url when persisted)."""
        raw: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict):
                p_type = part.get("type")
                if p_type == "text" and part.get("text"):
                    raw.append({"type": "text", "text": part["text"]})
                elif p_type == "image":
                    fname = part.get("filename") or "image"
                    mime = part.get("mime_type")
                    url = part.get("url")
                    u = cls._fetchable_image_url(url) if isinstance(url, str) else None
                    if u:
                        raw.append(
                            {
                                "type": "image",
                                "url": u,
                                "mime_type": mime,
                                "filename": fname,
                            }
                        )
                    else:
                        raw.append({"type": "text", "text": f"[image: {fname}]"})
                elif p_type == "audio":
                    fname = part.get("filename") or "audio"
                    mime = part.get("mime_type") or ""
                    dur = part.get("duration")
                    url = part.get("url")
                    u = cls._fetchable_image_url(url) if isinstance(url, str) else None
                    if u:
                        raw.append(
                            {
                                "type": "audio",
                                "url": u,
                                "mime_type": mime or None,
                                "filename": fname,
                                "duration": dur,
                            }
                        )
                    else:
                        dur_str = f", {dur}s" if dur else ""
                        raw.append(
                            {
                                "type": "text",
                                "text": f"[audio: {fname}{', ' + mime if mime else ''}{dur_str}]",
                            }
                        )
                elif p_type == "document":
                    fname = part.get("filename") or "document"
                    mime = part.get("mime_type")
                    sz = part.get("size_bytes")
                    url = part.get("url")
                    u = cls._fetchable_image_url(url) if isinstance(url, str) else None
                    if u:
                        raw.append(
                            {
                                "type": "document",
                                "url": u,
                                "mime_type": mime,
                                "filename": fname,
                                "size_bytes": sz,
                            }
                        )
                    else:
                        raw.append({"type": "text", "text": f"[document: {fname}]"})
            else:
                if hasattr(part, "text") and part.text:
                    raw.append({"type": "text", "text": part.text})
                elif hasattr(part, "type"):
                    if part.type == "image":
                        fname = getattr(part, "filename", None) or "image"
                        mime = getattr(part, "mime_type", None)
                        url = getattr(part, "url", None)
                        u = cls._fetchable_image_url(url) if isinstance(url, str) else None
                        if u:
                            raw.append(
                                {
                                    "type": "image",
                                    "url": u,
                                    "mime_type": mime,
                                    "filename": fname,
                                }
                            )
                        else:
                            raw.append({"type": "text", "text": f"[image: {fname}]"})
                    elif part.type == "audio":
                        fname = getattr(part, "filename", None) or "audio"
                        mime = getattr(part, "mime_type", None) or ""
                        dur = getattr(part, "duration", None)
                        url = getattr(part, "url", None)
                        u = cls._fetchable_image_url(url) if isinstance(url, str) else None
                        if u:
                            raw.append(
                                {
                                    "type": "audio",
                                    "url": u,
                                    "mime_type": mime or None,
                                    "filename": fname,
                                    "duration": dur,
                                }
                            )
                        else:
                            dur_str = f", {dur}s" if dur else ""
                            raw.append(
                                {
                                    "type": "text",
                                    "text": f"[audio: {fname}{', ' + mime if mime else ''}{dur_str}]",
                                }
                            )
                    elif part.type == "document":
                        fname = getattr(part, "filename", None) or "document"
                        mime = getattr(part, "mime_type", None)
                        sz = getattr(part, "size_bytes", None)
                        url = getattr(part, "url", None)
                        u = cls._fetchable_image_url(url) if isinstance(url, str) else None
                        if u:
                            raw.append(
                                {
                                    "type": "document",
                                    "url": u,
                                    "mime_type": mime,
                                    "filename": fname,
                                    "size_bytes": sz,
                                }
                            )
                        else:
                            raw.append({"type": "text", "text": f"[document: {fname}]"})
        merged = cls._merge_adjacent_text_parts(raw)
        return [
            p
            for p in merged
            if not (p.get("type") == "text" and not (str(p.get("text") or "")).strip())
        ]
