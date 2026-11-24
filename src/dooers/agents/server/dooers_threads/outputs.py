"""Conversão entre ``AgentEvent`` de produção e saídas do Dooers Threads.

Regra do payload (índice 4.1): o payload de cada saída é o dicionário ``data`` do
``AgentEvent`` de produção para o mesmo ``send_type``, sem as chaves de primeiro
nível com valor ``None``. Valores aninhados não mudam (o contrato aceita ``null``
neles).

Este módulo é puro: não faz E/S e não conhece ``output_id`` nem ``lease_fence``
(o sink monta o ``Public.OutputRequest``). Os tipos com mídia (``image``,
``audio``, ``document``) ficam fora de ``output_for``, porque precisam de upload;
``media_output`` monta a mensagem deles depois que o sink tem a parte.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.persistence.sanitize import without_nul
from dooers.agents.server.protocol.models import deserialize_s2c_part

#: Saídas por fase (``output:0`` até ``output:997``): o teto do servidor do Dooers Threads
#: (``execution_outputs`` por entrega e fase). Produção não tem teto por execução.
MAX_PHASE_OUTPUTS = 998


#: Outputs that make a phase ``responded``: a visible reply (Dooers Threads M07-005/006). Any other output
#: (run, reasoning, tool, thread, participant, message edit) leaves the phase ``silent``.
VISIBLE_REPLY_OUTPUTS = frozenset({"message.create", "form.create", "chart.create"})


_LONE_SURROGATES = re.compile("[\ud800-\udfff]")


def clean_text(text: str) -> str:
    """Production's ``without_nul`` and a ``U+FFFD`` for each lone surrogate (not valid UTF-8, refused by Dooers Threads)."""
    return _LONE_SURROGATES.sub("\ufffd", without_nul(text))


def clean_json(value: Any) -> Any:
    """``value`` as production stores it: no NUL and no lone surrogate in any string, key or value.

    Production strips NUL in persistence (``persistence.sanitize``); Postgres rejects it, and Dooers Threads refuses
    the output instead of cleaning it. A copy; other types stay as they are.
    """
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, dict):
        return {clean_text(key) if isinstance(key, str) else key: clean_json(item) for key, item in value.items()}
    return value


