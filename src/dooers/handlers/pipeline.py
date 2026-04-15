from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dooers.exceptions import HandlerError
from dooers.features.analytics.models import AnalyticsEvent
from dooers.features.analytics.agent_analytics import AgentAnalytics
from dooers.features.settings.agent_settings import AgentSettings
from dooers.handlers.context import AgentContext
from dooers.handlers.incoming import AgentIncoming
from dooers.handlers.memory import AgentMemory
from dooers.handlers.send import AgentEvent, AgentSend
from dooers.persistence.base import Persistence
from dooers.protocol.models import (
    AudioPart,
    ContentPart,
    DocumentPart,
    ImagePart,
    Run,
    TextPart,
    Thread,
    ThreadEvent,
    User,
    WireC2S_ContentPart,
    WireS2C_AudioPart,
    WireS2C_ContentPart,
    WireS2C_DocumentPart,
    WireS2C_FormCheckboxElement,
    WireS2C_FormFileElement,
    WireS2C_FormRadioElement,
    WireS2C_FormSelectElement,
    WireS2C_FormTextareaElement,
    WireS2C_FormTextElement,
    WireS2C_ImagePart,
    WireS2C_TextPart,
    deserialize_s2c_part,
)

if TYPE_CHECKING:
    from dooers.features.analytics.collector import AnalyticsCollector
    from dooers.features.settings.broadcaster import SettingsBroadcaster
    from dooers.features.settings.models import SettingsSchema
    from dooers.upload_store import UploadStore

logger = logging.getLogger("agents")


class UploadReferenceError(ValueError):
    """Raised when a ref_id cannot be resolved from the upload store."""


def _generate_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _user_author_display(user: User) -> str | None:
    """Display name for persisted user message events (Cosmos/Postgres `author` field)."""
    if user.user_name and str(user.user_name).strip():
        return str(user.user_name).strip()
    if user.user_email and str(user.user_email).strip():
        return str(user.user_email).strip()
    if user.user_id and str(user.user_id).strip():
        return str(user.user_id).strip()
    return None


Handler = Callable[
    [AgentIncoming, AgentSend, AgentMemory, AgentAnalytics, AgentSettings],
    AsyncGenerator[AgentEvent, None],
]


@dataclass
class HandlerContext:
    handler: Handler
    agent_id: str
    message: str
    organization_id: str = ""
    workspace_id: str = ""
    user: User = None  # type: ignore[assignment]
    thread_id: str | None = None
    thread_title: str | None = None
    content: list[WireC2S_ContentPart | dict[str, Any]] | None = None
    data: dict[str, Any] | None = None
    client_event_id: str | None = None
    event_type: str = "message"

    def __post_init__(self):
        if self.user is None:
            self.user = User(user_id="")


@dataclass
class PipelineResult:
    thread: Thread
    user_event: ThreadEvent
    is_new_thread: bool
    handler_content: list[ContentPart] | None = None


