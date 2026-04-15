from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Actor = Literal["user", "assistant", "system", "tool"]
EventType = Literal["message", "run.started", "run.finished", "tool.call", "tool.result", "tool.transaction", "form", "form.response"]
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
    url: str | None = Field(
        default=None,
        description="Public HTTP(S) URL when available (e.g. persisted upload); UI can replay, LLM may see link.",
    )


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    data: bytes = b""
    mime_type: str
    width: int | None = None
    height: int | None = None
    filename: str | None = None
    url: str | None = Field(
        default=None,
        description="Public HTTP(S) URL when available; LLM may fetch the image instead of using inline bytes.",
    )


class DocumentPart(BaseModel):
    type: Literal["document"] = "document"
    data: bytes
    mime_type: str
    filename: str
    size_bytes: int
    url: str | None = Field(
        default=None,
        description="Public HTTP(S) URL when available; UI can open/download from thread history.",
    )


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
    url: str | None = Field(
        default=None,
        description="Optional URL from POST /uploads when the attachment was persisted.",
    )


class WireC2S_ImagePart(BaseModel):
    type: Literal["image"] = "image"
    ref_id: str
    url: str | None = Field(
        default=None,
        description="Optional URL from POST /uploads when the attachment was persisted (fetchable HTTP(S)).",
    )


class WireC2S_DocumentPart(BaseModel):
    type: Literal["document"] = "document"
    ref_id: str
    url: str | None = Field(
        default=None,
        description="Optional URL from POST /uploads when the attachment was persisted.",
    )


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

# --- Wire S2C form elements (content parts for form events) ---


class FormOption(BaseModel):
    value: str
    label: str


class WireS2C_FormTextElement(BaseModel):
    type: Literal["text_input"] = "text_input"
    name: str
    label: str
    order: int = 0
    required: bool = False
    disabled: bool = False
    placeholder: str | None = None
    default: str | None = None
    input_type: Literal["text", "password", "email", "number"] = "text"


class WireS2C_FormTextareaElement(BaseModel):
    type: Literal["textarea_input"] = "textarea_input"
    name: str
    label: str
    order: int = 0
    required: bool = False
    disabled: bool = False
    placeholder: str | None = None
    default: str | None = None
    rows: int | None = None


class WireS2C_FormSelectElement(BaseModel):
    type: Literal["select_input"] = "select_input"
    name: str
    label: str
    options: list[FormOption]
    order: int = 0
    required: bool = False
    disabled: bool = False
    default: str | None = None
    placeholder: str | None = None


class WireS2C_FormRadioElement(BaseModel):
    type: Literal["radio_input"] = "radio_input"
    name: str
    label: str
    options: list[FormOption]
    order: int = 0
    required: bool = False
    disabled: bool = False
    default: str | None = None
    variant: Literal["native", "button"] = "native"


class WireS2C_FormCheckboxElement(BaseModel):
    type: Literal["checkbox_input"] = "checkbox_input"
    name: str
    label: str
    options: list[FormOption]
    order: int = 0
    required: bool = False
    disabled: bool = False
    default: list[str] | None = None
    variant: Literal["native", "button"] = "native"


class WireS2C_FormFileElement(BaseModel):
    type: Literal["file_input"] = "file_input"
    name: str
    label: str
    upload_url: str
    order: int = 0
    required: bool = False
    disabled: bool = False
    accept: str | None = None
    multiple: bool = False


WireS2C_FormElement = Annotated[
    WireS2C_FormTextElement
    | WireS2C_FormTextareaElement
    | WireS2C_FormSelectElement
    | WireS2C_FormRadioElement
    | WireS2C_FormCheckboxElement
    | WireS2C_FormFileElement,
    Field(discriminator="type"),
]


_S2C_TYPE_MAP: dict[str, type] = {
    "text": WireS2C_TextPart,
    "audio": WireS2C_AudioPart,
    "image": WireS2C_ImagePart,
    "document": WireS2C_DocumentPart,
}


def deserialize_s2c_part(data: dict) -> WireS2C_ContentPart:
    """Deserialize a dict into a typed WireS2C content part."""
    cls = _S2C_TYPE_MAP.get(data.get("type", ""))
    if cls is None:
        raise ValueError(f"Unknown content part type: {data.get('type')!r}")
    return cls(**data)


class User(BaseModel):
    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    identity_ids: list[str] = []
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
    agent_id: str
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