def message_content_bytes(payload: dict) -> int:
    """Size of an agent message as Dooers Threads counts it against its limit.

    UTF-8 bytes of the ``text`` parts, plus the compact JSON of each ``contact`` part, of ``template`` and
    of ``whatsapp``. Attachments and media links are fixed-size metadata and count zero.
    """

    def json_bytes(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    total = 0
    for part in payload.get("content", ()):
        if part.get("type") == "text":
            total += len(part["text"].encode("utf-8"))
        elif part.get("type") == "contact":
            total += json_bytes(part)
    for key in ("template", "whatsapp"):
        if payload.get(key):
            total += json_bytes(payload[key])
    return total


class UnsupportedAgentEvent(ValueError):
    """``send_type`` sem saída direta (mídia ou desconhecido)."""


@dataclass(frozen=True)
class DooersThreadsOutput:
    type: str
    payload: dict


class RunTracker:
    """Run atual de uma execução; espelha o ``current_run_id`` único de produção.

    Uma instância por execução. ``run_start`` abre um run novo e substitui o atual
    (o anterior fica como a produção o deixa). ``run_end`` fecha só o atual; depois
    dele não há run atual. Enquanto há run atual, as outras saídas levam o ``run_id``
    dele (índice 4.3.1).
    """

    def __init__(self) -> None:
        self._current: str | None = None

    @property
    def current(self) -> str | None:
        return self._current

    def open(self) -> str:
        self._current = str(uuid4())
        return self._current

    def close(self) -> str | None:
        """Fecha o run atual e o devolve; ``None`` se não há run atual."""
        run_id, self._current = self._current, None
        return run_id

    def restore(self, run_id: str | None) -> None:
        """Volta ao run atual de antes de um ``run.update`` que o Dooers Threads recusou."""
        self._current = run_id


def _present(data: dict) -> dict:
    return {key: deepcopy(value) for key, value in data.items() if value is not None}


def _message(event: AgentEvent, part: dict[str, Any]) -> dict:
    payload: dict[str, Any] = {"content": [part], "mentions": []}
    if event.data.get("author") is not None:
        payload["author"] = event.data["author"]
    if event.data.get("whatsapp") is not None:
        payload["whatsapp"] = deepcopy(event.data["whatsapp"])
    return payload


def _text(event: AgentEvent, runs: RunTracker) -> tuple[str, dict]:
    return "message.create", _message(event, {"type": "text", "text": event.data["text"]})


def _contact(event: AgentEvent, runs: RunTracker) -> tuple[str, dict]:
    part = {"type": "contact", **_present({k: event.data.get(k) for k in ("display_name", "vcard", "phones")})}
    return "message.create", _message(event, part)


def _template(event: AgentEvent, runs: RunTracker) -> tuple[str, dict]:
    data = event.data
    name = str(data.get("template_name") or "").strip()
    language = str(data.get("template_language") or "").strip()
    payload = _message(event, {"type": "text", "text": f"[template: {name} ({language})]"})
    payload["template"] = _present(
        {
            "template_name": data.get("template_name"),
            "template_language": data.get("template_language"),
            "template_components": data.get("template_components"),
        }
    )
    return "message.create", payload


#: Every field of a production media part (``WireS2C_ImagePart``, ``AudioPart``, ``DocumentPart``) that a ``media_link`` keeps.
MEDIA_LINK_FIELDS = ("url", "mime_type", "filename", "alt", "duration", "width", "height", "size_bytes", "ref_id")
MEDIA_KINDS = frozenset({"image", "audio", "document"})


def media_link_of(kind: str, fields: Any) -> dict[str, Any]:
    """The ``media_link`` of a production media part: each field it has, as production stores it (``None`` is absent).

    ``fields`` is the part (or the ``send.image``/``audio``/``document`` data); only the media fields are read, so the
    ``url``, the ``mime_type`` or the ``filename`` the handler gave reach Dooers Threads as they came, whatever their form.
    """
    return {"type": "media_link", "kind": kind} | {key: deepcopy(fields[key]) for key in MEDIA_LINK_FIELDS if fields.get(key) is not None}


def production_part(part: Any) -> dict[str, Any]:
    """A production content part (``WireS2C_*Part`` or its dict) as the Dooers Threads part an agent writes, with no file read.

    A dict goes through ``deserialize_s2c_part``, as ``pipeline.py`` does for ``send.update_user_event``. ``text`` and
    ``contact`` keep their fields (a contact with no name has ``display_name`` ``""``, the production default); a media
    part becomes a ``media_link`` with every field it has (production keeps the URL and the metadata, not a file).
    """
    model = deserialize_s2c_part(part) if isinstance(part, dict) else part
    data = model.model_dump()
    kind = data.get("type")
    if kind == "text":
        return {"type": "text", "text": data["text"]}
    if kind == "contact":
        contact: dict[str, Any] = {"type": "contact", "display_name": data.get("display_name") or ""}
        if data.get("vcard") is not None:
            contact["vcard"] = data["vcard"]
        if data.get("phones"):
            contact["phones"] = deepcopy(data["phones"])
        return contact
    if kind in MEDIA_KINDS:
        return media_link_of(kind, data)
    raise UnsupportedAgentEvent(f"content part {kind!r} has no Dooers Threads part")


def _event_update(event: AgentEvent, runs: RunTracker) -> tuple[str, dict]:
    """``send.update_user_event``: the parts as production stores them (``deserialize_s2c_part``), as Dooers Threads parts."""
    content = event.data["content"]
    if not isinstance(content, list):
        raise TypeError("content must be a list")
    return "message.update", {"event_id": event.data["event_id"], "content": [production_part(part) for part in content]}


def _run_start(event: AgentEvent, runs: RunTracker) -> tuple[str, dict]:
    payload: dict[str, Any] = {"run_id": runs.open(), "status": "running"}
    if event.data.get("agent_id") is not None:
        payload["agent_id"] = event.data["agent_id"]
    return "run.update", payload


def _run_end(event: AgentEvent, runs: RunTracker) -> tuple[str, dict] | None:
    run_id = runs.close()
    if run_id is None:
        return None  # produção: run_end sem run atual não faz nada
    payload: dict[str, Any] = {"run_id": run_id, "status": event.data.get("status") or "succeeded"}
    if event.data.get("error") is not None:
        payload["error"] = event.data["error"]
    return "run.update", payload


def _passthrough(output_type: str):
    def build(event: AgentEvent, runs: RunTracker) -> tuple[str, dict]:
        return output_type, _present(event.data)

    return build


_BUILDERS = {
    "text": _text,
    "contact": _contact,
    "template": _template,
    "reasoning": _passthrough("reasoning.create"),
    "tool_call": _passthrough("tool_call.create"),
    "tool_result": _passthrough("tool_result.create"),
    "tool_transaction": _passthrough("tool_transaction.create"),
    "form": _passthrough("form.create"),
    "chart": _passthrough("chart.create"),
    "run_start": _run_start,
    "run_end": _run_end,
    "event_update": _event_update,
    "thread_update": _passthrough("thread.update"),
    "participant_add": _passthrough("participant.add"),
}


def output_for(event: AgentEvent, ordinal: int, *, runs: RunTracker | None = None) -> DooersThreadsOutput | None:
    """Saída do Dooers Threads para um ``AgentEvent`` de produção.

    Devolve ``None`` para ``run_end`` sem run atual: a produção não faz nada nesse
    caso. O sink pula o ``None`` e não consome ``ordinal``.

    ``ordinal`` é a posição da saída na execução (o sink o usa em ``output_id``).
    ``runs`` guarda os runs abertos da execução; sem ele, cada chamada é isolada.
    """
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("ordinal must be a non-negative integer")
    build = _BUILDERS.get(event.send_type)
    if build is None:
        raise UnsupportedAgentEvent(f"send_type {event.send_type!r} has no direct Dooers Threads output")
    tracker = runs if runs is not None else RunTracker()
    built = build(event, tracker)
    if built is None:
        return None
    output_type, payload = built
    if event.send_type not in ("run_start", "run_end") and tracker.current is not None:
        payload["run_id"] = tracker.current
    return DooersThreadsOutput(output_type, payload)


def media_output(event: AgentEvent, part: dict[str, Any], *, runs: RunTracker | None = None) -> DooersThreadsOutput:
    """``message.create`` de ``image``, ``audio`` ou ``document`` com a parte já pronta.

    O sink monta ``part`` (``attachment`` depois do upload, ou ``media_link``). O resto
    do payload segue a mesma regra das outras mensagens: ``author``, ``whatsapp`` e o
    ``run_id`` do run atual.
    """
    payload = _message(event, deepcopy(part))
    if runs is not None and runs.current is not None:
        payload["run_id"] = runs.current
    return DooersThreadsOutput("message.create", payload)


# --- inverso -----------------------------------------------------------------


def _from_message(payload: dict) -> AgentEvent | None:
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    part = content[0]
    extra: dict[str, Any] = {}
    if payload.get("author") is not None:
        extra["author"] = payload["author"]
    if payload.get("whatsapp") is not None:
        extra["whatsapp"] = deepcopy(payload["whatsapp"])
    template = payload.get("template")
    if isinstance(template, dict):
        data = {**_present(template), **extra}
        data.setdefault("template_components", [])
        return AgentEvent("template", data)
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        return AgentEvent("text", {"text": part["text"], **extra})
    if part.get("type") == "contact" and part.get("display_name") is not None:
        data = {"display_name": part["display_name"], "phones": deepcopy(part.get("phones") or []), **extra}
        if part.get("vcard") is not None:
            data["vcard"] = part["vcard"]
        return AgentEvent("contact", data)
    return None


def _from_run(payload: dict) -> AgentEvent | None:
    status = payload.get("status")
    if status == "running":
        return AgentEvent("run_start", _present({"agent_id": payload.get("agent_id")}))
    if status in ("succeeded", "failed"):
        return AgentEvent("run_end", _present({"status": status, "error": payload.get("error")}))
    return None


def _from_thread(payload: dict) -> AgentEvent | None:
    data = {key: payload[key] for key in ("title", "queue") if payload.get(key) is not None}
    return AgentEvent("thread_update", data) if data else None


def _from_participant(payload: dict) -> AgentEvent | None:
    user = payload.get("user")
    if isinstance(user, dict):
        return AgentEvent("participant_add", {"user": deepcopy(user)})
    participant = payload.get("participant")
    if isinstance(participant, dict) and participant.get("kind") == "human" and participant.get("principal_id"):
        return AgentEvent(
            "participant_add",
            {"user": _present({"user_id": participant["principal_id"], "user_name": participant.get("display_name")})},
        )
    return None


_PASSTHROUGH_KEYS = {
    "reasoning.created": ("reasoning", ("text", "author")),
    "tool_call.created": ("tool_call", ("id", "name", "display_name", "args")),
    "tool_result.created": ("tool_result", ("id", "name", "display_name", "args", "result")),
    "tool_transaction.created": ("tool_transaction", ("id", "name", "display_name", "args", "result")),
    "form.created": ("form", ("message", "elements", "submit_label", "cancel_label", "size")),
    "chart.created": (
        "chart",
        ("title", "message", "chart_type", "size", "x_key", "y_keys", "data", "series", "x_label", "y_label"),
    ),
    "message.updated": ("event_update", ("event_id", "content")),
}


def agent_event_from(event: dict) -> AgentEvent | None:
    """``AgentEvent`` de produção para um evento do Dooers Threads, ou ``None`` se não houver equivalente.

    Inverso de :func:`output_for` para os eventos gravados pelas saídas do agente.
    O ``data`` volta sem chaves ``None`` e sem ``run_id``. A função não filtra por
    ator: quem chama escolhe quais eventos converter.
    """
    if not isinstance(event, dict):
        return None
    event_type, payload = event.get("type"), event.get("payload")
    if not isinstance(event_type, str) or not isinstance(payload, dict):
        return None
    if event_type == "message.created":
        return _from_message(payload)
    if event_type == "run.updated":
        return _from_run(payload)
    if event_type == "thread.updated":
        return _from_thread(payload)
    if event_type == "participant.added":
        return _from_participant(payload)
    mapped = _PASSTHROUGH_KEYS.get(event_type)
    if mapped is None:
        return None
    send_type, keys = mapped
    return AgentEvent(send_type, _present({key: payload.get(key) for key in keys}))
