"""Chat artifact upload + signed read URLs from :class:`~dooers.config.AgentConfig`."""

from __future__ import annotations

import logging
import mimetypes
import time

from dooers_agents.config import AgentConfig
from dooers_agents.storage import azure_blob, gcs
from dooers_agents.storage.chat_artifact_keys import (
    build_chat_artifact_object_key,
    build_chat_artifact_thread_prefix,
)
from dooers_agents.upload_store import UploadEntry

logger = logging.getLogger(__name__)

_DELETE_MAX_ATTEMPTS = 5


def resolve_chat_artifact_backend(cfg: AgentConfig) -> str:
    pref = (cfg.chat_storage_service or "none").strip().lower()
    if pref in {"none", "gcp", "azure"}:
        return pref
    return "none"


def chat_storage_service_ready(cfg: AgentConfig) -> bool:
    return resolve_chat_artifact_backend(cfg) != "none"


def put_chat_artifact(
    cfg: AgentConfig,
    *,
    data: bytes,
    content_type: str | None,
    agent_id: str,
    thread_id: str | None,
    ref_id: str,
    filename: str,
) -> tuple[str | None, str]:
    """Upload bytes to configured backend. Returns (uri_or_none, object_key)."""
    key = build_chat_artifact_object_key(
        agent_id=agent_id,
        thread_id=thread_id,
        ref_id=ref_id,
        filename=filename,
    )
    backend = resolve_chat_artifact_backend(cfg)
    uri: str | None = None
    if backend == "gcp":
        uri = gcs.upload_bytes_to_blob_name(
            (cfg.gcp_storage_bucket or "").strip(),
            key,
            data,
            content_type,
        )
    elif backend == "azure":
        uri = azure_blob.upload_bytes_to_blob_name(
            (cfg.azure_storage_connection_string or "").strip(),
            (cfg.azure_storage_container or "").strip(),
            key,
            data,
            content_type,
        )
    else:
        logger.warning("Chat artifact upload skipped: no storage backend resolved")
    return uri, key


def try_fetch_upload_entry_from_blob(
    cfg: AgentConfig,
    *,
    agent_id: str,
    thread_id: str | None,
    ref_id: str,
    filename: str,
    mime_hint: str | None = None,
) -> UploadEntry | None:
    """Load bytes from durable chat artifact storage (cross-replica / after memory TTL)."""
    if not chat_storage_service_ready(cfg):
        return None
    key = build_chat_artifact_object_key(
        agent_id=agent_id,
        thread_id=thread_id,
        ref_id=ref_id,
        filename=filename,
    )
    backend = resolve_chat_artifact_backend(cfg)
    data: bytes | None = None
    if backend == "gcp":
        data = gcs.download_bytes((cfg.gcp_storage_bucket or "").strip(), key)
    elif backend == "azure":
        data = azure_blob.download_bytes(
            (cfg.azure_storage_connection_string or "").strip(),
            (cfg.azure_storage_container or "").strip(),
            key,
        )
    tid = (thread_id or "").strip()
    if not data and tid:
        key_nt = build_chat_artifact_object_key(
            agent_id=agent_id,
            thread_id=None,
            ref_id=ref_id,
            filename=filename,
        )
        if key_nt != key:
            if backend == "gcp":
                data = gcs.download_bytes((cfg.gcp_storage_bucket or "").strip(), key_nt)
            elif backend == "azure":
                data = azure_blob.download_bytes(
                    (cfg.azure_storage_connection_string or "").strip(),
                    (cfg.azure_storage_container or "").strip(),
                    key_nt,
                )
    if not data:
        return None
    mime = (mime_hint or "").strip()
    if not mime:
        guessed, _ = mimetypes.guess_type(filename)
        mime = guessed or "application/octet-stream"
    entry = UploadEntry(
        data=data,
        mime_type=mime,
        filename=filename,
        size_bytes=len(data),
        created_at=time.time(),
    )
    logger.info(
        "[agents] resolved upload ref_id=%s from blob (agent=%s thread=%s key_suffix=%s)",
        ref_id,
        agent_id,
        thread_id,
        filename,
    )
    return entry


def _gcs_blob_exists(bucket_name: str, blob_name: str) -> bool:
    if not bucket_name.strip() or not blob_name.strip():
        return False
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name.strip())
        blob = bucket.blob(blob_name.strip())
        return bool(blob.exists())
    except Exception as e:
        logger.debug("GCS exists check failed for %s: %s", blob_name, e)
        return False


