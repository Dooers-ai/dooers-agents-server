"""Tests for promoting no-thread chat artifacts to a real thread key."""

from __future__ import annotations

from unittest.mock import patch

from dooers_agents.config import AgentConfig
from dooers_agents.storage.chat_artifact_keys import build_chat_artifact_object_key
from dooers_agents.storage.chat_artifacts import (
    promote_orphan_chat_artifact_if_present,
    try_fetch_upload_entry_from_blob,
)


def _cfg_gcp() -> AgentConfig:
    return AgentConfig(
        database_type="postgres",
        chat_storage_service="gcp",
        gcp_storage_bucket="test-bucket",
    )


def test_promote_skips_when_storage_none() -> None:
    cfg = AgentConfig(database_type="postgres", chat_storage_service="none")
    with patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes") as dl:
        promote_orphan_chat_artifact_if_present(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
        )
    dl.assert_not_called()


def test_promote_skips_when_thread_id_empty() -> None:
    promote_orphan_chat_artifact_if_present(
        _cfg_gcp(),
        agent_id="a1",
        thread_id="   ",
        ref_id="r1",
        filename="x.png",
    )


def test_promote_skips_when_no_orphan_bytes() -> None:
    cfg = _cfg_gcp()
    with (
        patch("dooers_agents.storage.chat_artifacts._gcs_blob_exists", side_effect=[True, False]),
        patch("dooers_agents.storage.chat_artifacts.gcs.copy_blob_same_bucket", return_value=None),
        patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes", return_value=None) as dl,
    ):
        promote_orphan_chat_artifact_if_present(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
        )
    dl.assert_called_once()


def test_promote_gcp_uploads_then_deletes_source() -> None:
    cfg = _cfg_gcp()
    payload = b"hello"
    dest_key = build_chat_artifact_object_key(
        agent_id="a1",
        thread_id="t1",
        ref_id="r1",
        filename="x.png",
    )
    with (
        patch("dooers_agents.storage.chat_artifacts._gcs_blob_exists", side_effect=[True, False]) as ex,
        patch("dooers_agents.storage.chat_artifacts.gcs.copy_blob_same_bucket", return_value=None),
        patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes", return_value=payload) as dl,
        patch(
            "dooers_agents.storage.chat_artifacts.gcs.upload_bytes_to_blob_name",
            return_value="gs://test-bucket/x",
        ) as up,
        patch(
            "dooers_agents.storage.chat_artifacts._gcs_delete_blob_with_retries",
            return_value=True,
        ) as rm,
    ):
        promote_orphan_chat_artifact_if_present(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
            mime_hint="image/png",
        )
    assert dl.call_count == 1
    assert ex.call_count == 2
    assert ex.call_args_list[1].args == ("test-bucket", dest_key)
    up.assert_called_once_with("test-bucket", dest_key, payload, "image/png")
    rm.assert_called_once()


def test_promote_gcp_when_dest_exists_only_deletes_source() -> None:
    cfg = _cfg_gcp()
    with (
        patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes") as dl,
        patch("dooers_agents.storage.chat_artifacts._gcs_blob_exists", side_effect=[True, True]),
        patch("dooers_agents.storage.chat_artifacts.gcs.upload_bytes_to_blob_name") as up,
        patch(
            "dooers_agents.storage.chat_artifacts._gcs_delete_blob_with_retries",
            return_value=True,
        ) as rm,
    ):
        promote_orphan_chat_artifact_if_present(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
        )
    dl.assert_not_called()
    up.assert_not_called()
    rm.assert_called_once()


def test_promote_gcp_no_delete_when_upload_fails() -> None:
    cfg = _cfg_gcp()
    with (
        patch("dooers_agents.storage.chat_artifacts._gcs_blob_exists", side_effect=[True, False]),
        patch("dooers_agents.storage.chat_artifacts.gcs.copy_blob_same_bucket", return_value=None),
        patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes", return_value=b"data"),
        patch("dooers_agents.storage.chat_artifacts.gcs.upload_bytes_to_blob_name", return_value=None),
        patch(
            "dooers_agents.storage.chat_artifacts._gcs_delete_blob_with_retries",
            return_value=True,
        ) as rm,
    ):
        promote_orphan_chat_artifact_if_present(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
        )
    rm.assert_not_called()


def test_promote_gcp_server_side_copy_skips_download_upload() -> None:
    cfg = _cfg_gcp()
    dest_key = build_chat_artifact_object_key(
        agent_id="a1",
        thread_id="t1",
        ref_id="r1",
        filename="x.png",
    )
    with (
        patch("dooers_agents.storage.chat_artifacts._gcs_blob_exists", side_effect=[True, False]),
        patch(
            "dooers_agents.storage.chat_artifacts.gcs.copy_blob_same_bucket",
            return_value="gs://test-bucket/d",
        ) as cp,
        patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes") as dl,
        patch("dooers_agents.storage.chat_artifacts.gcs.upload_bytes_to_blob_name") as up,
        patch(
            "dooers_agents.storage.chat_artifacts._gcs_delete_blob_with_retries",
            return_value=True,
        ) as rm,
    ):
        promote_orphan_chat_artifact_if_present(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
            mime_hint="image/png",
        )
    dl.assert_not_called()
    up.assert_not_called()
    cp.assert_called_once()
    assert cp.call_args[0][0] == "test-bucket"
    assert cp.call_args[0][2] == dest_key
    rm.assert_called_once()


def test_try_fetch_falls_back_to_no_thread_when_thread_key_missing() -> None:
    cfg = _cfg_gcp()
    key_t = build_chat_artifact_object_key(
        agent_id="a1",
        thread_id="t1",
        ref_id="r1",
        filename="x.png",
    )
    key_nt = build_chat_artifact_object_key(
        agent_id="a1",
        thread_id=None,
        ref_id="r1",
        filename="x.png",
    )
    with patch("dooers_agents.storage.chat_artifacts.gcs.download_bytes") as dl:
        dl.side_effect = [None, b"orphan-bytes"]
        entry = try_fetch_upload_entry_from_blob(
            cfg,
            agent_id="a1",
            thread_id="t1",
            ref_id="r1",
            filename="x.png",
            mime_hint="image/png",
        )
    assert entry is not None
    assert entry.data == b"orphan-bytes"
    assert dl.call_count == 2
    assert dl.call_args_list[0][0][1] == key_t
    assert dl.call_args_list[1][0][1] == key_nt
