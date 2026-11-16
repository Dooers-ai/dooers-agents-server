"""Maestro events as the production thread models (``Thread``, ``ThreadEvent``, ``Run``).

Production reference (origin/main): ``HandlerPipeline.setup``/``execute`` write each event
(``handlers/pipeline.py``), the router writes the queue ``thread.update`` of a person
(``handlers/router.py``), and ``PostgresPersistence._row_to_event`` reads the events back.
A converted event is what production ``get_events`` returns for the same thread content:
the ``user`` keeps only id, name and e-mail, there is no ``client_event_id``, and empty
content or data is ``None``.

Use :func:`fold_events` on the Maestro events (oldest first) before :func:`to_thread_event`:
the fold gives the list production would have stored (edits applied, removals gone, the
run of each input event, the queue before each queue change).
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from dooers.agents.server.protocol.models import (
    Run,
    Thread,
    ThreadEvent,
    User,
    WireS2C_AudioPart,
    WireS2C_ChartEventData,
    WireS2C_ContactPart,
    WireS2C_DocumentPart,
    WireS2C_FormCheckboxElement,
    WireS2C_FormFileElement,
    WireS2C_FormRadioElement,
    WireS2C_FormSelectElement,
    WireS2C_FormTextareaElement,
    WireS2C_FormTextElement,
    WireS2C_ImagePart,
    WireS2C_TextPart,
)

from .links import LinkResolver

#: Payload key :func:`fold_events` adds to a ``thread.updated`` that changed the queue: the
#: queue before it (``data.from`` of the production ``thread.update`` event).
PREVIOUS_QUEUE = "previous_queue"

#: Payload key :func:`fold_events` adds to a message with a ``channel_delivery.updated``: the latest
#: cumulative ``{status, provider_message_id?, error_code?, error?}`` (plan 5 Task 7). ``_message``
#: folds it into ``data["delivery"]`` on read-back; never persisted on the Maestro wire itself.
CHANNEL_DELIVERY_KEY = "_channel_delivery"

#: Text before another agent's message in a thread with several agents (design 7.5).
AGENT_PREFIX = "[agente: {name}]"

#: ``AgentConfig.assistant_name`` default: the author production writes for its agent.
DEFAULT_ASSISTANT_NAME = "Assistant"

_MEDIA_PARTS = {"image": WireS2C_ImagePart, "audio": WireS2C_AudioPart, "document": WireS2C_DocumentPart}
#: Metadata each production part keeps from a Maestro ``attachment``.
_ATTACHMENT_FIELDS = {
    "image": ("mime_type", "filename", "alt", "width", "height"),
    "audio": ("mime_type", "filename", "duration"),
    "document": ("mime_type", "filename", "size_bytes"),
}
#: Metadata of production ``send.image``/``send.audio``/``send.document`` (``HandlerPipeline.execute``):
#: an agent's attachment (a URL the SDK uploaded) keeps only these, and no ``ref_id``.
_AGENT_MEDIA_FIELDS = {
    "image": ("mime_type", "alt"),
    "audio": ("mime_type", "duration"),
    "document": ("mime_type", "filename"),
}
#: Metadata each production part keeps from a Maestro ``media_link`` (a URL the SDK did not upload).
_MEDIA_LINK_FIELDS = {
    "image": ("mime_type", "filename", "alt"),
    "audio": ("mime_type", "filename", "duration"),
    "document": ("mime_type", "filename"),
}
#: ``HandlerPipeline.execute`` (send_type ``form``): known elements go through these models.
_FORM_ELEMENTS = {
    "text_input": WireS2C_FormTextElement,
    "textarea_input": WireS2C_FormTextareaElement,
    "select_input": WireS2C_FormSelectElement,
    "radio_input": WireS2C_FormRadioElement,
    "checkbox_input": WireS2C_FormCheckboxElement,
    "file_input": WireS2C_FormFileElement,
}
#: ``AgentSend.chart`` sets every key, so production stores every key (``exclude_unset``).
_CHART_KEYS = ("title", "message", "chart_type", "size", "x_key", "y_keys", "data", "series", "x_label", "y_label")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fold_events(events: list[dict], *, queue: str | None = None) -> list[dict]:
    """The Maestro events (oldest first) that production would have stored, in Maestro shape.

    - ``message.updated`` replaces the content of its target; ``message.removed`` takes its
      target out. Both control events leave the list, as do edits of a missing target
      (production logs a warning and changes nothing).
    - The input event of each run (``run.updated.trigger_event_id`` of a ``running`` update)
      gets the run's ``run_id`` in its payload, as ``HandlerPipeline.execute`` does on
      ``run_start``; with several runs the last one stays. An event the agent wrote inside its
      own run keeps that ``run_id``.
    - A ``thread.updated`` that changed the queue gets :data:`PREVIOUS_QUEUE`. ``queue`` is the queue
      the thread had before the first event of the list (a ``thread.created`` in the list replaces it):
      a list that does not start at the thread's first event passes what it knows.
    - A ``channel_delivery.updated`` folds onto its target's payload (:data:`CHANNEL_DELIVERY_KEY`),
      production's cumulative merge of ``_record_whatsapp_delivery``: each one overwrites only the
      fields it carries (``status`` always; ``provider_message_id``/``error_code``/``error`` only
      when present), so an earlier field a later update omits stays. It never becomes an event of its
      own (plan 5 Task 7; production has no event for it either). An update of a missing or removed
      target (a race with the edit/removal) is dropped, like ``message.updated`` on one.

    The input list and its events are not changed; a changed event is a new dict.
    """
    folded: list[dict | None] = []
    positions: dict[str, int] = {}
    own_run: set[str] = set()
    for event in events:
        kind, payload = event["type"], event["payload"]
        if kind in ("message.updated", "message.removed"):
            position = positions.get(payload["event_id"])
            if position is None:
                continue
            if kind == "message.removed":
                folded[position] = None
                del positions[payload["event_id"]]
            else:
                target = folded[position]
                folded[position] = {**target, "payload": {**target["payload"], "content": deepcopy(payload["content"])}}
            continue
        if kind == "channel_delivery.updated" and payload["event_id"] in positions:
            position = positions[payload["event_id"]]
            target = folded[position]
            previous = target["payload"].get(CHANNEL_DELIVERY_KEY) or {}
            updates = {key: payload[key] for key in ("status", "provider_message_id", "error_code", "error") if key in payload}
            folded[position] = {
                **target,
                "payload": {**target["payload"], CHANNEL_DELIVERY_KEY: {**previous, **updates}},
            }
            continue
        if kind == "run.updated" and payload["status"] == "running":
            position = positions.get(payload["trigger_event_id"])
            if position is not None and payload["trigger_event_id"] not in own_run:
                target = folded[position]
                folded[position] = {**target, "payload": {**target["payload"], "run_id": payload["run_id"]}}
        elif kind == "thread.created":
            queue = payload["thread"].get("queue")
        elif kind == "thread.updated" and "queue" in payload:
            if payload["queue"] != queue:
                event = {**event, "payload": {**payload, PREVIOUS_QUEUE: queue}}
            queue = payload["queue"]
        if "run_id" in payload:
            own_run.add(event["event_id"])
        positions[event["event_id"]] = len(folded)
        folded.append(event)
    return [event for event in folded if event is not None]


#: The name Maestro shows for a person the core did not name (a web chat person has no profile of its own there).
PLACEHOLDER_NAME = "Humano"
_ROLES = ("system_role", "organization_role", "workspace_role")


def _core_person(person: dict[str, Any], core: dict[str, Any] | None) -> dict[str, Any]:
    """``person`` (an actor) with what its participant (``core``) adds.

    Maestro puts the core's data (name, e-mail, identity IDs and the three roles) on each human it knows,
    on the participant and on the actor; ``system_role`` says that the core described the person. An
    actor that carries its own e-mail or identity IDs (the agent declared them when it published the
    message) keeps them, and the participant gives the rest. A placeholder or empty name gives way to
    the core name.
    """
    if not core:
        return person
    merged = {key: core[key] for key in ("email", "identity_ids", *_ROLES) if key in core and key not in person}
    merged.update(person)
    if "system_role" in core and person.get("display_name") in (None, "", PLACEHOLDER_NAME):
        merged["display_name"] = core.get("display_name")
    return merged


def _external_user(person: dict[str, Any], core: dict[str, Any] | None) -> User:
    """The production ``User`` of a channel contact or a public-chat visitor's actor or participant.

    A WhatsApp contact's participant (Maestro today) carries nothing beyond the channel contact
    identity, so without ``core``, or with one that names no one, ``User`` keeps whatever ``person``
    (the actor or participant itself) already has of its own — as production always did for the name:
    ``user_name = person.get("display_name") or None``.

    A public-chat visitor's participant (``core``, plan 6 Task 3b/4b's ``ContextResponse.participants``
    external variant) carries the core's own e-mail, identity IDs, the three roles, ``connection_type``
    (``guest`` | ``public_authenticated``) and ``guest_profile`` (the visitor's own pre-chat
    name/e-mail/phone, declared, not verified by the core). ``context.py``'s own ``_user`` (the live
    handler context) and ``legacy_model._message`` (a message's author) pass the participant as ``core``
    separately; ``production_user``'s other callers (``to_thread``'s already-enriched
    ``view["participants"]``) pass only ``person`` and leave ``core`` ``None`` — ``person`` itself is then
    the only source, the same fallback ``_core_person`` already gives a human. A bare message actor (the
    contract's own ``external`` actor variant carries none of this but ``connection_type``) has none to lose.

    The name is the actor's own ``display_name`` first: a WhatsApp contact declares its current name on
    every publication, while its participant keeps the one it had when it joined (production uses the
    current one). The visitor's actor has no name, so its name is the core's, else the guest's declared
    ``guest_profile`` one (production's ``_enrich_user_from_metadata``), else ``None`` — never a placeholder.
    The e-mail is the core's, else the declared one. ``connection_type`` falls back to the actor's.
    """
    source = core if core is not None else person
    guest_profile = source.get("guest_profile") or {}
    name = person.get("display_name") or source.get("display_name") or guest_profile.get("name") or None
    email = source.get("email") or guest_profile.get("email") or None
    connection_type = source.get("connection_type") or person.get("connection_type") or None
    return User(
        user_id=person["external_id"],
        user_name=name,
        user_email=email,
        identity_ids=list(source.get("identity_ids") or []),
        **{role: source[role] for role in _ROLES if source.get(role)},
        **({"connection_type": connection_type} if connection_type else {}),
    )


def production_user(person: dict[str, Any], core: dict[str, Any] | None = None) -> User:
    """The production ``User`` of a Maestro actor or participant.

    A person keeps the profile of the agent's context (an empty name or e-mail is ``None``, as production
    builds it from Core) and the three roles of Core's connection context. A person the core did not
    describe (an older Maestro, or an unknown person) has the ``User`` default roles. The placeholder name
    Maestro shows for a person without a name is ``None``, never ``"Humano"``: Maestro has no flag for
    it, so a real person named "Humano" also has no name. ``core`` is the participant of an actor: see
    ``_core_person`` for a human, ``_external_user`` for a channel contact or a public-chat visitor. An
    agent is its instance id and name, as production lists the agent in ``Thread.users``.
    """
    if person["kind"] == "human":
        person = _core_person(person, core)
        # Maestro has no flag for its placeholder, so the name "Humano" is no name, with or without core fields.
        name = person.get("display_name") or None
        if name == PLACEHOLDER_NAME:
            name = None
        return User(
            user_id=person["principal_id"],
            user_name=name,
            user_email=person.get("email") or None,
            identity_ids=list(person.get("identity_ids") or []),
            **{role: person[role] for role in _ROLES if person.get(role)},
        )
    if person["kind"] == "external":
        return _external_user(person, core)
    if person["kind"] == "agent":
        return User(user_id=person["instance_agent_id"], user_name=person.get("display_name") or None)
    return User(user_id="")


#: Production's default channel (web chat; a thread with no channel) — ``context.DEFAULT_CHANNEL``'s
#: own literal, duplicated here to avoid an import cycle (``context.py`` already imports this module).
DOOERS_PLATFORM_CHANNEL = "dooers-platform"

#: Production's channel for a public-chat visitor thread (the wire's own short name is ``public_chat``;
#: ``context._EXTERNAL_CHANNEL`` maps it to this one).
PUBLIC_CHAT_CHANNEL = "dooers-public-chat"

#: Production's WhatsApp channel (the wire's own short name already is production's own: see
#: ``context._EXTERNAL_CHANNEL``).
WHATSAPP_CHANNEL = "whatsapp"


def _public_chat_label(channel_meta: dict[str, Any] | None) -> str:
    """The public chat link's label, as production reads it: ``public_chat_label``, else ``public_chat.label``."""
    meta = channel_meta if isinstance(channel_meta, dict) else {}
    label = str(meta.get("public_chat_label") or "").strip()
    if not label:
        nested = meta.get("public_chat")
        if isinstance(nested, dict):
            label = str(nested.get("label") or "").strip()
    return label


def assistant_delivery_data(
    channel: str | None, channel_meta: dict[str, Any] | None, *, user_id: str | None = None
) -> dict[str, Any] | None:
    """Maestro-mode equivalent of production's ``HandlerPipeline._assistant_delivery_data``.

    Production stamps ``data["delivery"]`` on every assistant-authored ``message.created`` it persists
    (origin/main ``handlers/pipeline.py``, around line 1230): ``None`` on an empty channel or on
    ``"dooers-platform"`` (plain web chat); otherwise ``{"channel": channel, ...}`` plus channel-specific
    fields: ``public_chat_label`` (``_public_chat_label``) for ``"dooers-public-chat"`` (plan 6, Task 5);
    ``to_e164``/``instance_id`` (from ``channel_meta["whatsapp"]``, falling back to ``user_id`` for
    ``to_e164`` as production falls back to ``context.user.user_id``) for ``"whatsapp"`` (plan 5, Task 7).

    Plan 5 Task 7's other half — folding ``channel_delivery.updated`` into ``data["delivery"]`` on
    read-back — happens where a converted event's ``data`` is read back (``_message``, below), onto
    the dict this function returns: it never re-derives ``data["delivery"]`` from scratch there.
    """
    channel = (channel or "").strip()
    if not channel or channel == DOOERS_PLATFORM_CHANNEL:
        return None
    delivery: dict[str, Any] = {"channel": channel}
    if channel == PUBLIC_CHAT_CHANNEL:
        label = _public_chat_label(channel_meta)
        if label:
            delivery["public_chat_label"] = label
    elif channel == WHATSAPP_CHANNEL:
        meta = channel_meta if isinstance(channel_meta, dict) else {}
        whatsapp = meta.get("whatsapp")
        if isinstance(whatsapp, dict):
            to_e164 = str(whatsapp.get("to_e164") or user_id or "").strip()
            if to_e164:
                delivery["to_e164"] = to_e164
            instance_id = str(whatsapp.get("instance_id") or "").strip()
            if instance_id:
                delivery["instance_id"] = instance_id
    return {"delivery": delivery}


def public_chat_source_data(channel: str | None, channel_meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Production's ``HandlerPipeline._inbound_source_data`` for a public chat thread, else ``None``.

    Production stamps ``data["source"]`` on every inbound event of a thread with a channel; for
    ``"dooers-public-chat"`` it depends only on the thread's ``channel`` and ``channel_meta`` (the link's
    label), so the visitor's message — which Maestro stores without a ``data`` — gets it on read-back.
    A WhatsApp inbound's source has per-message values (``wa_message_id``, ``from_me``) the thread cannot
    give: its message carries them on its own ``payload.data`` (stamped at ingest), so it is not derived here.
    """
    if (channel or "").strip() != PUBLIC_CHAT_CHANNEL:
        return None
    source: dict[str, Any] = {"channel": PUBLIC_CHAT_CHANNEL}
    label = _public_chat_label(channel_meta)
    if label:
        source["public_chat_label"] = label
    return {"source": source}


def _stored(user: User) -> User:
    """The ``User`` production reads back from its events table (``user_id``, ``user_name``, ``user_email``)."""
    return User(user_id=user.user_id, user_name=user.user_name, user_email=user.user_email)


def _author_display(user: User) -> str | None:
    """Author of a person's event by production's own rule (``pipeline._user_author_display``)."""
    from dooers.agents.server.handlers.pipeline import _user_author_display

    return _user_author_display(user)


def _media_family(mime_type: str | None) -> str:
    mime_type = mime_type or ""
    return "image" if mime_type.startswith("image/") else "audio" if mime_type.startswith("audio/") else "document"


async def _part(part: dict[str, Any], links: LinkResolver, *, agent: bool = False) -> Any:
    """The production part of one Maestro part; ``agent`` when an agent wrote the message."""
    kind = part["type"]
    if kind == "text":
        return WireS2C_TextPart(text=part["text"])
    if kind == "contact":
        return WireS2C_ContactPart(
            display_name=part.get("display_name") or "", vcard=part.get("vcard"), phones=deepcopy(part.get("phones") or [])
        )
    if kind == "media_link":
        family = part["kind"]
        return _MEDIA_PARTS[family](url=part["url"], **{key: part[key] for key in _MEDIA_LINK_FIELDS[family] if key in part})
    if kind == "attachment":
        family = _media_family(part.get("mime_type"))
        url = await links.url_for(part["artifact_id"])
        if agent:
            # An agent's file is the URL of send.image/audio/document the SDK uploaded: production
            # stores that URL with the send's metadata only (no file name of the URL, no ref_id).
            return _MEDIA_PARTS[family](url=url, **{key: part[key] for key in _AGENT_MEDIA_FIELDS[family] if key in part})
        # A person's upload: the temporary link is the URL production keeps; the artifact id is its ref_id.
        metadata = {key: part[key] for key in _ATTACHMENT_FIELDS[family] if key in part}
        return _MEDIA_PARTS[family](url=url, ref_id=part["artifact_id"], **metadata)
    raise ValueError(f"unknown Maestro content part {kind!r}")


def _with_agent_prefix(parts: list[Any], name: str) -> list[Any]:
    prefix = AGENT_PREFIX.format(name=name)
    for index, part in enumerate(parts):
        if isinstance(part, WireS2C_TextPart):
            return [*parts[:index], WireS2C_TextPart(text=f"{prefix} {part.text}"), *parts[index + 1 :]]
    return [WireS2C_TextPart(text=prefix), *parts]


def _base(event: dict[str, Any], **fields: Any) -> ThreadEvent:
    return ThreadEvent(
        id=event["event_id"],
        thread_id=event["thread_id"],
        run_id=event["payload"].get("run_id"),
        created_at=_time(event["created_at"]),
        **fields,
    )


def _own(actor: dict[str, Any], recipient_instance_id: str | None) -> bool:
    """The agent that reads the thread (an actor or a participant)."""
    return actor["kind"] == "agent" and actor["instance_agent_id"] == recipient_instance_id


def _participant_of(actor: dict[str, Any], participants: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The participant of an ``external`` actor (a channel contact or a public chat visitor), by ``participant_id``.

    Such an actor is a snapshot with no profile: its participant (the same ``participant_id``) has the name, e-mail and
    declared pre-chat (``context._user`` does the same for the live context). Any other actor has no lookup.
    """
    if actor["kind"] != "external":
        return None
    return next((p for p in participants or [] if p.get("participant_id") == actor.get("participant_id")), None)


async def _message(
    event: dict[str, Any],
    *,
    recipient_instance_id: str,
    agent_names: Mapping[str, str],
    multi_agent: bool,
    links: LinkResolver,
    assistant_name: str,
    channel: str | None = None,
    channel_meta: dict[str, Any] | None = None,
    participants: list[dict[str, Any]] | None = None,
) -> ThreadEvent:
    payload, actor = event["payload"], event["actor"]
    content = [await _part(part, links, agent=actor["kind"] == "agent") for part in payload["content"]]
    user = User(user_id="")
    if _own(actor, recipient_instance_id):
        # pipeline.py: ``author=event.data.get("author") or self._assistant_name``.
        legacy_actor, author = "assistant", payload.get("author") or assistant_name
    elif actor["kind"] == "agent" and multi_agent:
        # Several agents: the other agent speaks as a user named after it (design 7.5).
        legacy_actor = "user"
        author = agent_names.get(actor["instance_agent_id"]) or actor["display_name"]
        content = _with_agent_prefix(content, author)
    elif actor["kind"] == "agent":
        # Another agent, now alone in the thread: an assistant message under its own name.
        legacy_actor, author = "assistant", payload.get("author") or actor["display_name"] or None
    elif actor["kind"] == "system":
        legacy_actor, author = "system", actor["display_name"] or None
    else:
        # A channel contact or a public chat visitor: the actor is a snapshot with no profile, its participant
        # (the same ``participant_id``) has it (``context._user`` does the same for the live context).
        person = production_user(actor, _participant_of(actor, participants))
        legacy_actor, author, user = "user", _author_display(person), _stored(person)
    # F7: production's ``_assistant_delivery_data(context)`` on every assistant message (own agent, or
    # another agent alone in the thread, since both render as ``assistant`` here); never on a person's
    # or another agent's (multi_agent) message.
    # Final review fix 1: outside a delivery there is no live ``context.user.user_id`` to fall back to for
    # ``to_e164`` (only the thread's own, inbound ``channel_meta``, which has no reason to carry it); the
    # event's own ``payload["whatsapp"]`` — set at publish time from the handler's own
    # ``send.whatsapp.*(to_e164=..., instance_id=...)`` call (``sink.py``'s ``_resolve_whatsapp_route``) — is
    # the one value read-back can use instead, so the fallback stays ``user_id`` in name only.
    whatsapp_route = payload.get("whatsapp")
    whatsapp_to_e164 = whatsapp_route.get("to_e164") if isinstance(whatsapp_route, dict) else None
    delivery = assistant_delivery_data(channel, channel_meta, user_id=whatsapp_to_e164) if legacy_actor == "assistant" else None
    channel_delivery = payload.get(CHANNEL_DELIVERY_KEY)
    if delivery is not None and channel_delivery:
        # F7/Task 7: fold the latest ``channel_delivery.updated`` onto the stored row, as production's
        # ``_record_whatsapp_delivery`` folds it onto ``thread_event.data["delivery"]`` — never re-derived.
        merged = dict(delivery["delivery"])
        merged["status"] = channel_delivery["status"]
        provider_message_id = str(channel_delivery.get("provider_message_id") or "").strip()
        if provider_message_id:
            merged["wa_message_id"] = provider_message_id
        if channel_delivery.get("error_code") is not None:
            merged["error_code"] = channel_delivery["error_code"]
        error_message = str(channel_delivery.get("error") or "").strip()
        if error_message:
            merged["error_message"] = error_message[:500]
        delivery = {"delivery": merged}
    template = payload.get("template")
    data = None
    if template is not None:
        # pipeline.py stores send.template as ``data.template`` = {name, language, components}, plus
        # the same delivery data every other assistant message gets.
        data = {
            **(delivery or {}),
            "template": {
                "name": str(template.get("template_name") or "").strip(),
                "language": str(template.get("template_language") or "").strip(),
                "components": deepcopy(template.get("template_components") or []),
            },
        }
    elif delivery is not None:
        data = delivery
    elif legacy_actor == "user" and actor["kind"] == "external":
        # Production's ``_with_inbound_source_data`` on the visitor's own message.
        data = public_chat_source_data(channel, channel_meta)
    stored = payload.get("data")
    if isinstance(stored, dict) and stored:
        # What the message itself carries (``payload.data``: the ingress stamps a WhatsApp inbound's ``source`` there, with
        # per-message values the thread cannot give) wins key by key over what is derived from the thread above, so a
        # ``source`` of its own is never mixed with the derived one. The one rule inside and outside a delivery.
        data = {**(data or {}), **deepcopy(stored)}
    return _base(event, type="message", actor=legacy_actor, author=author, user=user, content=content or None, data=data)


def _form_element(element: dict[str, Any]) -> dict[str, Any]:
    model = _FORM_ELEMENTS.get(element.get("type", ""))
    return deepcopy(element) if model is None else model(**element).model_dump(exclude_unset=True)


def _agent_event(event: dict[str, Any], *, recipient_instance_id: str, assistant_name: str) -> ThreadEvent | None:
    """Events of ``send.reasoning``, ``tool_*``, ``chart`` and ``form``, as ``HandlerPipeline.execute`` writes them.

    The author of the own agent's events is ``assistant_name``; another agent's is its Maestro name.
    """
    kind, payload = event["type"], event["payload"]
    name = assistant_name if _own(event["actor"], recipient_instance_id) else event["actor"].get("display_name") or None
    if kind == "reasoning.created":
        return _base(event, type="reasoning", actor="assistant", author=payload.get("author") or name, data={"text": payload["text"]})
    if kind == "tool_call.created":
        data = {
            "id": payload["id"],
            "name": payload["name"],
            "display_name": payload.get("display_name"),
            "args": deepcopy(payload["args"]),
        }
        return _base(event, type="tool.call", actor="assistant", data=data)
    if kind in ("tool_result.created", "tool_transaction.created"):
        data = {
            "id": payload["id"],
            "name": payload["name"],
            "display_name": payload.get("display_name"),
            "args": deepcopy(payload.get("args")),
            "result": deepcopy(payload["result"]),
        }
        if kind == "tool_result.created":
            return _base(event, type="tool.result", actor="tool", data=data)
        return _base(event, type="tool.transaction", actor="assistant", data=data)
    if kind == "chart.created":
        chart = WireS2C_ChartEventData(**{key: deepcopy(payload.get(key)) for key in _CHART_KEYS})
        return _base(event, type="chart", actor="assistant", author=name, data=chart.model_dump(exclude_unset=True))
    if kind == "form.created":
        data = {
            "message": payload.get("message", ""),
            "elements": [_form_element(element) for element in payload.get("elements", [])],
            "submit_label": payload.get("submit_label", "Send"),
            "cancel_label": payload.get("cancel_label", "Cancel"),
            "size": payload.get("size", "medium"),
        }
        return _base(event, type="form", actor="assistant", author=name, data=data)
    return None


def _form_response(
    event: dict[str, Any],
    participants: list[dict[str, Any]] | None = None,
    channel: str | None = None,
    channel_meta: dict[str, Any] | None = None,
) -> ThreadEvent:
    """A person's answer to ``send.form``: production's ``form.response`` event, without content.

    Production stamps ``data.source`` on every inbound event (``_with_inbound_source_data``), so a public chat visitor's
    answer has it too: Maestro stores no ``data`` on a ``form.responded``, so it is derived from the thread's channel, as
    ``_message`` does for the visitor's own message.
    """
    payload, actor = event["payload"], event["actor"]
    person = production_user(actor, _participant_of(actor, participants))
    data = {"form_event_id": payload["form_event_id"], "values": deepcopy(payload["values"]), "cancelled": payload["cancelled"]}
    if actor["kind"] == "external":
        data.update(public_chat_source_data(channel, channel_meta) or {})
    return _base(event, type="form.response", actor="user", author=_author_display(person), user=_stored(person), data=data)


def _queue_update(
    event: dict[str, Any], *, recipient_instance_id: str, assistant_name: str, participants: list[dict[str, Any]] | None = None
) -> ThreadEvent | None:
    """A queue change: production's ``thread.update`` event (a title change writes none)."""
    payload, actor = event["payload"], event["actor"]
    if PREVIOUS_QUEUE not in payload:
        return None
    user = production_user(actor, _participant_of(actor, participants))
    if _own(actor, recipient_instance_id):
        user = User(user_id=user.user_id, user_name=assistant_name)
    if actor["kind"] == "agent":
        author = user.user_name  # pipeline.py (send.update_thread): the assistant name
    else:
        author = user.user_name or user.user_email or user.user_id or None  # router.py (thread.upsert)
    data = {"field": "queue", "from": payload[PREVIOUS_QUEUE], "to": payload["queue"]}
    return _base(event, type="thread.update", actor="system", author=author, user=_stored(user), data=data)


async def to_thread_event(
    event: dict,
    *,
    recipient_instance_id: str,
    agent_names: Mapping[str, str],
    multi_agent: bool,
    links: LinkResolver,
    assistant_name: str = DEFAULT_ASSISTANT_NAME,
    channel: str | None = None,
    channel_meta: dict[str, Any] | None = None,
    participants: list[dict[str, Any]] | None = None,
) -> ThreadEvent | None:
    """The production ``ThreadEvent`` of one folded Maestro event, or ``None`` without an equivalent.

    ``recipient_instance_id`` is the agent that runs the handler; ``agent_names`` gives the
    name of each agent by instance id; ``multi_agent`` is true when the thread has several
    active agents. An ``attachment`` gets its temporary link from ``links``. A person's
    attachment is an upload (``ref_id`` = artifact id, with its file metadata); an agent's is
    the URL its ``send.image``/``audio``/``document`` gave, which the SDK uploaded: production
    stores only the URL and that send's metadata, so the part has no ``ref_id`` (an image or
    audio has no file name either).
    ``assistant_name`` is ``AgentConfig.assistant_name``: production writes it as the author of
    its agent's events (and as the agent's ``user_name`` of a queue change), whatever name the
    agent has in Maestro.
    ``channel``/``channel_meta`` are the thread's own, at read time (``context.thread_from_view``/
    ``thread_at_cutoff``): ``message.created`` uses them to add ``data.delivery`` to an
    assistant message (``assistant_delivery_data``, F7), and a public chat visitor's message or form answer
    to add ``data.source``. Without them (the default, ``None``), no
    delivery is added, as before this field existed.
    ``participants`` are the thread's (the delivery context's): an ``external`` actor of a message carries
    only its ``participant_id``, so its name, e-mail and declared pre-chat come from its participant (on a message,
    a form answer and a queue change). The visitor's own message gets ``data.source`` from ``channel``/``channel_meta``
    (``public_chat_source_data``) unless its ``payload.data`` has one (a WhatsApp inbound's, stamped at ingest): the
    ``payload.data`` of any message is its ``data``, and its keys win over the derived ones.

    Actors: the own agent is ``assistant``; a person or channel contact is ``user``;
    ``tool_result.created`` is ``tool``; a queue change is ``system``. Another agent's
    message is ``assistant`` too, except with ``multi_agent``: then it is ``user``, its author
    is the agent's name and its text starts with ``[agente: <name>] ``. ``run.updated``,
    ``participant.added``, ``thread.created``, ``channel_delivery.updated`` and the delivery,
    execution and interaction events have no production event.
    """
    kind = event["type"]
    if kind == "message.created":
        return await _message(
            event,
            recipient_instance_id=recipient_instance_id,
            agent_names=agent_names,
            multi_agent=multi_agent,
            links=links,
            assistant_name=assistant_name,
            channel=channel,
            channel_meta=channel_meta,
            participants=participants,
        )
    if kind == "form.responded":
        return _form_response(event, participants, channel, channel_meta)
    if kind == "thread.updated":
        return _queue_update(event, recipient_instance_id=recipient_instance_id, assistant_name=assistant_name, participants=participants)
    return _agent_event(event, recipient_instance_id=recipient_instance_id, assistant_name=assistant_name)


def _in_join_order(participants: list[dict]) -> list[dict]:
    """The participants as production lists them: in the order they joined the thread.

    The Maestro context returns them ordered by a random participant UUID, so the order of the view
    means nothing. ``joined_sequence`` (a decimal string) says who joined first; at the same
    sequence the owner comes first, then the agent, then the rest, and the participant id breaks the
    last tie. The result is ``[owner, agent, added...]``, as production's ``Thread.users``. The owner is
    the human with the ``owner`` role, else (a channel thread) the first external participant that
    ``to_thread`` makes the owner: production lists a visitor or contact before the agent.
    """
    has_human_owner = any(p["kind"] == "human" and p.get("role") == "owner" for p in participants)
    externals = [p for p in participants if p["kind"] == "external" and p["state"] == "active"]
    channel_owner = None if has_human_owner else min(externals, key=_join_key, default=None)

    def rank(participant: dict) -> int:
        if participant is channel_owner or (participant["kind"] == "human" and participant.get("role") == "owner"):
            return 0
        return 1 if participant["kind"] == "agent" else 2

    return sorted(participants, key=lambda p: (int(p.get("joined_sequence") or 0), rank(p), p.get("participant_id") or ""))


def _join_key(participant: dict) -> tuple[int, str]:
    return int(participant.get("joined_sequence") or 0), participant.get("participant_id") or ""


def _thread_metadata(view: dict, owner: dict | None) -> dict[str, Any] | None:
    """``Thread.metadata`` of a public chat thread, as production stores it (``HandlerPipeline.setup``).

    Production keeps the metadata of the first ``event.create`` of the thread: the channel and its meta the
    visitor's browser sent, and the pre-chat it declared (``guest_name``/``guest_email``/``guest_phone``). Maestro
    has all of it: the thread's ``channel``/``channel_meta`` and the owner participant's ``guest_profile``. Only
    what exists is set. A thread of another channel has no such metadata here (``None``).
    """
    if (view.get("channel") or "").strip() != PUBLIC_CHAT_CHANNEL:
        return None
    metadata: dict[str, Any] = {"channel": PUBLIC_CHAT_CHANNEL}
    if view.get("channel_meta"):
        metadata["channel_meta"] = deepcopy(view["channel_meta"])
    profile = (owner or {}).get("guest_profile") or {}
    for key in ("name", "email", "phone"):
        if profile.get(key):
            metadata[f"guest_{key}"] = profile[key]
    return metadata


def to_thread(
    view: dict,
    *,
    last_event_at: datetime | None = None,
    recipient_instance_id: str | None = None,
    assistant_name: str = DEFAULT_ASSISTANT_NAME,
) -> Thread:
    """The production ``Thread`` of a Maestro thread view (``Public.Thread``).

    ``agent_id`` is the agent that created the thread, else the first agent participant.
    ``owner`` is the person with the ``owner`` role, else the first channel contact (a
    production channel thread belongs to the contact). ``users`` are the active participants
    in the order they joined (``_in_join_order``), the agent included, as production lists them: the reading agent
    (``recipient_instance_id``) under ``assistant_name``, as ``thread_access.agent_participant``
    lists it; without one, every agent under its Maestro name. The view has no time of the
    last event: ``last_event_at`` is the caller's, else ``updated_at``.
    """
    participants = _in_join_order(view["participants"])
    active = [participant for participant in participants if participant["state"] == "active"]
    agents = [p for p in active if p["kind"] == "agent"] or [p for p in participants if p["kind"] == "agent"]
    owner = next((p for p in participants if p["kind"] == "human" and p["role"] == "owner"), None) or next(
        (p for p in active if p["kind"] == "external"), None
    )
    updated_at = _time(view["updated_at"])
    return Thread(
        metadata=_thread_metadata(view, owner),
        id=view["thread_id"],
        agent_id=view.get("owner_instance_agent_id") or (agents[0]["instance_agent_id"] if agents else ""),
        organization_id=view["organization_id"],
        workspace_id=view["workspace_id"] or "",
        owner=production_user(owner) if owner is not None else User(user_id=""),
        users=[
            User(user_id=participant["instance_agent_id"], user_name=assistant_name)
            if _own(participant, recipient_instance_id)
            else production_user(participant)
            for participant in active
        ],
        title=view["title"] or None,
        queue=view.get("queue"),
        created_at=_time(view["created_at"]),
        updated_at=updated_at,
        last_event_at=last_event_at or updated_at,
    )


def to_run(event: dict) -> Run:
    """The production ``Run`` of one ``run.updated`` event.

    Maestro records the executing agent when ``send.run_start()`` has no ``agent_id``
    (production stores ``None``), so the writer's own id is ``None`` here. A run is several
    events: :func:`to_runs` folds them.
    """
    payload, created_at = event["payload"], _time(event["created_at"])
    agent_id = payload["agent_id"]
    return Run(
        id=payload["run_id"],
        thread_id=event["thread_id"],
        agent_id=None if agent_id == event["actor"].get("instance_agent_id") else agent_id,
        status=payload["status"],
        started_at=created_at,
        ended_at=None if payload["status"] == "running" else created_at,
        error=payload.get("error"),
    )


def to_runs(events: list[dict]) -> list[Run]:
    """The production ``Run`` of each ``run_id`` in ``events``, in the order the runs started.

    As production's ``run_start`` then ``update_run``: ``agent_id`` and ``started_at`` come
    from the first ``run.updated`` of the run; status, error and ``ended_at`` from the last.
    """
    runs: dict[str, Run] = {}
    for event in events:
        if event["type"] != "run.updated":
            continue
        run = to_run(event)
        first = runs.get(run.id)
        runs[run.id] = run if first is None else run.model_copy(update={"agent_id": first.agent_id, "started_at": first.started_at})
    return list(runs.values())