class HandlerPipeline:
    def __init__(
        self,
        persistence: Persistence,
        broadcast_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        analytics_collector: AnalyticsCollector | None = None,
        settings_broadcaster: SettingsBroadcaster | None = None,
        settings_schema: SettingsSchema | None = None,
        assistant_name: str = "Assistant",
        upload_store: UploadStore | None = None,
    ):
        self._persistence = persistence
        self._broadcast_callback = broadcast_callback
        self._analytics_collector = analytics_collector
        self._settings_broadcaster = settings_broadcaster
        self._settings_schema = settings_schema
        self._assistant_name = assistant_name
        self._upload_store = upload_store

    async def setup(self, context: HandlerContext) -> PipelineResult:
        now = _now()
        thread_id = context.thread_id
        is_new_thread = False

        if not thread_id:
            thread_id = _generate_id()
            thread = Thread(
                id=thread_id,
                agent_id=context.agent_id,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                owner=context.user,
                users=[context.user],
                title=context.thread_title,
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            await self._persistence.create_thread(thread)
            is_new_thread = True

            await self._broadcast(
                context.agent_id,
                {
                    "type": "thread.upsert",
                    "thread": thread,
                },
            )

            await self._track_event(
                context.agent_id,
                AnalyticsEvent.THREAD_CREATED.value,
                thread_id=thread_id,
                user_id=context.user.user_id,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
            )
        else:
            thread = await self._persistence.get_thread(thread_id)
            if thread:
                if thread.agent_id != context.agent_id:
                    raise PermissionError(f"Thread {thread_id} belongs to different agent")
            else:
                # Auto-create thread for deterministic IDs (e.g., dispatch with pre-computed thread_id)
                thread = Thread(
                    id=thread_id,
                    agent_id=context.agent_id,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    owner=context.user,
                    users=[context.user],
                    title=context.thread_title,
                    created_at=now,
                    updated_at=now,
                    last_event_at=now,
                )
                await self._persistence.create_thread(thread)
                is_new_thread = True

                await self._broadcast(
                    context.agent_id,
                    {
                        "type": "thread.upsert",
                        "thread": thread,
                    },
                )

                await self._track_event(
                    context.agent_id,
                    AnalyticsEvent.THREAD_CREATED.value,
                    thread_id=thread_id,
                    user_id=context.user.user_id,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                )

        if context.content:
            handler_parts, storage_parts = self._resolve_content_parts(context.content)
        else:
            text = context.message or ""
            handler_parts = [TextPart(text=text)]
            storage_parts = [WireS2C_TextPart(text=text)]

        user_event_id = _generate_id()
        user_event = ThreadEvent(
            id=user_event_id,
            thread_id=thread_id,
            run_id=None,
            type=context.event_type,
            actor="user",
            author=_user_author_display(context.user),
            user=context.user,
            content=storage_parts if context.event_type != "form.response" else None,
            data=context.data,
            created_at=now,
            client_event_id=context.client_event_id,
        )
        await self._persistence.create_event(user_event)

        await self._broadcast(
            context.agent_id,
            {
                "type": "event.append",
                "thread_id": thread_id,
                "events": [user_event],
            },
        )

        return PipelineResult(
            thread=thread,
            user_event=user_event,
            is_new_thread=is_new_thread,
            handler_content=handler_parts,
        )

    async def execute(
        self,
        context: HandlerContext,
        result: PipelineResult,
    ) -> AsyncGenerator[AgentEvent, None]:
        thread_id = result.thread.id
        thread = result.thread

        handler_content = result.handler_content or []
        c2s_content_type = getattr(handler_content[0], "type", "text") if handler_content else "text"
        message = self._extract_message(handler_content)
        agent_context = AgentContext(
            thread_id=thread_id,
            event_id=result.user_event.id,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            user=context.user,
            thread_title=thread.title,
            thread_created_at=thread.created_at,
        )
        incoming = AgentIncoming(
            message=message,
            content=handler_content,
            context=agent_context,
        )

        # Populate form response fields if this is a form.response event
        if result.user_event.type == "form.response" and context.data:
            incoming.form_data = context.data.get("values")
            incoming.form_cancelled = context.data.get("cancelled", False)
            incoming.form_event_id = context.data.get("form_event_id")

        send = AgentSend()
        memory = AgentMemory(thread_id=thread_id, persistence=self._persistence)

        analytics = self._create_analytics(
            agent_id=context.agent_id,
            thread_id=thread_id,
            user_id=context.user.user_id,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
        )

        settings = self._create_settings(agent_id=context.agent_id)

        current_run_id: str | None = None
        current_agent_id: str | None = None

        try:
            async for event in context.handler(incoming, send, memory, analytics, settings):
                event_now = _now()

                if event.send_type == "run_start":
                    current_run_id = _generate_id()
                    current_agent_id = event.data.get("agent_id")
                    run = Run(
                        id=current_run_id,
                        thread_id=thread_id,
                        agent_id=current_agent_id,
                        status="running",
                        started_at=event_now,
                    )
                    await self._persistence.create_run(run)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "run.upsert",
                            "run": run,
                        },
                    )

                    result.user_event.run_id = current_run_id
                    await self._persistence.update_event(result.user_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [result.user_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.MESSAGE_C2S.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=result.user_event.id,
                        data={"type": c2s_content_type},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "run_end":
                    if current_run_id:
                        run = Run(
                            id=current_run_id,
                            thread_id=thread_id,
                            agent_id=current_agent_id,
                            status=event.data.get("status", "succeeded"),
                            started_at=event_now,
                            ended_at=event_now,
                            error=event.data.get("error"),
                        )
                        await self._persistence.update_run(run)
                        await self._broadcast(
                            context.agent_id,
                            {
                                "type": "run.upsert",
                                "run": run,
                            },
                        )
                        current_run_id = None
                        current_agent_id = None

                elif event.send_type == "text":
                    event_id = _generate_id()
                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="message",
                        actor="assistant",
                        author=event.data.get("author") or self._assistant_name,
                        content=[WireS2C_TextPart(text=event.data["text"])],
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.MESSAGE_S2C.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"type": "text"},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "audio":
                    event_id = _generate_id()
                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="message",
                        actor="assistant",
                        author=event.data.get("author") or self._assistant_name,
                        content=[
                            WireS2C_AudioPart(
                                url=event.data["url"],
                                mime_type=event.data.get("mime_type"),
                                duration=event.data.get("duration"),
                            )
                        ],
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.MESSAGE_S2C.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"type": "audio"},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "image":
                    event_id = _generate_id()
                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="message",
                        actor="assistant",
                        author=event.data.get("author") or self._assistant_name,
                        content=[
                            WireS2C_ImagePart(
                                url=event.data["url"],
                                mime_type=event.data.get("mime_type"),
                                alt=event.data.get("alt"),
                            )
                        ],
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.MESSAGE_S2C.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"type": "image"},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "document":
                    event_id = _generate_id()
                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="message",
                        actor="assistant",
                        author=event.data.get("author") or self._assistant_name,
                        content=[
                            WireS2C_DocumentPart(
                                url=event.data["url"],
                                filename=event.data["filename"],
                                mime_type=event.data["mime_type"],
                            )
                        ],
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.MESSAGE_S2C.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"type": "document"},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "form":
                    event_id = _generate_id()
                    elements_raw = event.data.get("elements", [])
                    form_element_models = {
                        "text_input": WireS2C_FormTextElement,
                        "textarea_input": WireS2C_FormTextareaElement,
                        "select_input": WireS2C_FormSelectElement,
                        "radio_input": WireS2C_FormRadioElement,
                        "checkbox_input": WireS2C_FormCheckboxElement,
                        "file_input": WireS2C_FormFileElement,
                    }
                    validated_elements = []
                    for el in elements_raw:
                        el_type = el.get("type", "")
                        model_cls = form_element_models.get(el_type)
                        if model_cls:
                            validated_elements.append(model_cls(**el).model_dump(exclude_unset=True))
                        else:
                            logger.warning("Unknown form element type: %s", el_type)
                            validated_elements.append(el)

                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="form",
                        actor="assistant",
                        author=event.data.get("author") or self._assistant_name,
                        content=None,
                        data={
                            "message": event.data.get("message", ""),
                            "elements": validated_elements,
                            "submit_label": event.data.get("submit_label", "Send"),
                            "cancel_label": event.data.get("cancel_label", "Cancel"),
                            "size": event.data.get("size", "medium"),
                        },
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.MESSAGE_S2C.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"type": "form"},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "tool_call":
                    event_id = _generate_id()
                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="tool.call",
                        actor="assistant",
                        data={
                            "id": event.data["id"],
                            "name": event.data["name"],
                            "display_name": event.data.get("display_name"),
                            "args": event.data["args"],
                        },
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.TOOL_CALLED.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"name": event.data["name"]},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "tool_result":
                    thread_event = ThreadEvent(
                        id=_generate_id(),
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="tool.result",
                        actor="tool",
                        data={
                            "id": event.data.get("id"),
                            "name": event.data["name"],
                            "display_name": event.data.get("display_name"),
                            "args": event.data.get("args"),
                            "result": event.data["result"],
                        },
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )

                elif event.send_type == "tool_transaction":
                    event_id = _generate_id()
                    thread_event = ThreadEvent(
                        id=event_id,
                        thread_id=thread_id,
                        run_id=current_run_id,
                        type="tool.transaction",
                        actor="assistant",
                        data={
                            "id": event.data["id"],
                            "name": event.data["name"],
                            "display_name": event.data.get("display_name"),
                            "args": event.data["args"],
                            "result": event.data["result"],
                        },
                        created_at=event_now,
                    )
                    await self._persistence.create_event(thread_event)
                    await self._broadcast(
                        context.agent_id,
                        {
                            "type": "event.append",
                            "thread_id": thread_id,
                            "events": [thread_event],
                        },
                    )
                    await self._track_event(
                        context.agent_id,
                        AnalyticsEvent.TOOL_CALLED.value,
                        thread_id=thread_id,
                        user_id=context.user.user_id,
                        run_id=current_run_id,
                        event_id=event_id,
                        data={"name": event.data["name"]},
                        organization_id=context.organization_id,
                        workspace_id=context.workspace_id,
                    )

                elif event.send_type == "event_update":
                    target_event_id = event.data["event_id"]
                    existing_event = await self._persistence.get_event(target_event_id)
                    if not existing_event:
                        logger.warning("[agents] event_update: event %s not found", target_event_id)
                    else:
                        existing_event.content = [deserialize_s2c_part(p) for p in event.data["content"]]
                        await self._persistence.update_event(existing_event)
                        await self._broadcast(
                            context.agent_id,
                            {
                                "type": "event.append",
                                "thread_id": existing_event.thread_id,
                                "events": [existing_event],
                            },
                        )

                elif event.send_type == "thread_update":
                    thread = await self._persistence.get_thread(thread_id)
                    if thread:
                        if event.data.get("title") is not None:
                            thread.title = event.data["title"]
                        thread.updated_at = event_now
                        await self._persistence.update_thread(thread)
                        await self._broadcast(
                            context.agent_id,
                            {
                                "type": "thread.upsert",
                                "thread": thread,
                            },
                        )

                if event.send_type != "thread_update":
                    await self._update_thread_last_event(thread_id, event_now)

                yield event

            if result.user_event.run_id is None:
                await self._track_event(
                    context.agent_id,
                    AnalyticsEvent.MESSAGE_C2S.value,
                    thread_id=thread_id,
                    user_id=context.user.user_id,
                    run_id=None,
                    event_id=result.user_event.id,
                    data={"type": c2s_content_type},
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                )

        except Exception as e:
            if result.user_event.run_id is None:
                await self._track_event(
                    context.agent_id,
                    AnalyticsEvent.MESSAGE_C2S.value,
                    thread_id=thread_id,
                    user_id=context.user.user_id,
                    run_id=None,
                    event_id=result.user_event.id,
                    data={"type": c2s_content_type},
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                )

            logger.error("[agents] handler error: %s", e, exc_info=True)

            await self._track_event(
                context.agent_id,
                AnalyticsEvent.ERROR_OCCURRED.value,
                thread_id=thread_id,
                user_id=context.user.user_id,
                run_id=current_run_id,
                data={"error": str(e), "type": type(e).__name__},
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
            )

            error_now = _now()

            if current_run_id:
                run = Run(
                    id=current_run_id,
                    thread_id=thread_id,
                    agent_id=current_agent_id,
                    status="failed",
                    started_at=error_now,
                    ended_at=error_now,
                    error=str(e),
                )
                await self._persistence.update_run(run)
                await self._broadcast(
                    context.agent_id,
                    {
                        "type": "run.upsert",
                        "run": run,
                    },
                )

            error_event = ThreadEvent(
                id=_generate_id(),
                thread_id=thread_id,
                run_id=current_run_id,
                type="message",
                actor="system",
                content=[WireS2C_TextPart(text=str(e))],
                created_at=error_now,
            )
            await self._persistence.create_event(error_event)
            await self._broadcast(
                context.agent_id,
                {
                    "type": "event.append",
                    "thread_id": thread_id,
                    "events": [error_event],
                },
            )
            await self._update_thread_last_event(thread_id, error_now)

            raise HandlerError(str(e), original=e) from e

    async def _broadcast(self, agent_id: str, payload: dict[str, Any]) -> None:
        if self._broadcast_callback:
            await self._broadcast_callback(agent_id, payload)

    async def _track_event(
        self,
        agent_id: str,
        event: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        data: dict[str, Any] | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        if self._analytics_collector:
            await self._analytics_collector.track(
                event=event,
                agent_id=agent_id,
                thread_id=thread_id,
                user_id=user_id,
                run_id=run_id,
                event_id=event_id,
                data=data,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )

    async def _update_thread_last_event(self, thread_id: str, timestamp: datetime) -> None:
        thread = await self._persistence.get_thread(thread_id)
        if thread:
            thread.last_event_at = timestamp
            thread.updated_at = timestamp
            await self._persistence.update_thread(thread)

    def _create_analytics(
        self,
        agent_id: str,
        thread_id: str,
        user_id: str | None,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> AgentAnalytics:
        if self._analytics_collector:
            return AgentAnalytics(
                agent_id=agent_id,
                thread_id=thread_id,
                user_id=user_id,
                run_id=None,
                collector=self._analytics_collector,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )

        class NoopCollector:
            async def track(self, **kwargs) -> None:
                pass

            async def feedback(self, **kwargs) -> None:
                pass

        return AgentAnalytics(
            agent_id=agent_id,
            thread_id=thread_id,
            user_id=user_id,
            run_id=None,
            collector=NoopCollector(),  # type: ignore
            organization_id=organization_id,
            workspace_id=workspace_id,
        )

    def _create_settings(self, agent_id: str) -> AgentSettings:
        if self._settings_schema and self._settings_broadcaster:
            return AgentSettings(
                agent_id=agent_id,
                schema=self._settings_schema,
                persistence=self._persistence,
                broadcaster=self._settings_broadcaster,
            )

        from dooers.features.settings.models import SettingsSchema

        class NoopBroadcaster:
            async def broadcast_snapshot(self, **kwargs) -> None:
                pass

            async def broadcast_patch(self, **kwargs) -> None:
                pass

        class NoopPersistence:
            async def get_settings(self, agent_id: str) -> dict:
                return {}

            async def update_setting(self, agent_id: str, field_id: str, value) -> None:
                pass

            async def set_settings(self, agent_id: str, values: dict) -> None:
                pass

        return AgentSettings(
            agent_id=agent_id,
            schema=SettingsSchema(fields=[]),
            persistence=NoopPersistence(),  # type: ignore
            broadcaster=NoopBroadcaster(),  # type: ignore
        )

    def _resolve_content_parts(
        self, parts: list[WireC2S_ContentPart | dict[str, Any]]
    ) -> tuple[list[ContentPart], list[WireS2C_ContentPart]]:
        """Resolve C2S wire content parts into handler format (with bytes) and
        storage format (metadata only, no bytes).

        Returns (handler_parts, storage_parts).
        """
        handler_parts: list[ContentPart] = []
        storage_parts: list[WireS2C_ContentPart] = []

        for part in parts:
            if hasattr(part, "model_dump"):
                data = part.model_dump()
            elif isinstance(part, dict):
                data = part
            else:
                raise ValueError(f"Unsupported content part: {type(part)}")

            part_type = data.get("type")

            if part_type == "text":
                handler_parts.append(TextPart(text=data["text"]))
                storage_parts.append(WireS2C_TextPart(text=data["text"]))

            elif part_type in ("audio", "image", "document") and "ref_id" in data:
                # WebSocket path — resolve ref_id from upload store
                ref_id = data["ref_id"]
                entry = self._upload_store.consume(ref_id) if self._upload_store else None
                if entry is None:
                    raise UploadReferenceError(f"Upload reference '{ref_id}' not found or expired")

                if part_type == "audio":
                    wire_url = data.get("url")
                    url_str = wire_url.strip() if isinstance(wire_url, str) else None
                    if url_str == "":
                        url_str = None
                    handler_parts.append(
                        AudioPart(
                            data=entry.data,
                            mime_type=entry.mime_type,
                            duration=data.get("duration"),
                            filename=entry.filename,
                            url=url_str,
                        )
                    )
                    storage_parts.append(
                        WireS2C_AudioPart(
                            mime_type=entry.mime_type,
                            duration=data.get("duration"),
                            filename=entry.filename,
                            url=url_str,
                        )
                    )
                elif part_type == "image":
                    wire_url = data.get("url")
                    url_str = wire_url.strip() if isinstance(wire_url, str) else None
                    if url_str == "":
                        url_str = None
                    handler_parts.append(
                        ImagePart(
                            data=entry.data,
                            mime_type=entry.mime_type,
                            filename=entry.filename,
                            url=url_str,
                        )
                    )
                    storage_parts.append(
                        WireS2C_ImagePart(
                            mime_type=entry.mime_type,
                            filename=entry.filename,
                            url=url_str,
                        )
                    )
                elif part_type == "document":
                    wire_url = data.get("url")
                    url_str = wire_url.strip() if isinstance(wire_url, str) else None
                    if url_str == "":
                        url_str = None
                    handler_parts.append(
                        DocumentPart(
                            data=entry.data,
                            mime_type=entry.mime_type,
                            filename=entry.filename,
                            size_bytes=entry.size_bytes,
                            url=url_str,
                        )
                    )
                    storage_parts.append(
                        WireS2C_DocumentPart(
                            mime_type=entry.mime_type,
                            filename=entry.filename,
                            size_bytes=entry.size_bytes,
                            url=url_str,
                        )
                    )

            elif part_type == "audio" and "data" in data:
                # Dispatch path — bytes passed directly
                durl = data.get("url")
                url_str = durl.strip() if isinstance(durl, str) else None
                if url_str == "":
                    url_str = None
                handler_parts.append(
                    AudioPart(
                        data=data["data"],
                        mime_type=data.get("mime_type", ""),
                        duration=data.get("duration"),
                        filename=data.get("filename"),
                        url=url_str,
                    )
                )
                storage_parts.append(
                    WireS2C_AudioPart(
                        mime_type=data.get("mime_type"),
                        duration=data.get("duration"),
                        filename=data.get("filename"),
                        url=url_str,
                    )
                )

            elif part_type == "image" and "data" in data:
                durl = data.get("url")
                url_str = durl.strip() if isinstance(durl, str) else None
                if url_str == "":
                    url_str = None
                handler_parts.append(
                    ImagePart(
                        data=data["data"],
                        mime_type=data.get("mime_type", ""),
                        filename=data.get("filename"),
                        url=url_str,
                    )
                )
                storage_parts.append(
                    WireS2C_ImagePart(
                        mime_type=data.get("mime_type"),
                        filename=data.get("filename"),
                        url=url_str,
                    )
                )

            elif part_type == "document" and "data" in data:
                durl = data.get("url")
                url_str = durl.strip() if isinstance(durl, str) else None
                if url_str == "":
                    url_str = None
                handler_parts.append(
                    DocumentPart(
                        data=data["data"],
                        mime_type=data.get("mime_type", ""),
                        filename=data.get("filename", ""),
                        size_bytes=data.get("size_bytes", 0),
                        url=url_str,
                    )
                )
                storage_parts.append(
                    WireS2C_DocumentPart(
                        mime_type=data.get("mime_type"),
                        filename=data.get("filename"),
                        size_bytes=data.get("size_bytes"),
                        url=url_str,
                    )
                )

            else:
                raise ValueError(f"Unsupported content part type: {part_type!r}")

        return handler_parts, storage_parts

    def _extract_message(self, content: list[ContentPart]) -> str:
        texts = []
        for part in content:
            if isinstance(part, TextPart):
                texts.append(part.text)
        return " ".join(texts)
