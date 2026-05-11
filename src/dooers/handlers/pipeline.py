from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from dooers.config import AgentConfig
from dooers.exceptions import HandlerError, UnsupportedContentTypeError
from dooers.features.analytics.agent_analytics import AgentAnalytics
from dooers.features.analytics.models import AnalyticsEvent
from dooers.features.settings.agent_settings import AgentSettings
from dooers.handlers.content_policy import format_allowed_content_policy_denial
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
from dooers.storage.chat_artifacts import (
    chat_storage_service_ready,
    promote_orphan_chat_artifact_if_present,
    try_fetch_upload_entry_from_blob,
)

if TYPE_CHECKING:
    from dooers.features.analytics.collector import AnalyticsCollector
    from dooers.features.settings.broadcaster import SettingsBroadcaster
    from dooers.features.settings.models import SettingsSchema
    from dooers.upload_store import UploadStore

from dooers.upload_store import UploadEntry

logger = logging.getLogger("agents")


class UploadReferenceError(ValueError):
    """Raised when a ref_id cannot be resolved from the upload store."""


def _generate_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _user_author_display(user: User) -> str | None:
    """Display name for persisted user message events (Cosmos/Postgres `author` field).

    Never falls back to `user_id` — for guests the id is an opaque `guest:<hex>`
    token that is not meaningful to managers viewing the thread. Guest display
    info arrives on the `User` already (enriched from thread metadata upstream).
    """
    if user.user_name and str(user.user_name).strip():
        return str(user.user_name).strip()
    if user.user_email and str(user.user_email).strip():
        return str(user.user_email).strip()
    return None


