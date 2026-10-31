from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from dooers.agents.server.features.analytics.models import AnalyticsEventPayload
from dooers.agents.server.features.memory.models import MemoryPreferences, MemoryRecord
from dooers.agents.server.features.schedules.models import ScheduleRecord
from dooers.agents.server.features.settings.models import SettingsField, SettingsFieldGroup
from dooers.agents.server.protocol.models import (
    ChatContext,
    Run,
    Thread,
    ThreadArtifact,
    ThreadEvent,
    User,
    WireC2S_ContentPart,
)

T = TypeVar("T")


class WSFrame(BaseModel, Generic[T]):
    id: str
    type: str
    payload: T


class ConnectPayload(BaseModel):
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user: User = User(user_id="")
    auth_token: str | None = None
    client: dict | None = None


class ThreadListPayload(BaseModel):
    cursor: str | None = None
    limit: int | None = None
    queue: str | None = None


class ThreadSearchPayload(BaseModel):
    # Length is checked by the handler: a model-level failure would close the socket.
    query: str
    cursor: str | None = None
    limit: int | None = None
    queue: str | None = None


class ThreadSubscribePayload(BaseModel):
    thread_id: str
    after_event_id: str | None = None
    #: Required for elevated (non-participant) read when no same-day in-memory grant exists.
    access_reason: str | None = None


class ThreadUnsubscribePayload(BaseModel):
    thread_id: str


class EventCreateEventPayload(BaseModel):
    type: Literal["message", "form.response"]
    actor: Literal["user"]
    content: list[WireC2S_ContentPart] = []
    data: dict | None = None


class EventCreatePayload(BaseModel):
    thread_id: str | None = None
    client_event_id: str | None = None
    event: EventCreateEventPayload
    metadata: dict[str, Any] | None = None
    chat_context: ChatContext | None = None
    #: Used only when ``thread_id`` is omitted (new thread). Ignored otherwise.
    queue: str | None = None


class C2S_Connect(BaseModel):
    id: str
    type: Literal["connect"]
    payload: ConnectPayload


class C2S_ThreadSearch(BaseModel):
    id: str
    type: Literal["thread.search"]
    payload: ThreadSearchPayload


class C2S_ThreadList(BaseModel):
    id: str
    type: Literal["thread.list"]
    payload: ThreadListPayload


class C2S_ThreadSubscribe(BaseModel):
    id: str
    type: Literal["thread.subscribe"]
    payload: ThreadSubscribePayload


class C2S_ThreadUnsubscribe(BaseModel):
    id: str
    type: Literal["thread.unsubscribe"]
    payload: ThreadUnsubscribePayload


class ThreadDeletePayload(BaseModel):
    thread_id: str


class C2S_ThreadDelete(BaseModel):
    id: str
    type: Literal["thread.delete"]
    payload: ThreadDeletePayload


class ThreadParticipantsAddPayload(BaseModel):
    thread_id: str
    user: User


class C2S_ThreadParticipantsAdd(BaseModel):
    id: str
    type: Literal["thread.participants.add"] = "thread.participants.add"
    payload: ThreadParticipantsAddPayload


class ThreadUpdatePayload(BaseModel):
    thread_id: str
    queue: str | None = None


class C2S_ThreadUpdate(BaseModel):
    id: str
    type: Literal["thread.update"] = "thread.update"
    payload: ThreadUpdatePayload


class C2S_EventCreate(BaseModel):
    id: str
    type: Literal["event.create"]
    payload: EventCreatePayload


class AnalyticsSubscribePayload(BaseModel):
    agent_id: str


class AnalyticsUnsubscribePayload(BaseModel):
    agent_id: str


class FeedbackPayload(BaseModel):
    target_type: Literal["event", "run", "thread"]
    target_id: str
    feedback: Literal["like", "dislike"]
    reason: str | None = None
    classification: str | None = None


