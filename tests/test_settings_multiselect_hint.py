import pytest

from dooers.agents.server.features.settings.models import (
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsSchema,
    SettingsSelectOption,
    normalize_setting_value,
)

MODELS = SettingsField(
    id="composer_models",
    type=SettingsFieldType.MULTISELECT,
    label="Modelos no seletor",
    value=["a"],
    hint="Quais modelos o usuário pode escolher no chat.",
    options=[SettingsSelectOption(value=v, label=v.upper()) for v in ("a", "b", "c")],
)


def test_multiselect_keeps_option_order_and_drops_duplicates():
    assert normalize_setting_value(MODELS, ["c", "a", "c"]) == ["a", "c"]
    assert normalize_setting_value(MODELS, None) == []


@pytest.mark.parametrize("bad", ["a", ["a", 1], ["z"], {"a": True}])
def test_multiselect_refuses_values_that_do_not_fit(bad):
    with pytest.raises(ValueError):
        normalize_setting_value(MODELS, bad)


def test_other_types_pass_through():
    text = SettingsField(id="t", type=SettingsFieldType.TEXT, label="T")
    assert normalize_setting_value(text, "qualquer") == "qualquer"


def test_hint_reaches_the_public_schema():
    schema = SettingsSchema(fields=[SettingsFieldGroup(id="g", label="G", hint="Grupo", fields=[MODELS])])
    dumped = schema.to_public_http_dict()
    group = dumped["fields"][0]
    assert group["hint"] == "Grupo"
    assert group["fields"][0]["type"] == "multiselect"
    assert group["fields"][0]["hint"].startswith("Quais modelos")