def _enrich_user_from_metadata(user: User, metadata: dict[str, Any] | None) -> User:
    """Populate missing user_name / user_email from thread metadata.

    Anonymous public-chat visitors supply name/email/phone via the pre-chat
    form, which is persisted as thread metadata. The visitor's `User` object
    (built from the validation webhook) has empty name/email — this helper
    merges the metadata so stored events carry the visitor's display info.
    """
    if not metadata:
        return user
    patches: dict[str, Any] = {}
    if not user.user_name and metadata.get("guest_name"):
        patches["user_name"] = str(metadata["guest_name"])
    if not user.user_email and metadata.get("guest_email"):
        patches["user_email"] = str(metadata["guest_email"])
    if not patches:
        return user
    return user.model_copy(update=patches)


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
    metadata: dict[str, Any] | None = None

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
        allowed_content_types: frozenset[str] | None = None,
        content_policy_denial_message: str | None = None,
        agent_config: AgentConfig | None = None,
    ):
        self._persistence = persistence
        self._broadcast_callback = broadcast_callback
        self._analytics_collector = analytics_collector
        self._settings_broadcaster = settings_broadcaster
        self._settings_schema = settings_schema
        self._assistant_name = assistant_name
        self._upload_store = upload_store
        self._allowed_content_types = allowed_content_types
        self._content_policy_denial_message = (content_policy_denial_message or "").strip() or None
        self._agent_config = agent_config

    async def setup(self, context: HandlerContext) -> PipelineResult:
        now = _now()
        thread_id = context.thread_id
        is_new_thread = False

        if not thread_id:
            thread_id = _generate_id()
            # Enrich with the metadata from this EventCreate (first message on
            # a new thread carries the pre-chat form data). Owner/users get
            # the visitor's display name/email so manager views don't show
            # raw `guest:<hex>` ids.
            enriched_user = _enrich_user_from_metadata(context.user, context.metadata)
            thread = Thread(
                id=thread_id,
                agent_id=context.agent_id,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                owner=enriched_user,
                users=[enriched_user],
                title=context.thread_title,
                metadata=context.metadata,
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            context = replace(context, user=enriched_user)
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
                # Enrich the session user with the thread's stored guest
                # metadata so subsequent messages persist the visitor's
                # display name/email on each event.
                context = replace(context, user=_enrich_user_from_metadata(context.user, thread.metadata))
            else:
                # Auto-create thread for deterministic IDs (e.g., dispatch with pre-computed thread_id)
                enriched_user = _enrich_user_from_metadata(context.user, context.metadata)
                thread = Thread(
                    id=thread_id,
                    agent_id=context.agent_id,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    owner=enriched_user,
                    users=[enriched_user],
                    title=context.thread_title,
                    metadata=context.metadata,
                    created_at=now,
                    updated_at=now,
                    last_event_at=now,
                )
                context = replace(context, user=enriched_user)
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
            handler_parts, storage_parts = await self._resolve_content_parts_async(
                context.content,
                agent_id=context.agent_id,
                thread_id=thread_id,
            )
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
            agent_id=context.agent_id,
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

        policy_denial: str | None = None
        if result.user_event.type == "message":
            policy_denial = format_allowed_content_policy_denial(
                parts=handler_content,
                allowed=self._allowed_content_types,
                message_template=self._content_policy_denial_message,
            )

        async def _handler_event_stream():
            send_local = AgentSend()
            if policy_denial:
                yield send_local.run_start(agent_id=context.agent_id)
                yield send_local.text(policy_denial, author=self._assistant_name)
                yield send_local.run_end(status="failed", error="content_policy")
                return
            async for ev in context.handler(incoming, send, memory, analytics, settings):
                yield ev

        try:
            async for event in _handler_event_stream():
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

    @staticmethod
    def _wire_filename_for_chat_blob_key(data: dict[str, Any], part_type: str) -> str:
        fn = data.get("filename")
        if isinstance(fn, str) and fn.strip():
            return fn.strip()
        if part_type == "image":
            return "image"
        if part_type == "audio":
            return "audio"
        return "file"

    async def _resolve_upload_entry_for_ref(
        self,
        ref_id: str,
        *,
        agent_id: str,
        thread_id: str | None,
        data: dict[str, Any],
        part_type: str,
    ) -> UploadEntry:
        """Prefer in-process upload store; fall back to durable chat artifact blob."""
        entry: UploadEntry | None = None
        if self._upload_store:
            entry = self._upload_store.consume(ref_id)
        if self._agent_config and chat_storage_service_ready(self._agent_config):
            wire_fn = self._wire_filename_for_chat_blob_key(data, part_type)
            mime_raw = data.get("mime_type")
            mime_hint = mime_raw.strip() if isinstance(mime_raw, str) else None
            # Always promote when we have a thread id, even if this replica already
            # resolved bytes from the in-memory upload store. Otherwise the durable
            # object stays only under ``no-thread/`` and later hydration signs a
            # thread-scoped URL for an object that does not exist (GCS still returns
            # a signed URL string without verifying the blob).
            if (thread_id or "").strip():
                await asyncio.to_thread(
                    partial(
                        promote_orphan_chat_artifact_if_present,
                        self._agent_config,
                        agent_id=agent_id,
                        thread_id=thread_id,
                        ref_id=ref_id,
                        filename=wire_fn,
                        mime_hint=mime_hint,
                    )
                )
            if entry is None:
                entry = await asyncio.to_thread(
                    partial(
                        try_fetch_upload_entry_from_blob,
                        self._agent_config,
                        agent_id=agent_id,
                        thread_id=thread_id,
                        ref_id=ref_id,
                        filename=wire_fn,
                        mime_hint=mime_hint,
                    )
                )
        if entry is None:
            raise UploadReferenceError(f"Upload reference '{ref_id}' not found or expired")
        return entry

    async def _resolve_content_parts_async(
        self,
        parts: list[WireC2S_ContentPart | dict[str, Any]],
        *,
        agent_id: str,
        thread_id: str | None,
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

            if part_type == "video":
                raise UnsupportedContentTypeError(
                    "Video attachments are not supported yet. Send text, audio, images, or documents.",
                )

            if part_type == "text":
                handler_parts.append(TextPart(text=data["text"]))
                storage_parts.append(WireS2C_TextPart(text=data["text"]))

            elif part_type in ("audio", "image", "document") and "ref_id" in data:
                # WebSocket path — upload store first, then durable blob (replica / TTL)
                ref_id = data["ref_id"]
                entry = await self._resolve_upload_entry_for_ref(
                    ref_id,
                    agent_id=agent_id,
                    thread_id=thread_id,
                    data=data,
                    part_type=part_type,
                )

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
                            ref_id=ref_id,
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
                            ref_id=ref_id,
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
                            ref_id=ref_id,
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
                raise UnsupportedContentTypeError(
                    f"Unsupported content type {part_type!r}. "
                    "Only text, audio, image, and document are supported.",
                )

        return handler_parts, storage_parts

    def _extract_message(self, content: list[ContentPart]) -> str:
        texts = []
        for part in content:
            if isinstance(part, TextPart):
                texts.append(part.text)
        return " ".join(texts)
