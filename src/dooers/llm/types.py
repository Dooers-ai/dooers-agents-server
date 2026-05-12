"""Wire ids for :func:`~dooers.llm.user_input.format_user_input` (same as accepted string literals)."""

from __future__ import annotations

from enum import StrEnum


class LlmWireFormat(StrEnum):
    """Optional typed ``api_provider``; creators may instead pass the ``.value`` string."""

    OPENAI_RESPONSES = "openai_responses"
    OPENAI_COMPLETIONS = "openai_completions"
    GEMINI = "gemini"
    CLAUDE = "claude"