class C2S_AnalyticsSubscribe(BaseModel):
    id: str
    type: Literal["analytics.subscribe"]
    payload: AnalyticsSubscribePayload


class C2S_AnalyticsUnsubscribe(BaseModel):
    id: str
    type: Literal["analytics.unsubscribe"]
    payload: AnalyticsUnsubscribePayload


class C2S_Feedback(BaseModel):
    id: str
    type: Literal["feedback"]
    payload: FeedbackPayload


class EventListPayload(BaseModel):
    thread_id: str
    before_event_id: str | None = None
    limit: int = 50


class C2S_EventList(BaseModel):
    id: str
    type: Literal["event.list"] = "event.list"
    payload: EventListPayload


class ThreadArtifactsListPayload(BaseModel):
    thread_id: str
    cursor: str | None = None
    limit: int = 30
    direction: Literal["in", "out", "all"] = "all"


class C2S_ThreadArtifactsList(BaseModel):
    id: str
    type: Literal["thread.artifacts.list"] = "thread.artifacts.list"
    payload: ThreadArtifactsListPayload


class SettingsSubscribePayload(BaseModel):
    agent_id: str
    audience: Literal["creator", "user"] = "user"
    """Which settings surface to load: studio/creator vs runtime user."""

    agent_owner_user_id: str | None = None
    """Platform agent owner (from GET /agents). Required for creator audience when not org admin/manager."""


class SettingsUnsubscribePayload(BaseModel):
    agent_id: str


class SettingsPatchPayload(BaseModel):
    field_id: str
    value: Any


class C2S_SettingsSubscribe(BaseModel):
    id: str
    type: Literal["settings.subscribe"]
    payload: SettingsSubscribePayload


class C2S_SettingsUnsubscribe(BaseModel):
    id: str
    type: Literal["settings.unsubscribe"]
    payload: SettingsUnsubscribePayload


class C2S_SettingsPatch(BaseModel):
    id: str
    type: Literal["settings.patch"]
    payload: SettingsPatchPayload


class SettingsPublicSchemaC2SPayload(BaseModel):
    """Empty payload; public template schema is returned out-of-band."""

    pass


class C2S_SettingsPublicSchema(BaseModel):
    id: str
    type: Literal["settings.public_schema"]
    payload: SettingsPublicSchemaC2SPayload = Field(default_factory=SettingsPublicSchemaC2SPayload)


class SettingsSeedPayload(BaseModel):
    worker_id: str
    values: dict[str, Any] = Field(default_factory=dict)
    seed_secret: str
    """When rotating the runtime key, set to the new secret after verifying `seed_secret` matches the old hash."""
    next_seed_secret: str | None = None
    """If True, shallow-merge ``values`` onto existing worker settings (creator-only sync). If False, replace."""
    merge: bool = False


class C2S_SettingsSeed(BaseModel):
    id: str
    type: Literal["settings.seed"]
    payload: SettingsSeedPayload


class SettingsMergeServiceSecretsPayload(BaseModel):
    worker_id: str
    patch: dict[str, str] = Field(default_factory=dict)
    seed_secret: str
    next_seed_secret: str | None = None


class C2S_SettingsMergeServiceSecrets(BaseModel):
    id: str
    type: Literal["settings.merge_service_secrets"]
    payload: SettingsMergeServiceSecretsPayload


# --- Run control / long-term memory / schedules (0.26.0) ---


class RunCancelPayload(BaseModel):
    thread_id: str
    #: Optional: only stop this run (stale clicks on an old run are ignored).
    run_id: str | None = None


class C2S_RunCancel(BaseModel):
    id: str
    type: Literal["run.cancel"] = "run.cancel"
    payload: RunCancelPayload


class MemoryListPayload(BaseModel):
    scope: Literal["user", "workspace"] | None = None
    query: str | None = None
    cursor: str | None = None
    limit: int | None = None


class C2S_MemoryList(BaseModel):
    id: str
    type: Literal["memory.list"] = "memory.list"
    payload: MemoryListPayload = Field(default_factory=MemoryListPayload)