def _azure_blob_exists(connection_string: str, container: str, blob_name: str) -> bool:
    if not connection_string.strip() or not container.strip() or not blob_name.strip():
        return False
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        client = BlobServiceClient.from_connection_string(connection_string)
        bc = client.get_blob_client(container=container.strip(), blob=blob_name.strip())
        return bool(bc.exists())
    except Exception as e:
        logger.debug("Azure exists check failed for %s: %s", blob_name, e)
        return False


def _gcs_delete_blob_with_retries(bucket_name: str, blob_name: str) -> bool:
    """Delete blob; on failure retry up to ``_DELETE_MAX_ATTEMPTS`` times with backoff."""
    if not bucket_name.strip() or not blob_name.strip():
        return True
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        return False
    client = storage.Client()
    bucket = client.bucket(bucket_name.strip())
    blob = bucket.blob(blob_name.strip())
    if not blob.exists():
        return True
    for attempt in range(_DELETE_MAX_ATTEMPTS):
        try:
            blob.delete()
            return True
        except Exception as e:
            logger.warning(
                "GCS delete attempt %s/%s failed for %s: %s",
                attempt + 1,
                _DELETE_MAX_ATTEMPTS,
                blob_name,
                e,
            )
            if attempt + 1 >= _DELETE_MAX_ATTEMPTS:
                logger.error("GCS delete exhausted retries for %s", blob_name)
                return False
            time.sleep(0.05 * (2**attempt))
    return False


def _azure_delete_blob_with_retries(connection_string: str, container: str, blob_name: str) -> bool:
    if not connection_string.strip() or not container.strip() or not blob_name.strip():
        return True
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-untyped]
    except ImportError:
        return False
    client = BlobServiceClient.from_connection_string(connection_string)
    bc = client.get_blob_client(container=container.strip(), blob=blob_name.strip())
    if not bc.exists():
        return True
    for attempt in range(_DELETE_MAX_ATTEMPTS):
        try:
            bc.delete_blob()
            return True
        except Exception as e:
            logger.warning(
                "Azure delete attempt %s/%s failed for %s: %s",
                attempt + 1,
                _DELETE_MAX_ATTEMPTS,
                blob_name,
                e,
            )
            if attempt + 1 >= _DELETE_MAX_ATTEMPTS:
                logger.error("Azure delete exhausted retries for %s", blob_name)
                return False
            time.sleep(0.05 * (2**attempt))
    return False


def promote_orphan_chat_artifact_if_present(
    cfg: AgentConfig,
    *,
    agent_id: str,
    thread_id: str | None,
    ref_id: str,
    filename: str,
    mime_hint: str | None = None,
) -> None:
    """If a durable upload lives under ``no-thread/``, copy it to the real thread key and delete the orphan.

    Used when the client uploaded before a thread id existed. Delete uses up to
    ``_DELETE_MAX_ATTEMPTS`` retries with exponential backoff; a failed delete leaves
    both objects in place (fetch still works via thread path after successful copy).

    A durable object under ``no-thread/`` after a successful copy almost always means
    the service account used here lacks ``storage.objects.delete`` (or equivalent)
    on objects created by the HTTP upload principal — grant bucket-level object admin
    or lifecycle rules to prune ``no-thread/`` if duplicates are unacceptable.
    """
    if not chat_storage_service_ready(cfg):
        return
    tid = (thread_id or "").strip()
    if not tid:
        return
    source_key = build_chat_artifact_object_key(
        agent_id=agent_id,
        thread_id=None,
        ref_id=ref_id,
        filename=filename,
    )
    dest_key = build_chat_artifact_object_key(
        agent_id=agent_id,
        thread_id=tid,
        ref_id=ref_id,
        filename=filename,
    )
    if source_key == dest_key:
        return
    backend = resolve_chat_artifact_backend(cfg)
    mime = (mime_hint or "").strip()
    if not mime:
        guessed, _ = mimetypes.guess_type(filename)
        mime = guessed or "application/octet-stream"

    if backend == "gcp":
        bucket = (cfg.gcp_storage_bucket or "").strip()
        if not _gcs_blob_exists(bucket, source_key):
            return
        if _gcs_blob_exists(bucket, dest_key):
            if not _gcs_delete_blob_with_retries(bucket, source_key):
                logger.warning(
                    "promote orphan chat artifact: copied data already at dest=%s but "
                    "failed to delete orphan source=%s (check GCS IAM for delete on "
                    "objects created by the upload principal)",
                    dest_key,
                    source_key,
                )
            return
        # Prefer server-side copy (same bucket) — avoids RAM and client upload limits.
        uri = gcs.copy_blob_same_bucket(bucket, source_key, dest_key)
        if not uri:
            data = gcs.download_bytes(bucket, source_key)
            if not data:
                return
            uri = gcs.upload_bytes_to_blob_name(bucket, dest_key, data, mime)
            if not uri:
                logger.warning("promote orphan chat artifact: GCS upload failed dest=%s", dest_key)
                return
        if not _gcs_delete_blob_with_retries(bucket, source_key):
            logger.warning(
                "promote orphan chat artifact: GCS promoted dest=%s but failed to delete "
                "orphan source=%s (orphan may remain under no-thread/; check bucket IAM)",
                dest_key,
                source_key,
            )
        return

    if backend == "azure":
        cs = (cfg.azure_storage_connection_string or "").strip()
        container = (cfg.azure_storage_container or "").strip()
        data = azure_blob.download_bytes(cs, container, source_key)
        if not data:
            return
        if _azure_blob_exists(cs, container, dest_key):
            if not _azure_delete_blob_with_retries(cs, container, source_key):
                logger.warning(
                    "promote orphan chat artifact: dest=%s exists but failed to delete "
                    "orphan source=%s (check Azure RBAC for delete on upload-created blobs)",
                    dest_key,
                    source_key,
                )
            return
        uri = azure_blob.upload_bytes_to_blob_name(cs, container, dest_key, data, mime)
        if not uri:
            logger.warning("promote orphan chat artifact: Azure upload failed dest=%s", dest_key)
            return
        if not _azure_delete_blob_with_retries(cs, container, source_key):
            logger.warning(
                "promote orphan chat artifact: Azure copy ok dest=%s but failed to delete orphan source=%s",
                dest_key,
                source_key,
            )


