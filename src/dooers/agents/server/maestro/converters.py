"""Conversão textual explícita. Autores nunca viram instruções system."""

from __future__ import annotations

import json
from typing import Any


def history_messages(events: list[dict[str, Any]], recipient_instance_id: str, format: str = "openai") -> list[dict[str, Any]]:
    from .client import MaestroContextError

    if format not in {"openai", "anthropic", "google", "cohere", "voyage", "langchain", "openai_completions", "openai_responses"}:
        raise MaestroContextError("CONTEXT_INVALID")
    messages = []
    for event in events:
        if event["type"] != "message.created":
            continue
        actor = event["actor"]
        own = actor["kind"] == "agent" and actor["instance_agent_id"] == recipient_instance_id
        # JSON delimita a autoria como dado. IDs e sequência preservados no texto;
        # `user` é papel de entrada do provedor, nunca identidade humana fabricada.
        parts = [
            part["text"] if part["type"] == "text" else f"[attachment artifact_id={part['artifact_id']}]"
            for part in event["payload"]["content"]
        ]
        metadata = json.dumps(
            {"actor": actor, "event_id": event["event_id"], "sequence": event["sequence"]}, ensure_ascii=True, separators=(",", ":")
        )
        text = f"[maestro source {metadata}]\n" + "\n".join(parts)
        if format == "google":
            message = {"role": "model" if own else "user", "parts": [{"text": text}]}
        elif format == "cohere":
            message = {"role": "CHATBOT" if own else "USER", "message": text}
        elif format == "openai_responses":
            message = {"role": "assistant" if own else "user", "content": [{"type": "output_text" if own else "input_text", "text": text}]}
        else:
            message = {"role": "assistant" if own else "user", "content": text}
        messages.append(message)
    return messages
