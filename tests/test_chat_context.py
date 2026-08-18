from dooers.agents.server.features.settings.chat_llm import (
    LLM_MODELS_FIELD_ID,
    apply_chat_llm_override,
    resolve_chat_llm_override,
)
from dooers.agents.server.features.settings.models import (
    SettingsField,
    SettingsFieldType,
    SettingsSchema,
    SettingsSelectOption,
)
from dooers.agents.server.protocol.frames import EventCreateEventPayload, EventCreatePayload
from dooers.agents.server.protocol.models import ChatContext


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
