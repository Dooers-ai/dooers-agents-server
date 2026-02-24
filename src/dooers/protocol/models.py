from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Actor = Literal["user", "assistant", "system", "tool"]
EventType = Literal["message", "run.started", "run.finished", "tool.call", "tool.result", "tool.transaction"]
RunStatus = Literal["running", "succeeded", "failed", "canceled"]


# --- Handler format (what the agent developer receives in incoming.content) ---


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class AudioPart(BaseModel):
    type: Literal["audio"] = "audio"
    data: bytes
    mime_type: str
    duration: float | None = None
    filename: str | None = None


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    data: bytes
    mime_type: str
    width: int | None = None
    height: int | None = None
    filename: str | None = None


class DocumentPart(BaseModel):
    type: Literal["document"] = "document"
    data: bytes
    mime_type: str
    filename: str
    size_bytes: int


ContentPart = Annotated[
    TextPart | AudioPart | ImagePart | DocumentPart,
    Field(discriminator="type"),
]


# --- Wire C2S format (ref_id from client uploads) ---


class WireC2S_TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class WireC2S_AudioPart(BaseModel):
    type: Literal["audio"] = "audio"
    ref_id: str
    duration: float | None = None


class WireC2S_ImagePart(BaseModel):
    type: Literal["image"] = "image"
    ref_id: str


class WireC2S_DocumentPart(BaseModel):
    type: Literal["document"] = "document"
    ref_id: str


WireC2S_ContentPart = Annotated[
    WireC2S_TextPart | WireC2S_AudioPart | WireC2S_ImagePart | WireC2S_DocumentPart,
    Field(discriminator="type"),
]


# --- Wire S2C format (URLs/metadata for client display) ---


class WireS2C_TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class WireS2C_AudioPart(BaseModel):
    type: Literal["audio"] = "audio"
    url: str | None = None
    mime_type: str | None = None
    duration: float | None = None
    filename: str | None = None


class WireS2C_ImagePart(BaseModel):
    type: Literal["image"] = "image"
    url: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    alt: str | None = None
    filename: str | None = None


class WireS2C_DocumentPart(BaseModel):
    type: Literal["document"] = "document"
    url: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


WireS2C_ContentPart = Annotated[
    WireS2C_TextPart | WireS2C_AudioPart | WireS2C_ImagePart | WireS2C_DocumentPart,
    Field(discriminator="type"),
]


class User(BaseModel):
    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    system_role: str = "user"
    organization_role: str = "member"
    workspace_role: str = "member"


class ThreadEvent(BaseModel):
    id: str
    thread_id: str
    run_id: str | None = None
    type: EventType
    actor: Actor
    author: str | None = None
    user: User = User(user_id="")
    content: list[WireS2C_ContentPart] | None = None
    data: dict | None = None
    created_at: datetime
    streaming: bool | None = None
    finalized: bool | None = None
    client_event_id: str | None = None


class Thread(BaseModel):
    id: str
    worker_id: str
    organization_id: str = ""
    workspace_id: str = ""
    owner: User = User(user_id="")
    users: list[User] = []
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    last_event_at: datetime


class Run(BaseModel):
    id: str
    thread_id: str
    agent_id: str | None = None
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None = None
    error: str | None = None
