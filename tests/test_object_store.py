from unittest.mock import patch

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.storage.object_store import (
    InvalidStorageKey,
    ObjectStore,
    StorageNotConfigured,
    StorageWriteError,
)

PREFIX = "agents/e7d3594c-f61f-49c7-a4a0-ca288dd0619e/"


def _cfg():
    return AgentConfig(
        database_type="postgres",
        storage_type="dooers",
        gcp_storage_bucket="bkt",
        dooers_storage_prefix=PREFIX,
    )


@pytest.mark.asyncio
async def test_put_prefixes_key_and_records_index():
    seen = {}

    def _up(bucket, name, data, ct):
        seen["name"] = name
        return f"gs://{bucket}/{name}"

    with (
        patch("dooers.agents.server.storage.object_store.gcs.upload_bytes_to_blob_name", _up),
        patch("dooers.agents.server.storage.object_store.ObjectIndex.record") as rec,
    ):
        out = await ObjectStore(_cfg()).put("rag/a.txt", b"hi", "text/plain")
    assert seen["name"] == PREFIX + "rag/a.txt"  # auto-prefixed under the org
    assert out["key"] == "rag/a.txt"  # logical key returned
    rec.assert_called_once()


@pytest.mark.asyncio
async def test_get_reads_prefixed_object():
    def _dl(bucket, name):
        assert name == PREFIX + "rag/a.txt"
        return b"hi"

    with patch("dooers.agents.server.storage.object_store.gcs.download_bytes", _dl):
        assert await ObjectStore(_cfg()).get("rag/a.txt") == b"hi"


@pytest.mark.asyncio
async def test_list_returns_index_entries():
    with patch(
        "dooers.agents.server.storage.object_store.ObjectIndex.entries",
        return_value=[{"key": "rag/a.txt", "size": 2}],
    ):
        out = await ObjectStore(_cfg()).list("rag/")
    assert out[0]["key"] == "rag/a.txt"


@pytest.mark.asyncio
async def test_key_traversal_rejected():
    for bad in ["../secret", "/abs", "a/../../b", ""]:
        with pytest.raises(InvalidStorageKey):
            await ObjectStore(_cfg()).put(bad, b"x")


@pytest.mark.asyncio
async def test_reserved_index_key_rejected():
    # The bucket-stored manifest itself must never be reachable through the
    # creator-facing key namespace (no generation check on this path -> would
    # corrupt the org's index).
    for bad in [".dooers/object-index.json", ".dooers/anything"]:
        with pytest.raises(InvalidStorageKey):
            await ObjectStore(_cfg()).put(bad, b"x")


@pytest.mark.asyncio
async def test_put_raises_on_failed_upload_and_skips_index():
    # gcs.upload_bytes_to_blob_name swallows its own errors and returns None on
    # failure — put() must not silently record an index entry for bytes that
    # never landed.
    with (
        patch("dooers.agents.server.storage.object_store.gcs.upload_bytes_to_blob_name", return_value=None),
        patch("dooers.agents.server.storage.object_store.ObjectIndex.record") as rec,
    ):
        with pytest.raises(StorageWriteError):
            await ObjectStore(_cfg()).put("rag/a.txt", b"hi", "text/plain")
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_delete_calls_gcs_and_index_then_returns_result():
    seen = {}

    def _del(bucket, name):
        seen["name"] = name
        return True

    with (
        patch("dooers.agents.server.storage.object_store.gcs.delete_blob", _del),
        patch("dooers.agents.server.storage.object_store.ObjectIndex.remove") as rm,
    ):
        out = await ObjectStore(_cfg()).delete("rag/a.txt")
    assert seen["name"] == PREFIX + "rag/a.txt"  # gcs sees the prefixed name
    rm.assert_called_once_with("rag/a.txt")  # index sees the logical key
    assert out is True  # returns the gcs result


@pytest.mark.asyncio
async def test_delete_keeps_index_entry_when_gcs_delete_fails():
    # gcs.delete_blob returns False only on a genuine error (True when deleted or
    # already absent) — the index must not drop a key whose blob may still exist.
    with (
        patch("dooers.agents.server.storage.object_store.gcs.delete_blob", return_value=False),
        patch("dooers.agents.server.storage.object_store.ObjectIndex.remove") as rm,
    ):
        out = await ObjectStore(_cfg()).delete("rag/a.txt")
    rm.assert_not_called()
    assert out is False


@pytest.mark.asyncio
async def test_delete_none_backend_returns_false_without_gcs():
    cfg = AgentConfig(database_type="postgres", storage_type="none")
    with patch("dooers.agents.server.storage.object_store.gcs.delete_blob") as del_mock:
        assert await ObjectStore(cfg).delete("k") is False
    del_mock.assert_not_called()


@pytest.mark.asyncio
async def test_none_backend_put_raises_get_empty():
    cfg = AgentConfig(database_type="postgres", storage_type="none")
    with pytest.raises(StorageNotConfigured):
        await ObjectStore(cfg).put("k", b"x")
    assert await ObjectStore(cfg).get("k") is None
    assert await ObjectStore(cfg).list() == []


@pytest.mark.asyncio
async def test_missing_prefix_fails_closed_even_with_bucket_and_dooers_type():
    # Without a prefix, objects would land unscoped at the bucket root instead of
    # under the org's isolated path — treat that as "not configured" too.
    cfg = AgentConfig(database_type="postgres", storage_type="dooers", gcp_storage_bucket="bkt", dooers_storage_prefix="")
    with pytest.raises(StorageNotConfigured):
        await ObjectStore(cfg).put("k", b"x")
    assert await ObjectStore(cfg).get("k") is None
    assert await ObjectStore(cfg).list() == []
    assert await ObjectStore(cfg).delete("k") is False