class MemoryCreatePayload(BaseModel):
    content: str
    scope: Literal["user", "workspace"] = "user"
    kind: str = "fact"
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False


class C2S_MemoryCreate(BaseModel):
    id: str
    type: Literal["memory.create"] = "memory.create"
    payload: MemoryCreatePayload


class MemoryUpdatePayload(BaseModel):
    memory_id: str
    content: str | None = None
    tags: list[str] | None = None
    pinned: bool | None = None
    kind: str | None = None
    #: ``"workspace"`` shares a personal memory with the workspace members (explicit action).
    scope: Literal["workspace"] | None = None


class C2S_MemoryUpdate(BaseModel):
    id: str
    type: Literal["memory.update"] = "memory.update"
    payload: MemoryUpdatePayload


class MemoryDeletePayload(BaseModel):
    memory_id: str


class C2S_MemoryDelete(BaseModel):
    id: str
    type: Literal["memory.delete"] = "memory.delete"
    payload: MemoryDeletePayload


class C2S_MemoryPrefsGet(BaseModel):
    id: str
    type: Literal["memory.prefs.get"] = "memory.prefs.get"
    payload: dict[str, Any] = Field(default_factory=dict)


class MemoryPrefsUpdatePayload(BaseModel):
    enabled: bool | None = None
    custom_instructions: str | None = None
    about_me: str | None = None


class C2S_MemoryPrefsUpdate(BaseModel):
    id: str
    type: Literal["memory.prefs.update"] = "memory.prefs.update"
    payload: MemoryPrefsUpdatePayload


class ScheduleListPayload(BaseModel):
    thread_id: str | None = None
    include_closed: bool = False


class C2S_ScheduleList(BaseModel):
    id: str
    type: Literal["schedule.list"] = "schedule.list"
    payload: ScheduleListPayload = Field(default_factory=ScheduleListPayload)


class ScheduleUpdatePayload(BaseModel):
    schedule_id: str
    status: Literal["active", "paused"]


class C2S_ScheduleUpdate(BaseModel):
    id: str
    type: Literal["schedule.update"] = "schedule.update"
    payload: ScheduleUpdatePayload


class ScheduleDeletePayload(BaseModel):
    schedule_id: str


class C2S_ScheduleDelete(BaseModel):
    id: str
    type: Literal["schedule.delete"] = "schedule.delete"
    payload: ScheduleDeletePayload


class C2S_Ping(BaseModel):
    id: str
    type: Literal["ping"] = "ping"
    payload: dict[str, Any] = Field(default_factory=dict)


ClientToServer = Annotated[
    C2S_Connect
    | C2S_ThreadList
    | C2S_ThreadSearch
    | C2S_ThreadSubscribe
    | C2S_ThreadUnsubscribe
    | C2S_ThreadDelete
    | C2S_ThreadParticipantsAdd
    | C2S_ThreadUpdate
    | C2S_EventCreate
    | C2S_EventList
    | C2S_ThreadArtifactsList
    | C2S_AnalyticsSubscribe
    | C2S_AnalyticsUnsubscribe
    | C2S_Feedback
    | C2S_SettingsSubscribe
    | C2S_SettingsUnsubscribe
    | C2S_SettingsPatch
    | C2S_SettingsPublicSchema
    | C2S_SettingsSeed
    | C2S_SettingsMergeServiceSecrets
    | C2S_RunCancel
    | C2S_MemoryList
    | C2S_MemoryCreate
    | C2S_MemoryUpdate
    | C2S_MemoryDelete
    | C2S_MemoryPrefsGet
    | C2S_MemoryPrefsUpdate
    | C2S_ScheduleList
    | C2S_ScheduleUpdate
    | C2S_ScheduleDelete
    | C2S_Ping,
    Field(discriminator="type"),
]


class AckPayload(BaseModel):
    ack_id: str
    ok: bool
    error: dict | None = None
    server: dict[str, str] | None = None


