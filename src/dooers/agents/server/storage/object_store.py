"""Creator-facing object storage: agent.storage.put/get/list/delete.

Logical keys are auto-scoped under the org prefix (agents/<org_id>/), so the
creator never handles paths or isolation. Backend is AgentConfig.storage_type.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.storage import gcs
from dooers.agents.server.storage.object_index import ObjectIndex

logger = logging.getLogger(__name__)

_INDEX_SUFFIX = ".dooers/object-index.json"


class StorageNotConfigured(RuntimeError):
    """agent.storage used while storage_type is not a managed backend."""


class InvalidStorageKey(ValueError):
    """A logical key that escapes the org prefix or is empty."""


def _safe_key(key: str) -> str:
    k = (key or "").strip()
    if not k or k.startswith("/") or "\\" in k:
        raise InvalidStorageKey(f"invalid object key: {key!r}")
    if k == ".." or k.startswith("../") or "/../" in k or k.endswith("/.."):
        raise InvalidStorageKey(f"invalid object key: {key!r}")
    norm = posixpath.normpath(k)
    if norm != k or norm.startswith(".."):
        raise InvalidStorageKey(f"invalid object key: {key!r}")
    return k


class ObjectStore:
    def __init__(self, cfg: AgentConfig) -> None:
        self._cfg = cfg
        self._bucket = (getattr(cfg, "gcp_storage_bucket", "") or "").strip()
        self._prefix = (getattr(cfg, "dooers_storage_prefix", "") or "").strip()

    @property
    def _enabled(self) -> bool:
        return (self._cfg.storage_type or "none") == "dooers" and bool(self._bucket)

    def _index(self) -> ObjectIndex:
        return ObjectIndex(self._bucket, self._prefix + _INDEX_SUFFIX)

    def _blob_name(self, key: str) -> str:
        return self._prefix + _safe_key(key)

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> dict:
        if not self._enabled:
            raise StorageNotConfigured("storage_type is not 'dooers'")
        name = self._blob_name(key)
        uri = await asyncio.to_thread(gcs.upload_bytes_to_blob_name, self._bucket, name, data, content_type)
        await asyncio.to_thread(self._index().record, _safe_key(key), len(data), content_type)
        return {"key": _safe_key(key), "uri": uri, "size": len(data)}

    async def get(self, key: str) -> bytes | None:
        if not self._enabled:
            return None
        return await asyncio.to_thread(gcs.download_bytes, self._bucket, self._blob_name(key))

    async def list(self, prefix: str = "") -> list[dict]:
        if not self._enabled:
            return []
        return await asyncio.to_thread(self._index().entries, (prefix or "").lstrip("/"))

    async def delete(self, key: str) -> bool:
        if not self._enabled:
            return False
        ok = await asyncio.to_thread(gcs.delete_blob, self._bucket, self._blob_name(key))
        await asyncio.to_thread(self._index().remove, _safe_key(key))
        return ok
