import json

import httpx
import pytest
from pydantic import ValidationError

from dooers.agents.server.features.settings.chat_llm import PublicModelOption, PublicOptionsSchema
from dooers.agents.server.maestro.options_schema import PublicOptionsAdapter

A = "22222222-2222-4222-8222-222222222222"
B = "33333333-3333-4333-8333-333333333333"
R = "44444444-4444-4444-8444-444444444444"
TOKEN = "local_discovery_only_credential_123456789"


def schema(model="model-a", effort="low", version="a-v1"):
    return PublicOptionsSchema(version=version, models=(PublicModelOption(model=model, efforts=(effort,)),))


@pytest.mark.asyncio
async def test_bound_instances_and_secrets_are_separate():
    for instance, declaration in [(A, schema()), (B, schema("model-b", "high", "b-v2"))]:
        adapter = PublicOptionsAdapter(instance_agent_id=instance, runtime_id=R, runtime_version=3,
                                      schema=declaration, resolve_read_token=lambda: TOKEN)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter), base_url="http://test") as client:
            result = await client.get("/maestro/options-schema", headers={"Authorization": "Bearer " + TOKEN})
            assert result.status_code == 200
            assert result.json()["instance_agent_id"] == instance
            assert result.json()["options"] == declaration.model_dump(mode="json")
            assert result.headers["cache-control"] == "private, no-store"
            assert not any(value in result.text for value in (TOKEN, "provider_key", "default_model", "endpoint"))
            assert result.json()["options"]["application_supported"] is False


@pytest.mark.asyncio
async def test_wrong_credential_origin_cookie_query_method_and_rotation_are_denied():
    current = TOKEN
    adapter = PublicOptionsAdapter(instance_agent_id=A, runtime_id=R, runtime_version=3,
                                  schema=schema(), resolve_read_token=lambda: current)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter), base_url="http://test") as client:
        for method, path, headers in [("GET", "/maestro/options-schema", {}),
                                      ("POST", "/maestro/options-schema", {"Authorization": "Bearer " + TOKEN}),
                                      ("GET", "/maestro/options-schema?endpoint=http://evil.test", {"Authorization": "Bearer " + TOKEN}),
                                      ("GET", "/maestro/options-schema", {"Authorization": "Bearer " + TOKEN, "Origin": "http://test"}),
                                      ("GET", "/maestro/options-schema", {"Authorization": "Bearer " + TOKEN, "Cookie": "session=secret"})]:
            result = await client.request(method, path, headers=headers)
            assert result.status_code == 401
            assert result.json() == {"error": "OPTIONS_UNAUTHENTICATED"}
        current = "revoked_credential_".ljust(40, "x")
        assert (await client.get("/maestro/options-schema", headers={"Authorization": "Bearer " + TOKEN})).status_code == 401


def test_strict_explicit_public_schema_never_imports_private_settings():
    for extra in ({"provider_key": "secret"}, {"default_model": "secret"}, {"application_supported": True}):
        with pytest.raises(ValidationError):
            PublicOptionsSchema(version="v1", models=(PublicModelOption(model="a"),), **extra)
    for declaration in [(PublicModelOption(model="a"), PublicModelOption(model="a")), ()]:
        with pytest.raises(ValidationError):
            PublicOptionsSchema(version="v1", models=declaration)
    with pytest.raises(ValidationError):
        PublicModelOption(model="a", efforts=("none",))
    with pytest.raises(ValidationError):
        PublicModelOption(model="a", efforts=("low", "low"))
    assert json.loads(schema().model_dump_json())["models"] == [{"model": "model-a", "efforts": ["low"]}]
