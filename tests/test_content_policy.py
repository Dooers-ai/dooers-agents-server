"""Tests for ``handlers.content_policy`` helpers."""

from dooers_agents.exceptions import UnsupportedContentTypeError
from dooers_agents.handlers.content_policy import (
    enforce_allowed_content_types,
    format_allowed_content_policy_denial,
    normalize_allowed_content_types,
    parse_allowed_content_types_setting,
)
from dooers_agents.protocol.models import AudioPart, ImagePart, TextPart


def test_parse_allowed_empty_means_none() -> None:
    assert parse_allowed_content_types_setting(None) is None
    assert parse_allowed_content_types_setting("") is None
    assert parse_allowed_content_types_setting("   ") is None
    assert parse_allowed_content_types_setting([]) is None


def test_parse_allowed_comma_separated() -> None:
    got = parse_allowed_content_types_setting("text, audio, doc")
    assert got == frozenset({"text", "audio", "document"})


def test_parse_allowed_json_list_string() -> None:
    got = parse_allowed_content_types_setting('["text","image"]')
    assert got == frozenset({"text", "image"})


def test_parse_allowed_unknown_tokens_only() -> None:
    assert parse_allowed_content_types_setting("video, unknown") is None


def test_normalize_frozenset() -> None:
    assert normalize_allowed_content_types(frozenset({"doc", "text"})) == frozenset({"text", "document"})


def test_format_denial_custom_template() -> None:
    got = format_allowed_content_policy_denial(
        parts=[TextPart(text="hi"), ImagePart(data=b"", mime_type="image/png")],
        allowed=frozenset({"text"}),
        message_template="Bad: {offenders} Allowed: {allowed}",
    )
    assert got == "Bad: image Allowed: text"


def test_enforce_raises_when_not_subset() -> None:
    try:
        enforce_allowed_content_types(
            parts=[TextPart(text="hi"), ImagePart(data=b"", mime_type="image/png")],
            allowed=frozenset({"text"}),
        )
    except UnsupportedContentTypeError as e:
        assert "image" in str(e).lower()
    else:
        raise AssertionError("expected UnsupportedContentTypeError")


def test_enforce_passes_when_not_configured() -> None:
    enforce_allowed_content_types(
        parts=[AudioPart(data=b"x", mime_type="audio/webm")],
        allowed=None,
    )
