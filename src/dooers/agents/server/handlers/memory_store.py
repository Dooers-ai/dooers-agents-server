"""Durable agent memory: AlloyDB/Postgres authority + Core-delegated RAG index.

This module is SDK-owned, never instantiated from LLM-provided identity.
Semantic memory requires authenticated dashboard delegation, not a runtime key.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from dooers.agents.server.database import SqlDatabase
from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.tools.rag.runtime import current_execution_context

logger = logging.getLogger(__name__)


class MemoryUnavailableError(RuntimeError):
    """Memory requires Postgres/AlloyDB and a verified user delegation."""


class LongTermMemoryStore:
    def __init__(self, persistence: PostgresPersistence, context: AgentContext):
        self.db = SqlDatabase(persistence)
        self.context = context
        self.memories_table = f"{persistence._prefix}long_term_memories"
        self.snapshots_table = f"{persistence._prefix}thread_context_snapshots"

    def _identity(self) -> tuple[str, str, str]:
        ctx = self.context
        runtime = current_execution_context()
        uid = (ctx.user.user_id or "").strip()
        if (
            not runtime or not runtime.auth_assertion or
            not uid or uid.startswith("guest:") or
            ctx.channel != "dooers-platform" or
            runtime.user_id != uid or runtime.agent_id != ctx.agent_id or
            not ctx.agent_id or not ctx.organization_id
        ):
            raise MemoryUnavailableError("Personal memory requires a Core-verified user delegation")
        return ctx.organization_id, ctx.agent_id, uid

    async def remember(self, content: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        from dooers.tools import rag

        organization_id, agent_id, uid = self._identity()
        content = (content or "").strip()
        if not content or len(content) > 5000:
            raise ValueError("memory content must contain 1 to 5000 characters")
        memory_id = uuid4()
        await self.db.execute(
            f"""INSERT INTO {self.memories_table}
                (id,organization_id,agent_id,scope,scope_id,content,metadata,
                 source_thread_id,source_event_id)
                VALUES ($1,$2,$3,'user',$4,$5,$6::jsonb,$7,$8)""",
            memory_id, organization_id, agent_id, uid, content,
            json.dumps(metadata or {}, default=str),
            self.context.thread_id, self.context.event_id,
        )
        # Store first; failed indexing remains pending and can be safely retried.
        await rag.memory_upsert(memory_id=str(memory_id), content=content, metadata=metadata)
        await self._mark_indexed(memory_id)
        return {"memory_id": str(memory_id), "content": content}

    async def _mark_indexed(self, memory_id: UUID) -> None:
        org, agent, uid = self._identity()
        await self.db.execute(
            f"""UPDATE {self.memories_table} SET index_state='ready'
                WHERE id=$1 AND organization_id=$2 AND agent_id=$3
                  AND scope='user' AND scope_id=$4 AND status='active'""",
            memory_id, org, agent, uid,
        )

    async def sync_pending(self, limit: int = 10) -> int:
        from dooers.tools import rag

        org, agent, uid = self._identity()
        rows = await self.db.fetch(
            f"""SELECT id,content,metadata FROM {self.memories_table}
                WHERE organization_id=$1 AND agent_id=$2 AND scope='user'
                  AND scope_id=$3 AND status='active' AND index_state='pending'
                ORDER BY created_at ASC LIMIT $4""",
            org, agent, uid, max(1, min(limit, 100)),
        )
        count = 0
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            try:
                await rag.memory_upsert(memory_id=str(row["id"]), content=row["content"], metadata=metadata)
                await self._mark_indexed(row["id"])
                count += 1
            except Exception:
                logger.warning("memory index retry failed for memory %s", row["id"], exc_info=True)
        return count

    async def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        from dooers.tools import rag

        org, agent, uid = self._identity()
        await self.sync_pending(limit=10)
        hits = await rag.memory_search(query.strip(), limit=max(1, min(limit, 50)))
        ids: list[UUID] = []
        for h in hits:
            try:
                ids.append(UUID(h["memory_id"]))
            except (ValueError, TypeError, KeyError):
                continue
        if not ids:
            return []
        # Recheck the canonical SQL record, including status, to prevent a
        # stale RAG index from resurrecting deleted or superseded memories.
        rows = await self.db.fetch(
            f"""SELECT id,content,metadata,version,updated_at
                FROM {self.memories_table} WHERE id=ANY($1::uuid[])
                  AND organization_id=$2 AND agent_id=$3 AND scope='user'
                  AND scope_id=$4 AND status='active'""",
            ids, org, agent, uid,
        )
        by_id = {str(row["id"]): row for row in rows}
        result = []
        for h in hits:
            row = by_id.get(h.get("memory_id"))
            if row is None:
                continue
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            result.append({
                "memory_id": str(row["id"]), "content": row["content"],
                "metadata": metadata, "version": row["version"],
                "score": h.get("score"),
            })
        return result

    async def update(self, memory_id: str, content: str) -> bool:
        from dooers.tools import rag

        org, agent, uid = self._identity()
        content = content.strip()
        if not content or len(content) > 5000:
            raise ValueError("memory content must contain 1 to 5000 characters")
        mid = UUID(memory_id)
        row = await self.db.fetchrow(
            f"""UPDATE {self.memories_table}
                SET content=$1,version=version+1,index_state='pending',updated_at=now()
                WHERE id=$2 AND organization_id=$3 AND agent_id=$4
                  AND scope='user' AND scope_id=$5 AND status='active'
                RETURNING id,metadata""",
            content,mid,org,agent,uid,
        )
        if not row:
            return False
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        await rag.memory_upsert(memory_id=memory_id, content=content, metadata=metadata)
        await self._mark_indexed(mid)
        return True

    async def forget(self, memory_id: str) -> bool:
        from dooers.tools import rag

        org, agent, uid = self._identity()
        mid = UUID(memory_id)
        row = await self.db.fetchrow(
            f"""UPDATE {self.memories_table}
                SET status='deleted',updated_at=now()
                WHERE id=$1 AND organization_id=$2 AND agent_id=$3
                  AND scope='user' AND scope_id=$4 AND status='active'
                RETURNING id""",
            mid,org,agent,uid,
        )
        if row is None:
            return False
        # SQL tombstone takes effect before the search index deletion.
        await rag.memory_delete(memory_id)
        return True

    async def get_summary(self) -> dict[str, str] | None:
        org, agent, _ = self._identity()
        row = await self.db.fetchrow(
            f"""SELECT summary,last_event_id FROM {self.snapshots_table}
                WHERE thread_id=$1 AND organization_id=$2 AND agent_id=$3""",
            self.context.thread_id, org, agent,
        )
        return dict(row) if row else None

    async def compact_history(self, summary: str, *, last_event_id: str) -> None:
        """Persist an LLM-generated summary and processed-history cursor.

        Callers use the Dooers Gateway to build/update the summary; this SDK
        method guarantees durable storage, not the summarization algorithm.
        """
        org, agent, _ = self._identity()
        if not summary.strip() or not last_event_id:
            raise ValueError("summary and last_event_id are required")
        await self.db.execute(
            f"""INSERT INTO {self.snapshots_table}
                (thread_id,organization_id,agent_id,summary,last_event_id)
                VALUES($1,$2,$3,$4,$5)
                ON CONFLICT(thread_id) DO UPDATE SET summary=EXCLUDED.summary,
                  last_event_id=EXCLUDED.last_event_id,updated_at=now()
                WHERE {self.snapshots_table}.agent_id=EXCLUDED.agent_id
                  AND {self.snapshots_table}.organization_id=EXCLUDED.organization_id""",
            self.context.thread_id,org,agent,summary.strip(),last_event_id,
        )

    async def prepare_context(self, query: str, *, token_budget: int = 2000) -> dict[str, Any]:
        """Budget bounded contextual memories; history remains in AgentMemory."""
        summary = await self.get_summary()
        found = await self.search(query, limit=8)
        remaining = max(256, min(int(token_budget), 16000)) * 4
        selected: list[dict[str, Any]] = []
        for item in found:
            if len(item["content"]) > remaining:
                break
            selected.append(item)
            remaining -= len(item["content"])
        return {"summary": summary, "memories": selected}
