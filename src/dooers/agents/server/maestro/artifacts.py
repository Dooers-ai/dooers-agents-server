"""Mídia Maestro via proxy autorizado; não usa storage/ref consumível legado."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from .client import MaestroContextClient, MaestroContextError
from .security import strict_json


@dataclass(frozen=True)
class MaestroHydratedArtifact:
    artifact_id: str
    filename: str
    mime_type: str
    data: bytes = field(repr=False)

    def content_part(self):
        from dooers.agents.server.protocol.models import AudioPart, DocumentPart, ImagePart

        if self.mime_type.startswith("audio/"):
            return AudioPart(data=self.data, mime_type=self.mime_type, filename=self.filename)
        if self.mime_type.startswith("image/"):
            return ImagePart(data=self.data, mime_type=self.mime_type, filename=self.filename)
        return DocumentPart(data=self.data, mime_type=self.mime_type, filename=self.filename, size_bytes=len(self.data))


class MaestroArtifactsClient:
    """Compartilha lease/capability/cancelamento do executor, nunca cache de bytes.

    Caminhos são construídos de IDs validados. Metadata do evento não é URL nem
    autoridade; cada GET/POST obtém credencial própria e revalida no Maestro.
    """

    def __init__(self, client: MaestroContextClient) -> None:
        self._client = client

    def _operation(self, operation: str) -> float:
        client = self._client
        if not client.config.artifact_mime_types or operation not in client._claims["operations"]:
            raise MaestroContextError("UNSUPPORTED_CAPABILITY")
        return min(client._check_current(), client.config.artifact_timeout_seconds)

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
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as http:
                    url = f"{client.config.callback_base_url}/artifacts/{reference.artifact_id}/download"
                    async with http.stream("GET", url, headers=headers) as response:
                        self._status(response.status_code, 200)
                        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        length = response.headers.get("content-length", "")
                        if (
                            mime not in client.config.artifact_mime_types
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

    def _status(self, status: int, expected: int) -> None:
        if status in {401, 403, 404, 409, 410}:
            self._client._closed = True
            raise MaestroContextError("ACCESS_REVOKED")
        if status != expected:
            raise MaestroContextError("CONTEXT_LIMIT" if status in {413, 422, 429} else "CONTEXT_UNAVAILABLE")

    async def upload(self, data: bytes, *, filename: str, mime_type: str, ordinal: int) -> dict[str, Any]:
        client = self._client
        try:
            budget = self._operation("artifact.upload")
            if (
                type(data) is not bytes
                or not 0 < len(data) <= client.config.max_artifact_bytes
                or mime_type not in client.config.artifact_mime_types
                or type(ordinal) is not int
                or not 0 <= ordinal < 98
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
                            or any(artifact[key] != value[key] for key in ("filename", "mime_type", "size_bytes", "sha256"))
                        ):
                            raise MaestroContextError("CONTEXT_INVALID")
                        client._check_current()
                        if time.time() >= expires_at:
                            raise MaestroContextError("ACCESS_REVOKED")
                        return {
                            "type": "attachment",
                            "artifact_id": artifact["artifact_id"],
                            "filename": filename,
                            "mime_type": mime_type,
                            "size_bytes": len(data),
                        }
        except MaestroContextError:
            raise
        except Exception:
            raise MaestroContextError() from None
