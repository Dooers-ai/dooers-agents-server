from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from dooers.features.analytics.models import AnalyticsEventPayload
from dooers.features.settings.models import SettingsField, SettingsFieldGroup
from dooers.protocol.models import Run, Thread, ThreadEvent, User, WireC2S_ContentPart

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


class ThreadSubscribePayload(BaseModel):
    thread_id: str
    after_event_id: str | None = None


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


class C2S_Connect(BaseModel):
    id: str
    type: Literal["connect"]
    payload: ConnectPayload


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


class C2S_SettingsSeed(BaseModel):
    id: str
    type: Literal["settings.seed"]
    payload: SettingsSeedPayload


ClientToServer = Annotated[
    C2S_Connect
    | C2S_ThreadList
    | C2S_ThreadSubscribe
    | C2S_ThreadUnsubscribe
    | C2S_ThreadDelete
    | C2S_EventCreate
    | C2S_EventList
    | C2S_AnalyticsSubscribe
    | C2S_AnalyticsUnsubscribe
    | C2S_Feedback
    | C2S_SettingsSubscribe
    | C2S_SettingsUnsubscribe
    | C2S_SettingsPatch
    | C2S_SettingsPublicSchema
    | C2S_SettingsSeed,
    Field(discriminator="type"),
]


class AckPayload(BaseModel):
    ack_id: str
    ok: bool
    error: dict | None = None


class ThreadListResultPayload(BaseModel):
    threads: list[Thread]
    cursor: str | None = None
    total_count: int = 0


class ThreadSnapshotPayload(BaseModel):
    thread: Thread
    events: list[ThreadEvent]
    runs: list[Run] | None = None


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
    """Same shape as the former HTTP GET /settings/schema body."""

    schema: dict[str, Any]


class S2C_SettingsPublicSchemaResult(BaseModel):
    id: str
    type: Literal["settings.public_schema.result"] = "settings.public_schema.result"
    payload: SettingsPublicSchemaResultPayload


ServerToClient = (
    S2C_Ack
    | S2C_ThreadListResult
    | S2C_ThreadSnapshot
    | S2C_EventAppend
    | S2C_EventListResult
    | S2C_ThreadUpsert
    | S2C_ThreadDeleted
    | S2C_RunUpsert
    | S2C_AnalyticsEvent
    | S2C_FeedbackAck
    | S2C_SettingsSnapshot
    | S2C_SettingsPatch
    | S2C_SettingsPublicSchemaResult
)
