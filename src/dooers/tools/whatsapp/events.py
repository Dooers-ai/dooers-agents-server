"""Map handler send events to WhatsApp tools ``/messages`` payloads."""

from __future__ import annotations

from typing import Any

from dooers.agents.server.handlers.send import AgentEvent


def body_for_event(event: AgentEvent) -> dict[str, Any] | None:
    w = event.data.get("whatsapp") or {}
    to_e164 = w.get("to_e164")
    instance_id = w.get("instance_id")
    if not to_e164 or not instance_id:
        return None
    st = event.send_type
    payload: dict[str, Any] = {
        "instance_id": instance_id,
        "to_e164": to_e164,
        "send_type": st,
    }
    d = event.data
    if st == "text":
        payload["text"] = d.get("text", "")
    elif st == "image":
        payload["url"] = d.get("url")
        payload["mime_type"] = d.get("mime_type")
    elif st == "document":
        payload["url"] = d.get("url")
        payload["filename"] = d.get("filename")
        payload["mime_type"] = d.get("mime_type")
    elif st == "audio":
        payload["url"] = d.get("url")
        payload["mime_type"] = d.get("mime_type")
        payload["duration"] = d.get("duration")
    elif st == "contact":
        payload["display_name"] = d.get("display_name") or ""
        payload["vcard"] = d.get("vcard")
        payload["phones"] = d.get("phones") or []
    elif st == "template":
        payload["template_name"] = d.get("template_name") or ""
        payload["template_language"] = d.get("template_language") or ""
        comps = d.get("template_components")
        if isinstance(comps, list):
            payload["template_components"] = comps
    else:
        return None
    return payload
