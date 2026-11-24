"""Attachments sweep (F5, R1): the SDK in Dooers Threads mode hands the handler what production hands it.

Production (0.25.2, legacy path): ``POST /uploads`` accepts any file up to ``AgentConfig.upload_max_size_bytes``
(25 MiB), the handler receives ``part.data`` with no fixed time limit, and a failure before the handler shows the
error text in the thread (``pipeline.py``). Before this fix, Dooers Threads mode capped one message at 16 MiB, cut every
download after 5 s, refused ``.doc``/``.zip``/``.exe`` and ended the execution ``runtime_rejected`` in silence.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.dooers_threads.artifacts import (
    DooersThreadsArtifactsClient,
    DooersThreadsHydratedArtifact,
    artifact_type_allowed,
    attachment_unavailable_text,
    media_url_upload_allowed,
)
from dooers.agents.server.dooers_threads.client import DooersThreadsContextError
from dooers.agents.server.dooers_threads.config import DooersThreadsConfig, DooersThreadsConfigurationError
from dooers.agents.server.dooers_threads.links import LinkResolver
from dooers.agents.server.protocol.models import AudioPart, DocumentPart, ImagePart
from dooers.agents.server.upload_store import DEFAULT_MAX_SIZE
from tests.test_dooers_threads_context import config_for
from tests.test_dooers_threads_context import contracts as contracts
from tests.test_dooers_threads_context import private_key as private_key
from tests.test_dooers_threads_executor import execute_fixture, setup
from tests.test_dooers_threads_sink_parity import published

MIB = 1024 * 1024


def default(cls, name):
    return next(f.default for f in dataclasses.fields(cls) if f.name == name)


def test_file_limit_is_production_upload_limit_and_no_message_total():
    assert DEFAULT_MAX_SIZE == 25 * MIB
    assert default(AgentConfig, "upload_max_size_bytes") == DEFAULT_MAX_SIZE
    assert default(DooersThreadsConfig, "max_artifact_bytes") == DEFAULT_MAX_SIZE
    assert default(DooersThreadsConfig, "max_hydrated_bytes") is None


def test_file_limit_never_above_production_and_message_total_never_below_one_file(private_key):
    config = config_for(private_key)
    with pytest.raises(DooersThreadsConfigurationError):
        replace(config, max_artifact_bytes=DEFAULT_MAX_SIZE + 1)
    with pytest.raises(DooersThreadsConfigurationError):
        replace(config, max_hydrated_bytes=DEFAULT_MAX_SIZE - 1)
    assert replace(config, max_hydrated_bytes=2 * DEFAULT_MAX_SIZE).max_hydrated_bytes == 2 * DEFAULT_MAX_SIZE


@pytest.mark.parametrize(
    "mime,filename",
    [
        ("application/msword", "contrato.doc"),
        ("application/vnd.ms-powerpoint", "slides.ppt"),
        ("application/zip", "pacote.zip"),
        ("application/x-msdownload", "setup.exe"),
        ("application/octet-stream", "dados.bin"),
        ("image/png", "foto.png"),
    ],
)
def test_any_type_without_allowlist_as_production(private_key, mime, filename):
    config = config_for(private_key)
    assert config.artifact_mime_types == ()
    assert artifact_type_allowed(config, mime, filename)
    # A DooersThreadsConfig allowlist still restricts, as production's allowlist does.
    assert not artifact_type_allowed(replace(config, artifact_mime_types=("text/plain",)), mime, filename)
    assert not artifact_type_allowed(config, "not a mime", filename)


def test_fetched_media_url_keeps_the_kind_policy_and_falls_back_to_the_link(private_key):
    config = config_for(private_key)
    assert media_url_upload_allowed(config, "image/png", "logo.png")
    assert not media_url_upload_allowed(config, "application/zip", "pacote.zip")


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("application/msword", DocumentPart),
        ("application/zip", DocumentPart),
        ("application/x-msdownload", DocumentPart),
        ("application/pdf", DocumentPart),
        ("image/svg+xml", ImagePart),
        ("audio/wav", AudioPart),
    ],
)
def test_part_type_matches_what_the_production_composer_sends(mime, expected):
    """Production: the document input sends ``document`` (also for .doc/.zip/.exe), images ``image``, voice ``audio``."""
    part = DooersThreadsHydratedArtifact(str(uuid4()), "arquivo", mime, b"bytes").content_part(None)
    assert isinstance(part, expected) and part.data == b"bytes" and part.mime_type == mime


def _attachment_message(fixture, *parts):
    content = [{"type": "text", "text": "veja os anexos"}, *parts]
    fixture.response["trigger_event"]["payload"]["content"] = content
    fixture.response["history"]["items"][0]["payload"]["content"] = list(content)


def _attachment(size, filename="grande.pdf", mime="application/pdf"):
    return {"type": "attachment", "artifact_id": str(uuid4()), "filename": filename, "mime_type": mime, "size_bytes": size}


async def test_two_files_of_15_mib_reach_the_handler_without_a_message_total(private_key, contracts):
    """Production consumes each upload (25 MiB each); the old 16 MiB total refused the second file."""
    async with setup(private_key, contracts) as fixture:
        first, second = _attachment(15 * MIB), _attachment(15 * MIB, "outro.pdf")
        _attachment_message(fixture, first, second)
        received = []

        async def hydrate(_self, reference, *, maximum_bytes=None):
            assert maximum_bytes == DEFAULT_MAX_SIZE
            return DooersThreadsHydratedArtifact(
                reference.artifact_id, reference.filename, reference.mime_type, b"x" * reference.size_bytes
            )

        async def handler(on, send, *_args):
            received.extend(len(part.data) for part in on.content if isinstance(part, DocumentPart))
            yield send.text("recebi")

        async def no_link(_self, _artifact_id):
            return None

        with patch.object(DooersThreadsArtifactsClient, "hydrate", hydrate), patch.object(LinkResolver, "url_for", no_link):
            response = await execute_fixture(fixture, handler)
        assert response.json()["state"] == "responded"
        assert received == [15 * MIB, 15 * MIB]


@pytest.mark.parametrize("code", ["CONTEXT_UNAVAILABLE", "CONTEXT_INVALID", "CONTEXT_LIMIT"])
async def test_attachment_that_cannot_be_read_shows_the_production_error(private_key, contracts, code):
    """F5c: the person sees production's text for an unreadable upload; the execution fails as a handler error."""
    async with setup(private_key, contracts) as fixture:
        part = _attachment(25 * MIB)
        _attachment_message(fixture, part)
        called = []

        async def hydrate(*_args, **_kwargs):
            raise DooersThreadsContextError(code)

        async def handler(_on, send, *_args):
            called.append(True)
            yield send.text("nunca")

        with patch.object(DooersThreadsArtifactsClient, "hydrate", hydrate):
            response = await execute_fixture(fixture, handler)
        assert not called
        assert response.status_code == 200 and response.json()["state"] == "failed"
        assert fixture.completions[-1]["outcome"] == "failed"
        assert fixture.completions[-1]["reason_code"] == "handler_error"
        (output,) = published(fixture)
        text = attachment_unavailable_text(part["artifact_id"])
        assert text == f"Upload reference '{part['artifact_id']}' not found or expired"
        assert [item for item in output["payload"]["content"] if item["type"] == "text"] == [{"type": "text", "text": text}]


async def test_lost_authority_while_reading_is_not_reported_as_a_message(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        _attachment_message(fixture, _attachment(1024))

        async def hydrate(*_args, **_kwargs):
            raise DooersThreadsContextError("ACCESS_REVOKED")

        async def handler(_on, send, *_args):
            yield send.text("nunca")

        with patch.object(DooersThreadsArtifactsClient, "hydrate", hydrate):
            await execute_fixture(fixture, handler)
        assert not published(fixture)
