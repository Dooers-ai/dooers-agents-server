from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from dooers.tools.connectors.formats import (
    ParsedCall,
    parse_call,
    result_message,
    to_anthropic,
    to_gemini,
    to_openai,
    to_openai_responses,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "title": "Summary"},
        "duration_minutes": {"type": "integer", "default": 30},
    },
    "required": ["summary"],
    "additionalProperties": False,
}
SPEC = {
    "name": "agenda__create_event",
    "connector": {"id": "c1", "slug": "agenda", "name": "Agenda", "icon": "x", "type": "google_calendar"},
    "tool": "create_event",
    "title": "Criar evento",
    "description": "Agenda: Criar evento",
    "input_schema": SCHEMA,
    "read_only": False,
    "destructive": False,
    "state": "needs_approval",
    "status": "ready",
}


def test_to_openai_golden():
    assert to_openai(SPEC) == {
        "type": "function",
        "function": {"name": "agenda__create_event", "description": "Agenda: Criar evento", "parameters": SCHEMA},
    }


def test_to_openai_responses_golden():
    assert to_openai_responses(SPEC) == {
        "type": "function",
        "name": "agenda__create_event",
        "description": "Agenda: Criar evento",
        "parameters": SCHEMA,
    }


def test_to_anthropic_golden():
    assert to_anthropic(SPEC) == {"name": "agenda__create_event", "description": "Agenda: Criar evento", "input_schema": SCHEMA}


def test_to_gemini_golden_strips_unsupported_keywords_and_keeps_property_names():
    spec = {
        **SPEC,
        "input_schema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "CreateIssue",
            "type": "object",
            "properties": {
                "title": {"type": "string", "title": "Title", "description": "Issue title"},
                "default": {"type": "string", "default": "x"},
                "labels": {
                    "type": "array",
                    "items": {"type": "object", "title": "L", "additionalProperties": False, "properties": {"title": {"type": "string"}}},
                },
                "when": {"anyOf": [{"type": "string", "default": "now"}, {"type": "null"}]},
                "state": {"type": "string", "enum": ["title", "default"]},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    }
    original = copy.deepcopy(spec)
    assert to_gemini(spec) == {
        "name": "agenda__create_event",
        "description": "Agenda: Criar evento",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Issue title"},
                "default": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}}}},
                "when": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "state": {"type": "string", "enum": ["title", "default"]},
            },
            "required": ["title"],
        },
    }
    assert spec == original  # input not mutated


def test_to_gemini_strips_unsupported_keys_from_reference_spec():
    params = to_gemini(SPEC)["parameters"]
    assert params == {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "duration_minutes": {"type": "integer"}},
        "required": ["summary"],
    }


def test_parse_openai_tool_call_object_and_dict():
    d = parse_call({"id": "c1", "type": "function", "function": {"name": "a__b", "arguments": '{"x": 1}'}})
    o = parse_call(SimpleNamespace(id="c1", type="function", function=SimpleNamespace(name="a__b", arguments='{"x": 1}')))
    expected = ParsedCall(provider="openai", call_id="c1", name="a__b", arguments={"x": 1}, raw_arguments_error=None)
    assert d == expected
    assert o == expected


def test_parse_openai_empty_arguments_is_empty_dict():
    p = parse_call({"id": "c1", "type": "function", "function": {"name": "x", "arguments": ""}})
    assert p.arguments == {} and p.raw_arguments_error is None


def test_parse_malformed_json_arguments_does_not_raise():
    p = parse_call({"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{oops"}})
    assert p.arguments is None and p.raw_arguments_error


def _openai(args):
    return {"id": "c1", "type": "function", "function": {"name": "x", "arguments": args}}


def _responses(args):
    return {"type": "function_call", "call_id": "c1", "name": "x", "arguments": args}


def _responses_obj(args):
    return SimpleNamespace(type="function_call", call_id="c1", name="x", arguments=args)


@pytest.mark.parametrize("make", [_openai, _responses, _responses_obj], ids=["openai", "responses", "responses_obj"])
@pytest.mark.parametrize("bad", ["{oops", "[1, 2]", '"text"', "123", '{"a": '])
def test_parse_string_arguments_malformed_or_not_object_is_invalid(make, bad):
    p = parse_call(make(bad))
    assert p.name == "x" and p.call_id == "c1"
    assert p.arguments is None
    assert p.raw_arguments_error


@pytest.mark.parametrize(
    "call, provider, call_id",
    [
        (_responses('{"x": 1}'), "openai_responses", "c1"),
        (_responses_obj('{"x": 1}'), "openai_responses", "c1"),
        ({"type": "tool_use", "id": "tu1", "name": "x", "input": {"x": 1}}, "anthropic", "tu1"),
        (SimpleNamespace(type="tool_use", id="tu1", name="x", input={"x": 1}), "anthropic", "tu1"),
        (SimpleNamespace(name="x", args={"x": 1}, id=None), "gemini", "x"),
        ({"name": "x", "args": {"x": 1}}, "gemini", "x"),
        (SimpleNamespace(function_call=SimpleNamespace(name="x", args={"x": 1}, id=None)), "gemini", "x"),
        ({"function_call": {"name": "x", "args": {"x": 1}, "id": "g1"}}, "gemini", "g1"),
        (("x", {"x": 1}), "generic", ""),
    ],
)
def test_parse_each_provider_shape(call, provider, call_id):
    p = parse_call(call)
    assert (p.provider, p.call_id, p.name, p.arguments, p.raw_arguments_error) == (provider, call_id, "x", {"x": 1}, None)


def test_parse_gemini_missing_args_is_empty_dict():
    assert parse_call({"name": "x"}).arguments == {}


def test_parse_unknown_shape_raises_value_error():
    with pytest.raises(ValueError):
        parse_call(42)


def test_result_message_shapes_per_provider():
    payload = {"status": "ok", "content": {"n": "ação"}}
    body = json.dumps(payload, ensure_ascii=False)
    mk = lambda provider, cid: ParsedCall(provider=provider, call_id=cid, name="x", arguments={}, raw_arguments_error=None)  # noqa: E731
    assert result_message(mk("openai", "c1"), payload) == {"role": "tool", "tool_call_id": "c1", "content": body}
    assert result_message(mk("openai_responses", "c2"), payload) == {"type": "function_call_output", "call_id": "c2", "output": body}
    assert result_message(mk("anthropic", "t3"), payload) == {"type": "tool_result", "tool_use_id": "t3", "content": body}
    assert result_message(mk("gemini", "x"), payload) == {"function_response": {"name": "x", "response": payload}}
    assert result_message(mk("generic", ""), payload) == payload
