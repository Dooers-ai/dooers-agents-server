from dooers.agents.server.features.settings.chat_llm import (
    LLM_MODELS_FIELD_ID,
    apply_chat_llm_override,
    resolve_chat_llm_override,
    resolve_chat_reasoning_effort,
)
from dooers.agents.server.features.settings.models import (
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsSchema,
    SettingsSelectOption,
)
from dooers.agents.server.protocol.frames import (
    EventCreateEventPayload,
    EventCreatePayload,
    S2C_SettingsPublicSchemaResult,
    SettingsPublicSchemaResultPayload,
)
from dooers.agents.server.protocol.models import ChatContext
from dooers.agents.server.protocol.parser import serialize_frame


def _schema() -> SettingsSchema:
    return SettingsSchema(
        fields=[
            SettingsField(
                id=LLM_MODELS_FIELD_ID,
                type=SettingsFieldType.SELECT,
                label="Models",
                value="gpt-4o",
                options=[
                    SettingsSelectOption(value="gpt-4o", label="GPT-4o"),
                    SettingsSelectOption(value="gpt-5.4-mini", label="GPT-5.4 mini"),
                ],
            )
        ]
    )


def test_event_create_payload_parses_chat_context() -> None:
    payload = EventCreatePayload(
        thread_id="t1",
        event=EventCreateEventPayload(type="message", actor="user", content=[]),
        chat_context={"llm_model": "gpt-4o"},
    )
    assert payload.chat_context is not None
    assert payload.chat_context.llm_model == "gpt-4o"


def test_resolve_chat_llm_override_allowlist() -> None:
    schema = _schema()
    ctx = ChatContext(llm_model="gpt-5.4-mini")
    assert resolve_chat_llm_override(ctx, schema=schema) == "gpt-5.4-mini"
    assert resolve_chat_llm_override(ChatContext(llm_model="unknown"), schema=schema) is None
    assert resolve_chat_llm_override(None, schema=schema) is None


def test_apply_chat_llm_override_replaces_llm_model() -> None:
    schema = _schema()
    values = {"llm_model": "gpt-4o", "persona": "x"}
    out = apply_chat_llm_override(
        values,
        chat_context=ChatContext(llm_model="gpt-5.4-mini"),
        schema=schema,
    )
    assert out["llm_model"] == "gpt-5.4-mini"
    assert out["persona"] == "x"
    assert values["llm_model"] == "gpt-4o"


def test_apply_chat_llm_override_copies_reasoning_effort() -> None:
    out = apply_chat_llm_override(
        {"llm_model": "gpt-4o"},
        chat_context=ChatContext(llm_model="gpt-5.4-mini", reasoning_effort="high"),
        schema=_schema(),
    )
    assert out["llm_model"] == "gpt-5.4-mini"
    assert out["reasoning_effort"] == "high"
    assert resolve_chat_reasoning_effort(ChatContext(reasoning_effort="nope")) is None


def test_public_schema_result_wires_schema_alias() -> None:
    llm_field = _schema().fields[0]
    assert isinstance(llm_field, SettingsField)
    nested = SettingsSchema(
        fields=[
            SettingsFieldGroup(id="llm", label="Models", fields=[llm_field]),
        ]
    )
    frame = S2C_SettingsPublicSchemaResult(
        id="req-1",
        payload=SettingsPublicSchemaResultPayload(
            schema=nested.to_public_http_dict(),
        ),
    )
    raw = serialize_frame(frame)
    assert '"schema_":' not in raw
    assert '"schema":' in raw
    assert "llm_models" in raw
