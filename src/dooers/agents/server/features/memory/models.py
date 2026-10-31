"""Long-term memory models: what an agent remembers about a user or a workspace.

Thread history (``AgentMemory``) is short-term and lives inside one conversation. These
records outlive conversations and are scoped by the runtime — never by model arguments:

* ``user`` scope — private to one person inside one workspace (``workspace_id`` may be ``""``
  for personal 1:1 chats);
* ``workspace`` scope — shared facts every member of the workspace benefits from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MemoryScope = Literal["user", "workspace"]
MemoryKind = Literal["fact", "preference", "instruction", "context", "note"]

MEMORY_SCOPES: tuple[str, ...] = ("user", "workspace")
MEMORY_KINDS: tuple[str, ...] = ("fact", "preference", "instruction", "context", "note")

#: Hard bounds that keep a single memory a memory (not a document dump).
MEMORY_MAX_CONTENT_CHARS = 2000
MEMORY_MAX_TAGS = 12
MEMORY_MAX_TAG_CHARS = 40
PREFERENCES_MAX_CHARS = 4000


class MemoryRecord(BaseModel):
    id: str
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    #: Owner for ``user`` scope; ``""`` for ``workspace`` scope.
    user_id: str = ""
    scope: MemoryScope = "user"
    kind: MemoryKind = "fact"
    content: str
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    source_thread_id: str | None = None
    source_event_id: str | None = None
    #: ``assistant`` when saved by the agent, ``user:<id>`` when written in the UI.
    created_by: str | None = None
    use_count: int = 0
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    #: Search relevance (not persisted).
    score: float | None = None


class MemoryPreferences(BaseModel):
    """Per-user, per-agent switches and free-form instructions (ChatGPT-style custom instructions)."""

    agent_id: str
    organization_id: str = ""
    user_id: str
    enabled: bool = True
    #: How the agent should answer ("be concise", "always cite sources"...).
    custom_instructions: str = ""
    #: What the user wants the agent to know about them.
    about_me: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True)
class MemoryOwner:
    """Who the memory belongs to — always built from trusted runtime context."""

    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user_id: str = ""

    @property
    def is_anonymous(self) -> bool:
        uid = (self.user_id or "").strip()
        return not uid or uid.startswith("guest:")

    def scope_key(self, scope: str) -> tuple[str, str, str, str]:
        """(agent, org, workspace, user) tuple a row of ``scope`` must match."""
        if scope == "workspace":
            return (self.agent_id, self.organization_id, self.workspace_id, "")
        return (self.agent_id, self.organization_id, self.workspace_id, self.user_id)


def normalize_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in tags or []:
        tag = " ".join(str(raw or "").strip().lower().split())[:MEMORY_MAX_TAG_CHARS]
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= MEMORY_MAX_TAGS:
            break
    return out


def normalize_content(content: str) -> str:
    text = " ".join(str(content or "").split())
    if not text:
        raise ValueError("memory content is empty")
    if len(text) > MEMORY_MAX_CONTENT_CHARS:
        raise ValueError(f"memory content exceeds {MEMORY_MAX_CONTENT_CHARS} characters")
    return text


def dedupe_key(content: str) -> str:
    """Case/punctuation-insensitive key used to merge near-identical memories."""
    kept = [ch for ch in content.lower() if ch.isalnum() or ch.isspace()]
    return " ".join("".join(kept).split())
