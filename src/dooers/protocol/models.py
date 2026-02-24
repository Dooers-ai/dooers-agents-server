from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Actor = Literal["user", "assistant", "system", "tool"]
EventType = Literal["message", "run.started", "run.finished", "tool.call", "tool.result", "tool.transaction"]
RunStatus = Literal["running", "succeeded", "failed", "canceled"]


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    url: str
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    alt: str | None = None


class DocumentPart(BaseModel):
    type: Literal["document"] = "document"
    url: str
    filename: str
    mime_type: str
    size_bytes: int | None = None


ContentPart = Annotated[
    TextPart | ImagePart | DocumentPart,
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
    content: list[ContentPart] | None = None
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
