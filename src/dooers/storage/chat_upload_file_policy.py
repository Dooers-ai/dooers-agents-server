"""Infer attachment kind and enforce :class:`~dooers.config.AgentConfig` allowlist for HTTP chat uploads."""

from __future__ import annotations

import mimetypes
import os
from typing import Any

from dooers.handlers.content_policy import normalize_allowed_content_types


def infer_chat_upload_kind(mime_type: str, filename: str) -> str | None:
    """Map upload to pipeline kind: image | audio | document; not ``text``."""
    m = (mime_type or "").strip().lower()
    if m.startswith("image/") and m != "image/*":
        return "image"
    if m.startswith("audio/") and m != "audio/*":
        return "audio"

    guess, _ = mimetypes.guess_type(filename)
    if guess:
        g = guess.lower()
        if g.startswith("image/"):
            return "image"
        if g.startswith("audio/"):
            return "audio"

    ext = os.path.splitext(filename or "")[1].lower()
    doc_exts = frozenset(
        {
            ".pdf",
            ".csv",
            ".tsv",
            ".json",
            ".txt",
            ".md",
            ".html",
            ".htm",
            ".xml",
            ".docx",
            ".xlsx",
            ".xls",
            ".pptx",
        }
    )
    if ext in doc_exts:
        return "document"
    if m in (
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        return "document"
    return None


def enforce_allowed_chat_file_kind(
    *,
    filename: str,
    mime_type: str,
    allowed_raw: Any,
) -> None:
    """Raise ``ValueError`` when an allowlist is active and the file kind is missing or disallowed."""
    allowed = normalize_allowed_content_types(allowed_raw)
    if allowed is None:
        return
    kind = infer_chat_upload_kind(mime_type, filename)
    if kind is None:
        raise ValueError(
            "Tipo de ficheiro não reconhecido como imagem, áudio ou documento.",
        )
    if kind not in allowed:
        allowed_h = ", ".join(sorted(allowed))
        raise ValueError(
            f"Este agente não aceita anexos do tipo «{kind}» neste canal. Permitidos: {allowed_h}.",
        )


PERSIST_CHAT_ATTACHMENTS_FIELD = "persist_chat_attachments"