class ThreadListResultPayload(BaseModel):
    threads: list[Thread]
    cursor: str | None = None
    total_count: int = 0


class ThreadSearchResultPayload(BaseModel):
    query: str
    threads: list[Thread]
    cursor: str | None = None
    total_count: int = 0


class ThreadSnapshotPayload(BaseModel):
    thread: Thread
    events: list[ThreadEvent]
    runs: list[Run] | None = None
    has_more: bool = False


class EventAppendPayload(BaseModel):
    thread_id: str
    events: list[ThreadEvent]


class ThreadUpsertPayload(BaseModel):
    thread: Thread


class RunUpsertPayload(BaseModel):
    run: Run


class S2C_Ack(BaseModel):
    id: str
    type: Literal["ack"] = "ack"
    payload: AckPayload


class S2C_ThreadListResult(BaseModel):
    id: str
    type: Literal["thread.list.result"] = "thread.list.result"
    payload: ThreadListResultPayload


class S2C_ThreadSearchResult(BaseModel):
    id: str
    type: Literal["thread.search.result"] = "thread.search.result"
    payload: ThreadSearchResultPayload


class S2C_ThreadSnapshot(BaseModel):
    id: str
    type: Literal["thread.snapshot"] = "thread.snapshot"
    payload: ThreadSnapshotPayload


class S2C_EventAppend(BaseModel):
    id: str
    type: Literal["event.append"] = "event.append"
    payload: EventAppendPayload


class S2C_ThreadUpsert(BaseModel):
    id: str
    type: Literal["thread.upsert"] = "thread.upsert"
    payload: ThreadUpsertPayload


class EventListResultPayload(BaseModel):
    thread_id: str
    events: list[ThreadEvent]
    cursor: str | None = None
    has_more: bool = False


class S2C_EventListResult(BaseModel):
    id: str
    type: Literal["event.list.result"] = "event.list.result"
    payload: EventListResultPayload


class ThreadArtifactsListResultPayload(BaseModel):
    thread_id: str
    artifacts: list[ThreadArtifact]
    cursor: str | None = None
    has_more: bool = False


class S2C_ThreadArtifactsListResult(BaseModel):
    id: str
    type: Literal["thread.artifacts.list.result"] = "thread.artifacts.list.result"
    payload: ThreadArtifactsListResultPayload


class ThreadDeletedPayload(BaseModel):
    thread_id: str


class S2C_ThreadDeleted(BaseModel):
    id: str
    type: Literal["thread.deleted"] = "thread.deleted"
    payload: ThreadDeletedPayload


class S2C_RunUpsert(BaseModel):
    id: str
    type: Literal["run.upsert"] = "run.upsert"
    payload: RunUpsertPayload


class S2C_AnalyticsEvent(BaseModel):
    id: str
    type: Literal["analytics.event"] = "analytics.event"
    payload: AnalyticsEventPayload


class FeedbackAckPayload(BaseModel):
    target_type: str
    target_id: str
    feedback: str
    ok: bool


class S2C_FeedbackAck(BaseModel):
    id: str
    type: Literal["feedback.ack"] = "feedback.ack"
    payload: FeedbackAckPayload


class SettingsSnapshotPayload(BaseModel):
    agent_id: str
    fields: list[SettingsField | SettingsFieldGroup]
    updated_at: datetime


class S2C_SettingsSnapshot(BaseModel):
    id: str
    type: Literal["settings.snapshot"] = "settings.snapshot"
    payload: SettingsSnapshotPayload


class SettingsPatchBroadcastPayload(BaseModel):
    agent_id: str
    field_id: str
    value: Any
    updated_at: datetime


class S2C_SettingsPatch(BaseModel):
    id: str
    type: Literal["settings.patch"] = "settings.patch"
    payload: SettingsPatchBroadcastPayload


class SettingsPublicSchemaResultPayload(BaseModel):
    model_config = {"populate_by_name": True, "ser_json_by_alias": True}

    schema_: dict[str, Any] = Field(alias="schema")


