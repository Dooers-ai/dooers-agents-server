"""Tests for ``dooers.llm.format_user_input``."""

import pytest

from dooers.handlers.context import AgentContext
from dooers.handlers.incoming import AgentIncoming
from dooers.llm import format_user_input
from dooers.llm.types import LlmWireFormat
from dooers.protocol.models import AudioPart, DocumentPart, ImagePart, TextPart, User


def _ctx(*, tid="t1", eid="e1", aid="a1") -> AgentContext:
    return AgentContext(
        thread_id=tid,
        agent_id=aid,
        event_id=eid,
        user=User(user_id="u"),
    )


def test_text_only_message_item_string_content() -> None:
    inc = AgentIncoming(message="hello", content=[], context=_ctx())
    assert format_user_input(inc, "openai_responses") == {
        "type": "message",
        "role": "user",
        "content": "hello",
    }


def test_accepts_enum_optional() -> None:
    inc = AgentIncoming(message="hello", content=[], context=_ctx())
    assert format_user_input(inc, LlmWireFormat.CLAUDE)["content"] == "hello"


def test_hyphen_normalized() -> None:
    inc = AgentIncoming(message="hello", content=[], context=_ctx())
    assert format_user_input(inc, "openai-responses")["content"] == "hello"


def test_invalid_api_provider() -> None:
    inc = AgentIncoming(message="x", content=[], context=_ctx())
    with pytest.raises(ValueError, match="Invalid api_provider"):
        format_user_input(inc, "unknown_backend")


def test_audio_part_plain_text_turn() -> None:
    inc = AgentIncoming(
        message="intro",
        content=[AudioPart(data=b"x", mime_type="audio/webm", filename="a.webm")],
        context=_ctx(),
    )
    out = format_user_input(inc, "openai_responses", True)
    assert out == {
        "type": "message",
        "role": "user",
        "content": "intro\n[audio: a.webm]",
    }


def test_audio_with_public_url() -> None:
    inc = AgentIncoming(
        message="",
        content=[
            AudioPart(
                data=b"",
                mime_type="audio/webm",
                filename="a.webm",
                url="https://cdn.example/audio.webm",
            )
        ],
        context=_ctx(),
    )
    out = format_user_input(inc, "openai_responses")
    assert out["content"] == "[audio: a.webm] https://cdn.example/audio.webm"


def test_document_part() -> None:
    inc = AgentIncoming(
        message="Please read:",
        content=[
            DocumentPart(
                data=b"x",
                mime_type="application/pdf",
                filename="x.pdf",
                size_bytes=1,
                url="https://cdn.example/x.pdf",
            )
        ],
        context=_ctx(),
    )
    c = format_user_input(inc, "openai_completions")["content"]
    assert isinstance(c, str)
    assert "Please read:" in c
    assert "[document: x.pdf]" in c
    assert "https://cdn.example/x.pdf" in c


def test_strict_rejects_image_without_payload() -> None:
    inc = AgentIncoming(message="hi", content=[ImagePart(data=b"", mime_type="image/jpeg")], context=_ctx())
    with pytest.raises(ValueError, match="image part"):
        format_user_input(inc, "openai_responses", True)


def test_non_strict_skips_empty_image() -> None:
    inc = AgentIncoming(message="only", content=[ImagePart(data=b"", mime_type="image/jpeg")], context=_ctx())
    assert format_user_input(inc, "openai_responses", False) == {
        "type": "message",
        "role": "user",
        "content": "only",
    }


def test_multimodal_list_content_native_parts() -> None:
    inc = AgentIncoming(
        message="see",
        content=[ImagePart(data=b"\xff\xd8\xff", mime_type="image/jpeg", filename="x.jpg")],
        context=_ctx(),
    )
    out = format_user_input(inc, "openai_responses")
    assert out["type"] == "message"
    assert out["role"] == "user"
    content = out["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "input_text", "text": "see"}
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "auto"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")


def test_gemini_wire_id() -> None:
    inc = AgentIncoming(
        message="",
        content=[ImagePart(data=b"\xff\xd8\xff", mime_type="image/jpeg")],
        context=_ctx(),
    )
    out = format_user_input(inc, "gemini")
    content = out["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "input_text"
    assert "imagem" in content[0]["text"]
    assert content[1]["type"] == "input_image"


def test_claude_wire_id() -> None:
    inc = AgentIncoming(
        message="",
        content=[ImagePart(data=b"\xff\xd8\xff", mime_type="image/jpeg")],
        context=_ctx(),
    )
    out = format_user_input(inc, "claude")
    content = out["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"


def test_merges_message_and_text_parts() -> None:
    inc = AgentIncoming(
        message="a",
        content=[TextPart(text="b")],
        context=_ctx(),
    )
    assert format_user_input(inc, "openai_responses")["content"] == "a\nb"


def test_order_text_image_audio_native_parts() -> None:
    inc = AgentIncoming(
        message="caption",
        content=[
            ImagePart(data=b"\xff\xd8\xff", mime_type="image/jpeg"),
            AudioPart(data=b"x", mime_type="audio/webm", filename="a.webm"),
        ],
        context=_ctx(),
    )
    out = format_user_input(inc, "openai_responses")
    content = out["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "input_text"
    assert content[0]["text"] == "caption"
    assert content[1]["type"] == "input_image"
    assert content[2]["type"] == "input_text"
    assert "[audio:" in content[2]["text"]


def test_role_parameter() -> None:
    inc = AgentIncoming(message="x", content=[], context=_ctx())
    out = format_user_input(inc, "openai_responses", strict=True, role="developer")
    assert out["role"] == "developer"
