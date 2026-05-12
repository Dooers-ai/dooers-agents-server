"""Map :class:`~dooers.handlers.incoming.AgentIncoming` to API wire content for the user turn."""

from __future__ import annotations

import base64
import logging
from typing import Any

from dooers.handlers.incoming import AgentIncoming
from dooers.llm.types import LlmWireFormat
from dooers.protocol.models import AudioPart, DocumentPart, ImagePart, TextPart

logger = logging.getLogger("dooers.llm")

MAX_IMAGES_PER_MESSAGE = 10


def _coerce_api_provider(api_provider: str | LlmWireFormat) -> LlmWireFormat:
    """Accept enum or documented string; ``-`` and case are normalized. Raises ``ValueError`` if unknown."""
    if isinstance(api_provider, LlmWireFormat):
        return api_provider
    if not isinstance(api_provider, str) or not api_provider.strip():
        raise ValueError("api_provider must be a non-empty string (see docs) or LlmWireFormat.")
    raw = api_provider.strip().lower().replace("-", "_")
    for fmt in LlmWireFormat:
        if fmt.value == raw:
            return fmt
    valid = ", ".join(repr(m.value) for m in LlmWireFormat)
    raise ValueError(f"Invalid api_provider {api_provider!r}; expected one of: {valid}")


def _public_https_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    low = u.lower()
    if low.startswith("https://") or low.startswith("http://"):
        return u
    return None


def _mime_for_image(mime: str | None) -> str:
    m = (mime or "").strip().lower()
    if m.startswith("image/") and m != "image/*":
        return m
    return "image/jpeg"


def _input_text_part(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text.strip()}


def _input_image_part(image_url: str) -> dict[str, Any]:
    return {"type": "input_image", "image_url": image_url.strip(), "detail": "auto"}


def _audio_to_input_text(part: AudioPart) -> dict[str, Any]:
    fname = part.filename or "audio"
    dur = part.duration
    dur_s = f" ({dur}s)" if dur is not None else ""
    url = _public_https_url(part.url)
    if url:
        return _input_text_part(f"[audio: {fname}{dur_s}] {url}")
    return _input_text_part(f"[audio: {fname}{dur_s}]")


def _document_to_input_text(part: DocumentPart) -> dict[str, Any]:
    fname = part.filename or "document"
    url = _public_https_url(part.url)
    if url:
        return _input_text_part(f"[document: {fname}] {url}")
    return _input_text_part(f"[document: {fname}]")


def _image_has_payload(img: ImagePart) -> bool:
    return bool(img.data or _public_https_url(img.url))


def _append_one_input_image(
    parts: list[dict[str, Any]],
    img: ImagePart,
) -> bool:
    """Append OpenAI Responses ``input_image`` if bytes or HTTP(S) URL are present."""
    data = img.data or b""
    mt = _mime_for_image(img.mime_type)
    fetch_url = _public_https_url(img.url)

    if fetch_url:
        parts.append(_input_image_part(fetch_url))
        return True
    if not data:
        return False
    b64 = base64.b64encode(data).decode("ascii")
    parts.append(_input_image_part(f"data:{mt};base64,{b64}"))
    return True


def _ordered_openai_responses_parts(
    incoming: AgentIncoming,
    *,
    strict: bool,
    wire: LlmWireFormat,
) -> list[dict[str, Any]]:
    """OpenAI Responses API user content: ``input_text`` / ``input_image`` items in order."""
    _ = wire  # reserved when provider-specific branching is added
    parts: list[dict[str, Any]] = []
    m = (incoming.message or "").strip()
    if m:
        parts.append(_input_text_part(m))

    images_seen = 0
    for p in incoming.content or []:
        if isinstance(p, TextPart):
            t = (p.text or "").strip()
            if t:
                parts.append(_input_text_part(t))
        elif isinstance(p, ImagePart):
            if images_seen >= MAX_IMAGES_PER_MESSAGE:
                logger.warning("Max images per message (%s) reached; skipping further images", MAX_IMAGES_PER_MESSAGE)
                continue
            if not _image_has_payload(p):
                if strict:
                    raise ValueError("An image part has no usable data or HTTP(S) URL; cannot build input_image.")
                logger.warning("Skipping image part with no bytes and no HTTP(S) URL")
                continue
            appended = _append_one_input_image(parts, p)
            if appended:
                images_seen += 1
            elif strict:
                raise ValueError("Cannot build input_image for this image part.")
        elif isinstance(p, AudioPart):
            parts.append(_audio_to_input_text(p))
        elif isinstance(p, DocumentPart):
            parts.append(_document_to_input_text(p))
        elif strict:
            raise ValueError(f"Unsupported content type for formatting: {type(p).__name__}.")
        else:
            logger.warning("[dooers.llm] skipping unrecognized content part: %s", type(p).__name__)

    return parts


def _collapse_text_only(parts: list[dict[str, Any]]) -> str:
    texts = [p["text"] for p in parts if p.get("type") == "input_text" and (p.get("text") or "").strip()]
    body = "\n".join(texts).strip()
    return body if body else "(sem texto)"


def _has_input_image(parts: list[dict[str, Any]]) -> bool:
    return any(p.get("type") == "input_image" for p in parts)


def _openai_agents_runner_message_item(
    *,
    role: str,
    content: str | list[dict[str, Any]],
) -> dict[str, Any]:
    """Wrap OpenAI Responses / Agents ``EasyInputMessageParam`` for ``Runner.run``."""
    allowed = ("user", "assistant", "system", "developer")
    r = role if role in allowed else "user"
    if isinstance(content, str):
        text = content.strip() or "(sem conteúdo)"
        return {"type": "message", "role": r, "content": text}
    if not content:
        return {"type": "message", "role": r, "content": "(sem conteúdo)"}
    if len(content) == 1 and content[0].get("type") == "input_text":
        return {"type": "message", "role": r, "content": content[0]["text"]}
    return {"type": "message", "role": r, "content": content}


def format_user_input(
    incoming: AgentIncoming,
    api_provider: str | LlmWireFormat,
    strict: bool = True,
    *,
    role: str = "user",
) -> dict[str, Any]:
    """Turn ``incoming`` into one OpenAI Agents / Responses **message item** for ``Runner.run`` input.

    ``api_provider`` must be one of the documented wire ids (strings, case-insensitive, ``-`` or ``_``):
    ``\"openai_responses\"``, ``\"openai_completions\"``, ``\"gemini\"``, ``\"claude\"``. Invalid values raise
    :class:`ValueError`. Creators choose this explicitly; there is **no** inference from agent settings.

    Returns ``{\"type\": \"message\", \"role\": ..., \"content\": ...}`` where ``content`` is either a
    string or a list of ``input_text`` / ``input_image`` parts.

    Raises :class:`ValueError` when the provider string is unknown, when ``strict`` is true and a part
    cannot be encoded, or when an unsupported part type appears in ``incoming.content``.
    """
    wire = _coerce_api_provider(api_provider)
    logger.debug("format_user_input api_provider=%s", wire.value)

    parts = _ordered_openai_responses_parts(incoming, strict=strict, wire=wire)

    if not parts:
        inner: str | list[dict[str, Any]] = "(sem texto)"
    elif not _has_input_image(parts):
        inner = _collapse_text_only(parts)
    else:
        if not any(p.get("type") == "input_text" for p in parts):
            parts.insert(
                0,
                _input_text_part("Responda com base na(s) imagem(ns) enviada(s) pelo utilizador."),
            )
        inner = parts

    return _openai_agents_runner_message_item(role=role, content=inner)
