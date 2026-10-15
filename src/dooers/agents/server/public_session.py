"""Revalidate public identities before accepting another frame; no private-session policy change."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PublicSessionBinding:
    token: str = field(repr=False)
    agent_id: str
    user_id: str
    organization_id: str
    workspace_id: str
    connection_type: str


async def public_session_current(binding: PublicSessionBinding, validator: Any, timeout: float = 5.0) -> bool:
    """Fail closed on missing/slow/dead authority or a changed canonical identity/scope."""
    if validator is None or not binding.token or binding.connection_type not in {"guest", "public_authenticated"}:
        return False
    try:
        result = await asyncio.wait_for(
            validator.validate(
                auth_token=binding.token,
                agent_id=binding.agent_id,
                guest_user_id="",
                organization_id=binding.organization_id,
                workspace_id=binding.workspace_id,
                user_id=None,
            ),
            timeout=timeout,
        )
        return bool(
            result.valid
            and result.user is not None
            and result.connection_type == binding.connection_type
            and result.user.user_id == binding.user_id
            and result.user.connection_type == binding.connection_type
            and result.agent_id == binding.agent_id
            and result.organization_id == binding.organization_id
            and result.workspace_id == binding.workspace_id
        )
    except Exception:
        return False


class PublicSessionSocket:
    """Registry broadcasts check the same current authority before writing bytes.

    This adds no timer/task, so an idle public socket is rechecked by the actual
    outgoing event rather than leaking it until the visitor sends another frame.
    """

    def __init__(self, native: Any, current: Callable[[], Awaitable[bool]], reject: Callable[[], Awaitable[None]]):
        self._native = native
        self._current = current
        self._reject = reject

    async def send_text(self, data: str) -> None:
        if not await self._current():
            await self._reject()
            # ConnectionRegistry counts this as a denied send, not delivery.
            raise PermissionError("PUBLIC_SESSION_REJECTED")
        await self._native.send_text(data)

    async def receive_text(self) -> str:
        return await self._native.receive_text()
