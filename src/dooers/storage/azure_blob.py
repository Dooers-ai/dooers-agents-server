"""Azure Blob upload and read SAS URLs (optional azure-storage-blob)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _parse_account_name_key(connection_string: str) -> tuple[str, str] | None:
    if not (connection_string or "").strip():
        return None
    name_m = re.search(r"AccountName=([^;]+)", connection_string, re.IGNORECASE)
    key_m = re.search(r"AccountKey=([^;]+)", connection_string, re.IGNORECASE)
    if not name_m or not key_m:
        return None
    return name_m.group(1).strip(), key_m.group(1).strip()


def upload_bytes_to_blob_name(
    connection_string: str,
    container: str,
    blob_name: str,
    data: bytes,
    content_type: str | None = None,
) -> str | None:
    if not connection_string.strip() or not container.strip() or not blob_name.strip():
        return None
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("azure-storage-blob is not installed; skipping Azure upload")
        return None
    try:
        client = BlobServiceClient.from_connection_string(connection_string)
        cont = client.get_container_client(container.strip())
        try:
            cont.create_container()
        except Exception:
            pass
        bn = blob_name.strip()
        blob = cont.get_blob_client(bn)
        blob.upload_blob(
            data,
            overwrite=True,
            content_type=content_type or "application/octet-stream",
        )
        return blob.url
    except Exception as e:
        logger.warning("Azure blob upload failed: %s", e)
        return None


def generate_blob_read_sas_url(
    connection_string: str,
    container: str,
    blob_name: str,
    *,
    expiration_minutes: int = 60,
) -> str | None:
    if not connection_string.strip() or not container.strip() or not blob_name.strip():
        return None
    parsed = _parse_account_name_key(connection_string)
    if not parsed:
        logger.warning("Azure connection string missing AccountName/AccountKey")
        return None
    account_name, account_key = parsed
    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas  # type: ignore[import-untyped]
    except ImportError:
        return None
    bn = blob_name.strip()
    now = datetime.now(UTC)
    try:
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=container.strip(),
            blob_name=bn,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=now + timedelta(minutes=expiration_minutes),
            start=now - timedelta(seconds=60),
        )
    except Exception as e:
        logger.warning("Azure SAS generation failed for %s: %s", bn, e)
        return None
    enc = "/".join(quote(seg, safe="") for seg in bn.split("/"))
    return f"https://{account_name}.blob.core.windows.net/{container.strip()}/{enc}?{sas}"


def download_bytes(connection_string: str, container: str, blob_name: str) -> bytes | None:
    """Read blob bytes or None if missing / error."""
    if not connection_string.strip() or not container.strip() or not blob_name.strip():
        return None
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        client = BlobServiceClient.from_connection_string(connection_string)
        bc = client.get_blob_client(container=container.strip(), blob=blob_name.strip())
        if not bc.exists():
            return None
        return bc.download_blob().readall()
    except Exception as e:
        logger.debug("Azure blob download failed for %s: %s", blob_name, e)
        return None


def delete_blobs_with_prefix(connection_string: str, container: str, prefix: str) -> int:
    """Delete all blobs whose names start with ``prefix``. Returns count deleted."""
    if not connection_string.strip() or not container.strip() or not (prefix or "").strip():
        return 0
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-untyped]
    except ImportError:
        return 0
    try:
        client = BlobServiceClient.from_connection_string(connection_string)
        cc = client.get_container_client(container.strip())
        pfx = prefix.strip()
        n = 0
        for blob in cc.list_blobs(name_starts_with=pfx):
            name = getattr(blob, "name", None) or ""
            if not name:
                continue
            try:
                cc.delete_blob(name)
                n += 1
            except Exception as e:
                logger.warning("Azure delete failed for %s: %s", name, e)
        if n:
            logger.info("Azure deleted %d blob(s) under prefix %s", n, pfx)
        return n
    except Exception as e:
        logger.warning("Azure list/delete prefix failed %s: %s", prefix, e)
        return 0
