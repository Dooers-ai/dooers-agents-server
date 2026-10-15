"""Authenticated read-only discovery; no settings write or handler execution."""
from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable
from uuid import UUID

from dooers.agents.server.features.settings.chat_llm import PublicOptionsSchema


class PublicOptionsAdapter:
    """One adapter per logical instance/runtime. Mount explicitly in local runtime.

    The resolver reads the current *discovery-only* service credential. It is not
    a provider key, dispatch bearer or end-user credential; rotation takes effect
    on the next read. Authorizing reads does not enable per-turn application.
    """

    def __init__(self, *, instance_agent_id: str, runtime_id: str, runtime_version: int,
                 schema: PublicOptionsSchema, resolve_read_token: Callable[[], str | None],
                 path: str = "/maestro/options-schema"):
        if any(str(UUID(value)) != value for value in (instance_agent_id, runtime_id)):
            raise ValueError("Invalid options binding")
        if type(runtime_version) is not int or not 1 <= runtime_version <= 9007199254740991:
            raise ValueError("Invalid runtime version")
        if (
            not re.fullmatch(r"/(?:api/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/)?maestro/options-schema", path)
            or not isinstance(schema, PublicOptionsSchema)
            or not callable(resolve_read_token)
        ):
            raise ValueError("Invalid options configuration")
        self.instance_agent_id, self.runtime_id, self.runtime_version = instance_agent_id, runtime_id, runtime_version
        self.schema, self.resolve_read_token, self.path = schema.model_copy(deep=True), resolve_read_token, path

    async def __call__(self, scope, receive, send):
        status, result = 401, {"error": "OPTIONS_UNAUTHENTICATED"}
        try:
            headers = {}
            for name, value in scope.get("headers", []):
                name = name.lower()
                if name in headers or name in {b"cookie", b"origin", b"content-encoding"}:
                    raise ValueError()
                headers[name] = value.decode("ascii")
            token = self.resolve_read_token()
            candidate = headers.get(b"authorization", "")
            if (scope.get("type") != "http" or scope.get("method") != "GET"
                or scope.get("path") != self.path or scope.get("query_string")
                or not isinstance(token, str) or not 32 <= len(token) <= 512
                or not hmac.compare_digest(candidate, "Bearer " + token)):
                raise ValueError()
            result = {"instance_agent_id": self.instance_agent_id, "runtime_id": self.runtime_id,
                      "runtime_version": self.runtime_version, "options": self.schema.model_dump(mode="json")}
            if self.resolve_read_token() != token:
                raise ValueError()
            status = 200
        except Exception:
            result = {"error": "OPTIONS_UNAUTHENTICATED"}
        encoded = json.dumps(result, separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"private, no-store")]})
        await send({"type": "http.response.body", "body": encoded})
