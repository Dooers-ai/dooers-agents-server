"""GCS blob upload and V4 signed read URLs (optional google-cloud-storage)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud.storage import Client  # noqa: F401

logger = logging.getLogger(__name__)


def upload_bytes_to_blob_name(
    bucket_name: str,
    blob_name: str,
    data: bytes,
    content_type: str | None = None,
) -> str | None:
    """Return gs:// URI or None."""
    if not bucket_name.strip() or not blob_name.strip():
        return None
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("google-cloud-storage is not installed; skipping GCS upload")
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name.strip())
        blob = bucket.blob(blob_name.strip())
        blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
        uri = f"gs://{bucket_name.strip()}/{blob_name.strip()}"
        logger.info("GCS chat artifact uploaded %s", uri)
        return uri
    except Exception as e:
        logger.warning("GCS upload failed: %s", e)
        return None


def generate_signed_get_url(
    bucket_name: str,
    blob_name: str,
    *,
    expiration_minutes: int = 60,
) -> str | None:
    if not bucket_name.strip() or not blob_name.strip():
        return None
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name.strip())
        blob = bucket.blob(blob_name.strip())
        return blob.generate_signed_url(
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            version="v4",
        )
    except Exception as e:
        logger.warning("GCS signed URL failed for %s: %s", blob_name, e)
        return None


def delete_blobs_with_prefix(bucket_name: str, prefix: str) -> int:
    """Delete all objects whose names start with ``prefix``. Returns count deleted."""
    bn = bucket_name.strip()
    pfx = (prefix or "").strip()
    if not bn or not pfx:
        return 0
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        return 0
    try:
        client = storage.Client()
        bucket = client.bucket(bn)
        n = 0
        for blob in bucket.list_blobs(prefix=pfx):
            try:
                blob.delete()
                n += 1
            except Exception as e:
                logger.warning("GCS delete failed for %s: %s", blob.name, e)
        if n:
            logger.info("GCS deleted %d object(s) under prefix %s", n, pfx)
        return n
    except Exception as e:
        logger.warning("GCS list/delete prefix failed %s: %s", pfx, e)
        return 0


def copy_blob_same_bucket(
    bucket_name: str,
    source_blob_name: str,
    dest_blob_name: str,
) -> str | None:
    """Server-side copy within one bucket (no download through app memory).

    Uses the JSON ``copy``/``rewrite`` API. Prefer this for promoting ``no-thread/``
    objects to a thread prefix when the workload SA can ``objects.get`` + ``create``
    but multipart client uploads fail (size, timeout, or policy edge cases).
    """
    bn = bucket_name.strip()
    src = source_blob_name.strip()
    dst = dest_blob_name.strip()
    if not bn or not src or not dst:
        return None
    if src == dst:
        return f"gs://{bn}/{dst}"
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(bn)
        source_blob = bucket.blob(src)
        if not source_blob.exists():
            return None
        bucket.copy_blob(source_blob, bucket, dst)
        uri = f"gs://{bn}/{dst}"
        logger.info("GCS chat artifact server-side copy %s -> %s", src, dst)
        return uri
    except Exception as e:
        logger.warning("GCS copy_blob failed %s -> %s: %s", src, dst, e)
        return None


def download_bytes(bucket_name: str, blob_name: str) -> bytes | None:
    """Read object bytes or None if missing / error."""
    if not bucket_name.strip() or not blob_name.strip():
        return None
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name.strip())
        blob = bucket.blob(blob_name.strip())
        if not blob.exists():
            return None
        return blob.download_as_bytes()
    except Exception as e:
        logger.debug("GCS download failed for %s: %s", blob_name, e)
        return None
