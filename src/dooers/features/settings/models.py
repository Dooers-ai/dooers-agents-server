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
    is_internal: bool = False

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
    is_internal: bool = False


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

    def get_public_fields(self) -> list["SettingsField | SettingsFieldGroup"]:
        result: list[SettingsField | SettingsFieldGroup] = []
        for item in self.fields:
            if isinstance(item, SettingsFieldGroup):
                if item.is_internal:
                    continue
                public_children = [f for f in item.fields if not f.is_internal]
                if public_children:
                    group_copy = item.model_copy(update={"fields": public_children})
                    result.append(group_copy)
            elif not item.is_internal:
                result.append(item)
        return result
