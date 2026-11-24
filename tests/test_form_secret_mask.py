"""Password fields of a form response are masked before the event is stored or broadcast."""

from types import SimpleNamespace

import pytest

from dooers.agents.server.handlers import pipeline as pipeline_module

FORM = SimpleNamespace(
    data={
        "message": "Chave da API",
        "elements": [
            {"type": "text", "name": "api_key", "input_type": "password"},
            {"type": "text", "name": "label", "input_type": "text"},
        ],
    }
)


class _Persistence:
    async def get_event(self, event_id):
        return FORM if event_id == "form-1" else None


def _pipeline():
    cls = next(v for k, v in vars(pipeline_module).items() if isinstance(v, type) and hasattr(v, "_mask_secret_form_values"))
    obj = cls.__new__(cls)
    obj._persistence = _Persistence()
    return obj


@pytest.mark.asyncio
async def test_password_values_are_masked_others_kept():
    data = {"form_event_id": "form-1", "values": {"api_key": "sk-live-123", "label": "GitHub"}}
    stored = await _pipeline()._mask_secret_form_values(data)
    assert stored["values"] == {"api_key": pipeline_module.SECRET_MASK, "label": "GitHub"}
    assert data["values"]["api_key"] == "sk-live-123"  # the handler still gets the real value


@pytest.mark.asyncio
async def test_field_types_come_from_the_stored_form_not_the_client():
    data = {"form_event_id": "unknown", "values": {"api_key": "sk"}}
    assert await _pipeline()._mask_secret_form_values(data) == data
    assert await _pipeline()._mask_secret_form_values({"values": {"api_key": "x"}}) == {"values": {"api_key": "x"}}
