"""Explicit sandbox HTTPS transport. Application credentials remain separate from IAM."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

PROJECT = "sandbox-499421"
_HOST = re.compile(r"[a-z][a-z0-9-]*-68544666733\.southamerica-east1\.run\.app")
_METADATA = "http://169.254.169.254/computeMetadata/v1/"


class MaestroRemoteError(ValueError):
    def __init__(self):
        super().__init__("MAESTRO_REMOTE_TRANSPORT_UNAVAILABLE")


def sandbox_url(value: str, *, origin_only: bool = False) -> str:
    try:
        if not isinstance(value, str) or not value.isascii():
            raise ValueError
        u = urlsplit(value)
        origin = f"https://{u.hostname}"
        if (
            u.scheme != "https"
            or not _HOST.fullmatch(u.hostname or "")
            or u.netloc != u.hostname
            or u.query
            or u.fragment
            or u.geturl() != value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or (origin_only and value != origin)
            or (u.path and not re.fullmatch(r"/[A-Za-z0-9_./-]*", u.path))
            or "//" in u.path
            or any(part in {".", ".."} for part in u.path.split("/"))
        ):
            raise ValueError
        return origin
    except (TypeError, ValueError):
        raise MaestroRemoteError() from None


def _validate_token(token: str, audience: str) -> str:
    try:
        if not isinstance(token, str) or len(token) > 16384 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ValueError
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if (
            claims.get("aud") != audience
            or type(claims.get("exp")) not in {int, float}
            or not time.time() + 10 < claims["exp"] <= time.time() + 3700
        ):
            raise ValueError
        return token
    except Exception:
        raise MaestroRemoteError() from None


@dataclass(frozen=True)
class MaestroRemoteTransport:
    allowed_origins: tuple[str, ...]
    id_token_resolver: Callable[[str], Awaitable[str]] = field(repr=False)
    project_id: str = PROJECT

    def __post_init__(self):
        if (
            self.project_id != PROJECT
            or type(self.allowed_origins) is not tuple
            or not self.allowed_origins
            or not callable(self.id_token_resolver)
        ):
            raise MaestroRemoteError()
        for origin in self.allowed_origins:
            sandbox_url(origin, origin_only=True)

    def validate_url(self, value: str) -> str:
        origin = sandbox_url(value)
        if origin not in self.allowed_origins:
            raise MaestroRemoteError()
        return origin

    async def headers(self, value: str) -> dict[str, str]:
        origin = self.validate_url(value)
        try:
            async with asyncio.timeout(3):
                token = _validate_token(await self.id_token_resolver(origin), origin)
            return {"X-Serverless-Authorization": f"Bearer {token}"}
        except Exception:
            raise MaestroRemoteError() from None


def cloud_run_transport(allowed_origins: tuple[str, ...], *, service_name: str) -> MaestroRemoteTransport:
    """Metadata-only workload identity. No personal credentials, ADC files or CLI fallback."""
    if (
        not re.fullmatch(r"maestro-agent-[a-z0-9-]+", service_name)
        or os.environ.get("K_SERVICE") != service_name
        or os.environ.get("GOOGLE_CLOUD_PROJECT") != PROJECT
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("PUBSUB_EMULATOR_HOST")
    ):
        raise MaestroRemoteError()

    async def token(audience: str) -> str:
        sandbox_url(audience, origin_only=True)
        try:
            async with asyncio.timeout(3), httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:

                async def read(path: str) -> str:
                    async with client.stream("GET", _METADATA + path, headers={"Metadata-Flavor": "Google"}) as response:
                        if response.status_code != 200 or response.headers.get("metadata-flavor") != "Google":
                            raise MaestroRemoteError()
                        data = bytearray()
                        async for chunk in response.aiter_raw():
                            data.extend(chunk)
                            if len(data) > 16384:
                                raise MaestroRemoteError()
                        return data.decode("utf-8", errors="strict")

                # Sequential reads remain under one shared deadline and are cancelled together.
                project = await read("project/project-id")
                email = await read("instance/service-accounts/default/email")
                if project.strip() != PROJECT or email.strip() != f"{service_name}@{PROJECT}.iam.gserviceaccount.com":
                    raise MaestroRemoteError()
                return await read("instance/service-accounts/default/identity?" + urlencode({"audience": audience, "format": "full"}))
        except Exception:
            raise MaestroRemoteError() from None

    return MaestroRemoteTransport(allowed_origins, token)