def delete_chat_artifacts_for_thread(
    cfg: AgentConfig,
    *,
    agent_id: str,
    thread_id: str,
) -> int:
    """Delete all durable chat blobs under ``chat-artifacts/v1/<agent>/<thread>/``.

    Called when a thread is removed so storage does not retain orphaned attachments.
    Does not delete ``no-thread/`` uploads (those are not thread-scoped).
    """
    if not chat_storage_service_ready(cfg):
        return 0
    tid = (thread_id or "").strip()
    aid = (agent_id or "").strip()
    if not tid or not aid:
        return 0
    prefix = build_chat_artifact_thread_prefix(agent_id=aid, thread_id=tid)
    backend = resolve_chat_artifact_backend(cfg)
    if backend == "gcp":
        n = gcs.delete_blobs_with_prefix((cfg.gcp_storage_bucket or "").strip(), prefix)
        if n:
            logger.info("[agents] deleted %s chat artifact object(s) prefix=%s", n, prefix)
        return n
    if backend == "azure":
        n = azure_blob.delete_blobs_with_prefix(
            (cfg.azure_storage_connection_string or "").strip(),
            (cfg.azure_storage_container or "").strip(),
            prefix,
        )
        if n:
            logger.info("[agents] deleted %s chat artifact blob(s) prefix=%s", n, prefix)
        return n
    return 0


def chat_artifact_object_exists(cfg: AgentConfig, object_key: str) -> bool:
    """Return True if the blob exists in the configured chat artifact backend."""
    if not chat_storage_service_ready(cfg) or not (object_key or "").strip():
        return False
    backend = resolve_chat_artifact_backend(cfg)
    key = object_key.strip()
    if backend == "gcp":
        return _gcs_blob_exists((cfg.gcp_storage_bucket or "").strip(), key)
    if backend == "azure":
        return _azure_blob_exists(
            (cfg.azure_storage_connection_string or "").strip(),
            (cfg.azure_storage_container or "").strip(),
            key,
        )
    return False


def sign_chat_artifact_read_url(cfg: AgentConfig, object_key: str) -> str | None:
    ttl = max(1, int(getattr(cfg, "chat_artifact_signed_url_ttl_minutes", 60) or 60))
    backend = resolve_chat_artifact_backend(cfg)
    if backend == "gcp":
        return gcs.generate_signed_get_url(
            (cfg.gcp_storage_bucket or "").strip(),
            object_key,
            expiration_minutes=ttl,
        )
    if backend == "azure":
        return azure_blob.generate_blob_read_sas_url(
            (cfg.azure_storage_connection_string or "").strip(),
            (cfg.azure_storage_container or "").strip(),
            object_key,
            expiration_minutes=ttl,
        )
    return None
