from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, model_validator


class SettingsFieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    SELECT = "select"
    CHECKBOX = "checkbox"
    TEXTAREA = "textarea"
    PASSWORD = "password"
    EMAIL = "email"
    DATE = "date"
    IMAGE = "image"


class SettingsFieldVisibility(StrEnum):
    """Who may receive this field over WebSocket (internal is handler-only)."""

    INTERNAL = "internal"
    CREATOR = "creator"
    USER = "user"


class SettingsSelectOption(BaseModel):
    value: str
    label: str


class SettingsField(BaseModel):
    id: str
    type: SettingsFieldType
    label: str
    required: bool = False
    readonly: bool = False
    value: Any = None
    visibility: SettingsFieldVisibility = SettingsFieldVisibility.USER

    placeholder: str | None = None
    options: list[SettingsSelectOption] | None = None
    min: int | float | None = None
    max: int | float | None = None
    rows: int | None = None
    src: str | None = None
    width: int | None = None
    height: int | None = None


class SettingsFieldGroup(BaseModel):
    id: str
    label: str
    fields: list[SettingsField]
    collapsible: Literal["open", "closed"] | None = None
    visibility: SettingsFieldVisibility = SettingsFieldVisibility.USER


def _collect_all_fields(items: list["SettingsField | SettingsFieldGroup"]) -> list[SettingsField]:
    result: list[SettingsField] = []
    for item in items:
        if isinstance(item, SettingsFieldGroup):
            result.extend(item.fields)
        else:
            result.append(item)
    return result


class SettingsSchema(BaseModel):
    version: str = "1.0"
    fields: list[SettingsField | SettingsFieldGroup]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "SettingsSchema":
        all_fields = _collect_all_fields(self.fields)
        ids = [f.id for f in all_fields]
        if len(ids) != len(set(ids)):
            raise ValueError("Field IDs must be unique")
        return self

    def get_field(self, field_id: str) -> SettingsField | None:
        for item in self.fields:
            if isinstance(item, SettingsFieldGroup):
                for field in item.fields:
                    if field.id == field_id:
                        return field
            elif item.id == field_id:
                return item
        return None

    def get_defaults(self) -> dict[str, Any]:
        return {f.id: f.value for f in _collect_all_fields(self.fields)}

    def get_fields_for_audience(
        self, audience: Literal["creator", "user"]
    ) -> list["SettingsField | SettingsFieldGroup"]:
        """Fields visible to the given WebSocket subscription audience (never internal)."""

        def _field_visible(f: SettingsField) -> bool:
            if f.visibility == SettingsFieldVisibility.INTERNAL:
                return False
            if audience == "creator":
                return f.visibility == SettingsFieldVisibility.CREATOR
            return f.visibility == SettingsFieldVisibility.USER

        result: list[SettingsField | SettingsFieldGroup] = []
        for item in self.fields:
            if isinstance(item, SettingsFieldGroup):
                if item.visibility == SettingsFieldVisibility.INTERNAL:
                    continue
                public_children = [f for f in item.fields if _field_visible(f)]
                if public_children:
                    group_copy = item.model_copy(update={"fields": public_children})
                    result.append(group_copy)
            elif _field_visible(item):
                result.append(item)
        return result

    def get_public_fields(self) -> list["SettingsField | SettingsFieldGroup"]:
        """Deprecated: use get_fields_for_audience('user'). Kept for backward compatibility."""
        return self.get_fields_for_audience("user")
