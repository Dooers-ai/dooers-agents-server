"""Mídia Maestro via proxy autorizado; não usa storage/ref consumível legado."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from .client import MaestroContextClient, MaestroContextError
from .links import ArtifactLinkMissing
from .security import strict_json

logger = logging.getLogger(__name__)

_UPLOAD_BYTES_PER_SECOND = 1048576


def _error_code(raw: bytes) -> str | None:
    """``error.code`` of a ``Public.Error`` body, or ``None``; the rest of the body is never read."""
    try:
        code = strict_json(raw)["error"]["code"]
    except Exception:
        return None
    return code if isinstance(code, str) else None


def artifact_type_allowed(config: Any, mime_type: str, filename: str) -> bool:
    """Política de produção para arquivos (``infer_chat_upload_kind``: imagem, áudio ou documento).

    ``MaestroConfig.artifact_mime_types`` vazio (padrão) aceita tudo que a política aceita.
    Não vazio, restringe a esses tipos.
    """
    from dooers.agents.server.storage.chat_upload_file_policy import infer_chat_upload_kind

    if (
        not isinstance(mime_type, str)
        or not isinstance(filename, str)
        or not re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", mime_type)
        or not 3 <= len(mime_type) <= 128
        or infer_chat_upload_kind(mime_type, filename) is None
    ):
        return False
    return not config.artifact_mime_types or mime_type in config.artifact_mime_types


#: Why an execution cannot hand media to the handler (logged and raised; never a URL or a token).
ARTIFACT_LINKS_UNAVAILABLE = (
    "UNSUPPORTED_CAPABILITY: Maestro did not issue a temporary artifact link for this execution. "
    "Enable ARTIFACT_LINKS_ENABLED and set MAESTRO_ARTIFACT_LINK_KEY on the Maestro service. "
    "The SDK never gives the handler a media part without its link."
)


class MaestroArtifactLinkUnavailable(MaestroContextError):
    """Maestro has no artifact link route for this execution: a configuration error."""

    def __init__(self) -> None:
        super().__init__("UNSUPPORTED_CAPABILITY")
        self.args = (ARTIFACT_LINKS_UNAVAILABLE,)


@dataclass(frozen=True)
class MaestroHydratedArtifact:
    artifact_id: str
    filename: str
    mime_type: str
    data: bytes = field(repr=False)

    def content_part(self, url: str | None, *, duration: float | None = None):
        """The production part of an uploaded file: the bytes and the temporary link (``url``).

        ``url`` is ``None`` when Maestro has no link for this one file (``ArtifactLinkMissing``).

        Same fields as the production upload path (``ref_id`` parts of ``HandlerPipeline``):
        an audio keeps the ``duration`` of the message part; an image has no size.
        """
        from dooers.agents.server.protocol.models import AudioPart, DocumentPart, ImagePart

        if self.mime_type.startswith("audio/"):
            return AudioPart(data=self.data, mime_type=self.mime_type, duration=duration, filename=self.filename, url=url)
        if self.mime_type.startswith("image/"):
            return ImagePart(data=self.data, mime_type=self.mime_type, filename=self.filename, url=url)
        return DocumentPart(data=self.data, mime_type=self.mime_type, filename=self.filename, size_bytes=len(self.data), url=url)


class MaestroArtifactsClient:
    """Compartilha lease/capability/cancelamento do executor, nunca cache de bytes.

    Caminhos são construídos de IDs validados. Metadata do evento não é URL nem
    autoridade; cada GET/POST obtém credencial própria e revalida no Maestro.
    """

    def __init__(self, client: MaestroContextClient) -> None:
        self._client = client

    def _operation(self, operation: str, *, size: int = 0) -> float:
        """Time budget: ``artifact_timeout_seconds``, longer for a large upload (1 MiB/s floor)."""
        client = self._client
        if operation not in client._claims["operations"]:
            raise MaestroContextError("UNSUPPORTED_CAPABILITY")
        seconds = max(client.config.artifact_timeout_seconds, math.ceil(size / _UPLOAD_BYTES_PER_SECOND))
        return min(client._check_current(), seconds)

    async def _headers(self, *, uploading: bool = False) -> tuple[dict[str, str], float]:
        client = self._client
        credential = await client._credential(("responses:write",) if uploading else ("context:read", "deliveries:read"))
        client._check_current()
        return {
            "Authorization": f"Bearer {credential.token}",
            "Maestro-Execution-Capability": client._capability,
            "Accept-Encoding": "identity",
            "Cache-Control": "no-store",
        }, credential.expires_at

    async def hydrate(self, reference: Any, *, maximum_bytes: int | None = None) -> MaestroHydratedArtifact:
        client = self._client
        raw = bytearray()
        try:
            if str(UUID(reference.artifact_id)) != reference.artifact_id:
                raise ValueError
            budget = self._operation("artifact.download")
            maximum = client.config.max_artifact_bytes if maximum_bytes is None else maximum_bytes
            if type(maximum) is not int or not 0 < maximum <= client.config.max_artifact_bytes:
                raise MaestroContextError("CONTEXT_LIMIT")
            async with asyncio.timeout(budget):
                headers, expires_at = await self._headers()
                headers.update(await client.config.transport_headers(client.config.callback_base_url))
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as http:
                    url = f"{client.config.callback_base_url}/artifacts/{reference.artifact_id}/download"
                    async with http.stream("GET", url, headers=headers) as response:
                        self._status(response.status_code, 200)
                        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        length = response.headers.get("content-length", "")
                        if (
                            not artifact_type_allowed(client.config, mime, reference.filename or "attachment")
                            or response.headers.get("content-encoding", "identity") != "identity"
                            or not length.isascii()
                            or not length.isdecimal()
                            or not 0 < int(length) <= maximum
                            or reference.mime_type is not None
                            and reference.mime_type != mime
                            or reference.size_bytes is not None
                            and reference.size_bytes != int(length)
                        ):
                            raise MaestroContextError("CONTEXT_LIMIT")
                        async for chunk in response.aiter_raw():
                            client._check_current()
                            if time.time() >= expires_at:
                                raise MaestroContextError("ACCESS_REVOKED")
                            if len(raw) + len(chunk) > min(int(length), maximum):
                                raise MaestroContextError("CONTEXT_LIMIT")
                            raw.extend(chunk)
                        client._check_current()
                        if len(raw) != int(length) or time.time() >= expires_at:
                            raise MaestroContextError("CONTEXT_INVALID")
                        return MaestroHydratedArtifact(reference.artifact_id, reference.filename or "attachment", mime, bytes(raw))
        except MaestroContextError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise MaestroContextError("CONTEXT_INVALID") from None
        except Exception:
            client._closed = True
            raise MaestroContextError() from None
        finally:
            # Drop/overwrite partial buffers on failure, timeout and cancellation.
            raw[:] = b"\0" * len(raw)
            raw.clear()

    async def issue_link(self, artifact_id: str) -> dict[str, Any]:
        """``POST /executions/{id}/artifacts/{artifact_id}/links``: a temporary link, ``{url, expires_at}``.

        Maestro issues it only for a file of this thread linked up to the context cutoff (the
        attachments of the delivery qualify). A capability without ``artifact.link.issue`` or an
        ``UNSUPPORTED_CAPABILITY`` answer means the route is off (Maestro signs the operation only
        with ``ARTIFACT_LINKS_ENABLED``): that raises ``MaestroArtifactLinkUnavailable`` and logs
        which settings to change. A 403/404 is about this one file (removed, not linked up to the
        cutoff, not allowed): that raises ``ArtifactLinkMissing`` and the client stays open. The
        URL is a bearer link and is never logged.
        """
        client = self._client
        try:
            if str(UUID(artifact_id)) != artifact_id:
                raise ValueError
            if "artifact.link.issue" not in client._claims["operations"]:
                raise MaestroArtifactLinkUnavailable()
            budget = self._operation("artifact.link.issue")
            envelope = client._envelope
            async with asyncio.timeout(budget):
                headers, expires_at = await self._headers()
                headers.update({"Content-Type": "application/json", "Accept": "application/json"})
                headers.update(await client.config.transport_headers(client.config.callback_base_url))
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as http:
                    url = f"{client.config.callback_base_url}/executions/{envelope['execution_id']}/artifacts/{artifact_id}/links"
                    async with http.stream("POST", url, headers=headers, content=b"{}") as response:
                        raw = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(raw) + len(chunk) > 65536:
                                raise MaestroContextError("CONTEXT_LIMIT")
                            raw.extend(chunk)
                        if response.status_code != 200:
                            if _error_code(bytes(raw)) == "UNSUPPORTED_CAPABILITY":
                                raise MaestroArtifactLinkUnavailable()
                            if response.status_code in {403, 404}:
                                # This one file: the other parts and the execution go on.
                                raise ArtifactLinkMissing()
                            self._status(response.status_code, 200)
                        if (
                            response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
                            or response.headers.get("content-encoding", "identity") != "identity"
                        ):
                            raise MaestroContextError("CONTEXT_INVALID")
                        result = client._contracts.validate("Public.ArtifactLinkResponse", strict_json(bytes(raw)))
                        client._check_current()
                        if time.time() >= expires_at:
                            raise MaestroContextError("ACCESS_REVOKED")
                        return result
        except MaestroArtifactLinkUnavailable:
            logger.error(ARTIFACT_LINKS_UNAVAILABLE)
            raise
        except MaestroContextError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise MaestroContextError("CONTEXT_INVALID") from None
        except Exception:
            raise MaestroContextError() from None

    def _status(self, status: int, expected: int) -> None:
        if status in {401, 403, 404, 409, 410}:
            self._client._closed = True
            raise MaestroContextError("ACCESS_REVOKED")
        if status != expected:
            raise MaestroContextError("CONTEXT_LIMIT" if status in {413, 422, 429} else "CONTEXT_UNAVAILABLE")

    async def upload(self, data: bytes, *, filename: str, mime_type: str, ordinal: int) -> dict[str, Any]:
        from .outputs import MAX_PHASE_OUTPUTS  # outputs imports handlers; keep this module import-light

        client = self._client
        try:
            budget = self._operation("artifact.upload", size=len(data) if type(data) is bytes else 0)
            if (
                type(data) is not bytes
                or not 0 < len(data) <= client.config.max_artifact_bytes
                or not artifact_type_allowed(client.config, mime_type, filename)
                or type(ordinal) is not int
                or not 0 <= ordinal < MAX_PHASE_OUTPUTS
            ):
                raise MaestroContextError("CONTEXT_LIMIT")
            envelope = client._envelope
            value = {
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "workspace_id": envelope["workspace_id"],
                "thread_id": envelope["thread_id"],
                "execution_id": envelope["execution_id"],
                "lease_fence": envelope["lease_fence"],
            }
            client._contracts.validate("Public.ArtifactCreateRequest", value)
            # The local parser requires metadata first, without a metadata Content-Type.
            async with asyncio.timeout(budget):
                headers, expires_at = await self._headers(uploading=True)
                headers["Idempotency-Key"] = f"artifact:{envelope['delivery_id']}:{envelope['phase_id']}:{ordinal}"
                files = [
                    ("metadata", (None, json.dumps(value, ensure_ascii=False, separators=(",", ":")))),
                    ("file", (filename, data, mime_type)),
                ]
                headers.update(await client.config.transport_headers(client.config.callback_base_url))
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as http:
                    async with http.stream(
                        "POST", f"{client.config.callback_base_url}/artifacts", headers=headers, files=files
                    ) as response:
                        self._status(response.status_code, 201)
                        if (
                            response.headers.get("content-type", "").split(";")[0].lower() != "application/json"
                            or response.headers.get("content-encoding", "identity") != "identity"
                        ):
                            raise MaestroContextError("CONTEXT_INVALID")
                        raw = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(raw) + len(chunk) > 65536:
                                raise MaestroContextError("CONTEXT_LIMIT")
                            raw.extend(chunk)
                        result = client._contracts.validate("Public.UploadSessionResponse", strict_json(bytes(raw)))
                        artifact = result["artifact"]
                        if (
                            result["upload_mode"] != "multipart_proxy"
                            or artifact["state"] != "ready_unbound"
                            # Maestro stores a canonical MIME (e.g. audio/x-wav -> audio/wav) and
                            # refuses a part whose mime_type differs from it: the part uses the stored one.
                            or any(artifact[key] != value[key] for key in ("filename", "size_bytes", "sha256"))
                        ):
                            raise MaestroContextError("CONTEXT_INVALID")
                        client._check_current()
                        if time.time() >= expires_at:
                            raise MaestroContextError("ACCESS_REVOKED")
                        return {
                            "type": "attachment",
                            "artifact_id": artifact["artifact_id"],
                            "filename": filename,
                            "mime_type": artifact["mime_type"],
                            "size_bytes": len(data),
                        }
        except MaestroContextError:
            raise
        except Exception:
            raise MaestroContextError() from None