class S2C_SettingsPublicSchemaResult(BaseModel):
    id: str
    type: Literal["settings.public_schema.result"] = "settings.public_schema.result"
    payload: SettingsPublicSchemaResultPayload


class EventDeltaPayload(BaseModel):
    """Incremental text for an event announced with ``streaming: true`` (append ``delta``)."""

    thread_id: str
    event_id: str
    run_id: str | None = None
    kind: Literal["text", "reasoning"] = "text"
    delta: str
    #: Monotonic per event; clients may drop out-of-order duplicates.
    seq: int = 0


class S2C_EventDelta(BaseModel):
    id: str
    type: Literal["event.delta"] = "event.delta"
    payload: EventDeltaPayload


class MemoryListResultPayload(BaseModel):
    memories: list[MemoryRecord]
    cursor: str | None = None
    has_more: bool = False
    #: False when the agent has no long-term memory store (or the user is a guest).
    available: bool = True
    #: True when this user may create/edit ``workspace`` memories.
    can_manage_workspace: bool = False


class S2C_MemoryListResult(BaseModel):
    id: str
    type: Literal["memory.list.result"] = "memory.list.result"
    payload: MemoryListResultPayload


class MemoryUpsertPayload(BaseModel):
    memory: MemoryRecord


class S2C_MemoryUpsert(BaseModel):
    id: str
    type: Literal["memory.upsert"] = "memory.upsert"
    payload: MemoryUpsertPayload


class MemoryDeletedPayload(BaseModel):
    memory_id: str


class S2C_MemoryDeleted(BaseModel):
    id: str
    type: Literal["memory.deleted"] = "memory.deleted"
    payload: MemoryDeletedPayload


class MemoryPrefsPayload(BaseModel):
    prefs: MemoryPreferences
    available: bool = True


class S2C_MemoryPrefs(BaseModel):
    id: str
    type: Literal["memory.prefs"] = "memory.prefs"
    payload: MemoryPrefsPayload


class ScheduleListResultPayload(BaseModel):
    schedules: list[ScheduleRecord]
    available: bool = True


class S2C_ScheduleListResult(BaseModel):
    id: str
    type: Literal["schedule.list.result"] = "schedule.list.result"
    payload: ScheduleListResultPayload


class ScheduleUpsertPayload(BaseModel):
    schedule: ScheduleRecord


class S2C_ScheduleUpsert(BaseModel):
    id: str
    type: Literal["schedule.upsert"] = "schedule.upsert"
    payload: ScheduleUpsertPayload


class ScheduleDeletedPayload(BaseModel):
    schedule_id: str


class S2C_ScheduleDeleted(BaseModel):
    id: str
    type: Literal["schedule.deleted"] = "schedule.deleted"
    payload: ScheduleDeletedPayload


class S2C_Pong(BaseModel):
    id: str
    type: Literal["pong"] = "pong"
    payload: dict[str, Any] = Field(default_factory=dict)


ServerToClient = (
    S2C_Ack
    | S2C_ThreadListResult
    | S2C_ThreadSearchResult
    | S2C_ThreadSnapshot
    | S2C_EventAppend
    | S2C_EventListResult
    | S2C_ThreadArtifactsListResult
    | S2C_ThreadUpsert
    | S2C_ThreadDeleted
    | S2C_RunUpsert
    | S2C_AnalyticsEvent
    | S2C_FeedbackAck
    | S2C_SettingsSnapshot
    | S2C_SettingsPatch
    | S2C_SettingsPublicSchemaResult
    | S2C_EventDelta
    | S2C_MemoryListResult
    | S2C_MemoryUpsert
    | S2C_MemoryDeleted
    | S2C_MemoryPrefs
    | S2C_ScheduleListResult
    | S2C_ScheduleUpsert
    | S2C_ScheduleDeleted
    | S2C_Pong
)
