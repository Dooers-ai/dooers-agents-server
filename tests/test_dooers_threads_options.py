"""Real signed admission/context HTTP with fake provider, never paid traffic."""

import asyncio
import time

import pytest

from dooers.agents.server.dooers_threads.callbacks import DooersThreadsCallbackError
from dooers.agents.server.dooers_threads.context import DooersThreadsPrivateResources, current_dooers_threads_context
from dooers.agents.server.dooers_threads.executor import DooersThreadsTextExecutor
from dooers.agents.server.dooers_threads.recipient_options import DooersThreadsOptionsRegistration
from dooers.agents.server.features.settings.chat_llm import PublicModelOption, PublicOptionsSchema
from dooers.agents.server.protocol.dooers_threads import default_contracts
from tests.test_dooers_threads_executor import setup


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return default_contracts()


class Settings:
    schema = object()

    async def get_all(self):
        return {"llm_model": "private-default", "reasoning_effort": "medium"}

    async def get(self, name):
        return (await self.get_all())[name]

    async def set(self, name, value):
        raise AssertionError("Provider options must not be persisted")


@pytest.mark.parametrize("model,effort", [("model-a", "minimal"), ("model-b", "max")])
async def test_executor_applies_original_recipient_options_before_fake_provider(private_key, contracts, model, effort):
    async with setup(private_key, contracts) as fixture:
        recipient = fixture.admission.envelope["recipient_participant_id"]
        fixture.response["trigger_event"]["payload"]["agent_options"] = [
            {"participant_id": recipient, "model": model, "effort": effort},
            {"participant_id": "00000000-0000-4000-8000-000000000099", "model": "sibling-model", "effort": "high"},
        ]
        schema = PublicOptionsSchema(version="registered-v1", models=(PublicModelOption(model=model, efforts=(effort,)),))
        valid_until = time.time() + 60
        provider_calls = []
        private_settings = Settings()

        async def resolver(scope):
            return DooersThreadsPrivateResources(scope, {}, private_settings, {})

        async def handler(on, send, memory, analytics, settings):
            values = await settings.get_all()
            assert values == {"llm_model": model, "reasoning_effort": effort}
            assert on.context.chat_context.llm_model == model
            assert on.context.chat_context.reasoning_effort == effort
            provider_calls.append(values)
            yield send.text("fake-provider-result")

        result = await DooersThreadsTextExecutor(
            fixture.config,
            handler,
            resolver,
            contracts=contracts,
            options_resolver=lambda: DooersThreadsOptionsRegistration(
                **{
                    field: fixture.admission.envelope[field]
                    for field in ("organization_id", "workspace_id", "instance_agent_id", "runtime_id")
                },
                generation="local-v1",
                valid_until=valid_until,
                schema=schema,
            ),
        ).execute(fixture.admission, fixture.lease)
        assert result.confirmed
        assert len(provider_calls) == 1
        assert (await private_settings.get_all())["llm_model"] == "private-default"
        assert current_dooers_threads_context() is None
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "dooers-threads-heartbeat"]


@pytest.mark.parametrize("bad_model,bad_effort", [("other-model", "low"), ("model-a", "max")])
async def test_executor_rejects_unsupported_options_before_handler_and_provider(private_key, contracts, bad_model, bad_effort):
    async with setup(private_key, contracts) as fixture:
        recipient = fixture.admission.envelope["recipient_participant_id"]
        fixture.response["trigger_event"]["payload"]["agent_options"] = [
            {"participant_id": recipient, "model": bad_model, "effort": bad_effort}
        ]
        schema = PublicOptionsSchema(version="registered-v1", models=(PublicModelOption(model="model-a", efforts=("low",)),))
        valid_until = time.time() + 60
        provider_calls, resolutions = [], []

        async def resolver(scope):
            resolutions.append(scope)
            return DooersThreadsPrivateResources(scope, {}, Settings(), {})

        async def handler(*args):
            provider_calls.append(args)

        with pytest.raises(DooersThreadsCallbackError) as failure:
            await DooersThreadsTextExecutor(
                fixture.config,
                handler,
                resolver,
                contracts=contracts,
                options_resolver=lambda: DooersThreadsOptionsRegistration(
                    **{
                        field: fixture.admission.envelope[field]
                        for field in ("organization_id", "workspace_id", "instance_agent_id", "runtime_id")
                    },
                    generation="local-v1",
                    valid_until=valid_until,
                    schema=schema,
                ),
            ).execute(fixture.admission, fixture.lease)
        assert failure.value.code == "UNSUPPORTED_CAPABILITY"
        assert provider_calls == resolutions == []
        assert fixture.outputs == {} and fixture.completions == []
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "dooers-threads-heartbeat"]


@pytest.mark.parametrize("change", ["removed", "runtime", "generation"])
async def test_current_registration_change_during_private_resolution_denies_provider(private_key, contracts, change):
    from dataclasses import replace

    async with setup(private_key, contracts) as fixture:
        envelope = fixture.admission.envelope
        fixture.response["trigger_event"]["payload"]["agent_options"] = [
            {"participant_id": envelope["recipient_participant_id"], "model": "model-a", "effort": "low"}
        ]
        schema = PublicOptionsSchema(version="registered-v1", models=(PublicModelOption(model="model-a", efforts=("low",)),))
        registered = DooersThreadsOptionsRegistration(
            **{field: envelope[field] for field in ("organization_id", "workspace_id", "instance_agent_id", "runtime_id")},
            generation="local-v1",
            valid_until=time.time() + 60,
            schema=schema,
        )
        state, provider_calls = [registered], []

        async def resolver(scope):
            state[0] = (
                None
                if change == "removed"
                else replace(
                    registered,
                    **({"runtime_id": "00000000-0000-4000-8000-000000000099"} if change == "runtime" else {"generation": "local-v2"}),
                )
            )
            await asyncio.sleep(0)
            return DooersThreadsPrivateResources(scope, {}, Settings(), {})

        async def handler(*args):
            provider_calls.append(args)

        with pytest.raises(DooersThreadsCallbackError):
            await DooersThreadsTextExecutor(
                fixture.config, handler, resolver, contracts=contracts, options_resolver=lambda: state[0]
            ).execute(fixture.admission, fixture.lease)
        assert provider_calls == []
        assert fixture.outputs == {} and fixture.completions == []
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "dooers-threads-heartbeat"]
