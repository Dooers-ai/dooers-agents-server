"""Conversa com vários agentes no Maestro: quem fala é decidido antes de gerar a resposta.

O Maestro entrega a mensagem do humano a cada agente da conversa (um de cada vez, com
AGENT_TURN_POLICY=sequential). Deixar o próprio modelo decidir "falo ou não" dentro do prompt de
resposta falha com frequência: agentes respondem fora da especialidade e quem vem antes na fila toma a
pergunta do especialista certo. Aqui a decisão tem três camadas:

1. regras fixas, sem modelo: conversa só com este agente ou menção estruturada a ele -> responde;
   mensagem humana que menciona só outros agentes -> silêncio;
2. classificação curta pelo modelo do agente (``classify``): de quem é o assunto da última mensagem
   do humano, como JSON ``{"owners": [...]}``;
3. decisão no código a partir da classificação e de quem já falou nesta rodada.

Só quem decide responder recebe ``messages`` prontas (autoria de cada mensagem, participantes e
regras de resposta). O SDK não chama modelo nenhum: ``classify`` é do agente.

Nomes e especialidades vêm do agente (``directory``, por ``instance_agent_id``): o Maestro guarda
nomes genéricos ("Humano"/"Agente").
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

SILENCE = "[SILENCIO]"

AgentDirectory = Mapping[str, Mapping[str, str]]
"""``instance_agent_id`` -> ``{"name": ..., "specialty": ...}``; especialidade vazia = generalista."""


@dataclass(frozen=True)
class MaestroGroupTurn:
    """Decisão da vez: ``answer`` com ``messages`` para o modelo, ou silêncio (``send.silence()``)."""

    answer: bool
    reason: str
    messages: tuple[dict[str, str], ...] = ()
    owners: tuple[str, ...] | None = None


def _key(value: Any) -> str:
    return str(value or "").lower()


def _text(event: dict[str, Any]) -> str:
    return "\n".join(
        part["text"] if part["type"] == "text" else f"[anexo {part.get('filename') or part.get('artifact_id', '')}]"
        for part in event["payload"]["content"]
    )


def _entry(directory: AgentDirectory, instance_id: Any) -> tuple[str, str]:
    raw = {_key(k): v for k, v in directory.items()}.get(_key(instance_id), {})
    return (str(raw.get("name", "")).strip() or "Outro agente", str(raw.get("specialty", "")).strip())


def _agents(snapshot: dict[str, Any], own_instance_id: str) -> list[dict[str, Any]]:
    return [
        p
        for p in snapshot.get("participants", [])
        if p.get("kind") == "agent"
        and p.get("state", "active") == "active"
        and _key(p.get("instance_agent_id") or p.get("principal_id")) != _key(own_instance_id)
    ]


def _roster(snapshot: dict[str, Any], own_instance_id: str, directory: AgentDirectory) -> list[tuple[str, str]]:
    return [_entry(directory, p.get("instance_agent_id") or p.get("principal_id")) for p in _agents(snapshot, own_instance_id)]


def _roster_text(roster: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {name}: {specialty or 'generalista'}" for name, specialty in roster)


def _messages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    events = [event for event in snapshot["events"] if event["type"] == "message.created"]
    trigger = snapshot.get("trigger_event")
    if trigger and trigger["event_id"] not in {event["event_id"] for event in events}:
        events.append(trigger)
    return events


def _author(event: dict[str, Any], own_instance_id: str, directory: AgentDirectory) -> str | None:
    """None = o próprio agente."""
    actor = event["actor"]
    if actor["kind"] != "agent":
        return "Humano"
    if _key(actor.get("instance_agent_id")) == _key(own_instance_id):
        return None
    return _entry(directory, actor.get("instance_agent_id"))[0]


def _round(snapshot: dict[str, Any], own_instance_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Última mensagem do humano e as respostas de agentes depois dela (a rodada atual)."""
    events = _messages(snapshot)
    humans = [i for i, event in enumerate(events) if event["actor"]["kind"] != "agent"]
    if not humans:
        return None, []
    after = events[humans[-1] + 1 :]
    return events[humans[-1]], [e for e in after if _key(e["actor"].get("instance_agent_id")) != _key(own_instance_id)]


def pre_decision(snapshot: dict[str, Any], own_instance_id: str, own_participant_id: str) -> MaestroGroupTurn | None:
    """Casos decididos sem modelo; None quando é preciso classificar."""
    trigger = snapshot.get("trigger_event") or {}
    if not _agents(snapshot, own_instance_id):
        return MaestroGroupTurn(True, "solo")
    mentions_of = lambda event: {_key(m) for m in (event or {}).get("payload", {}).get("mentions", [])}  # noqa: E731
    mentions = mentions_of(trigger)
    # Na rodada sequencial o gatilho das vezes seguintes é a resposta do agente anterior: as menções
    # que valem para a rodada são as da mensagem do humano que a abriu.
    from_agent = trigger.get("actor", {}).get("kind") == "agent"
    human, _ = _round(snapshot, own_instance_id)
    round_mentions = mentions_of(human) if from_agent else mentions
    if _key(own_participant_id) in mentions | round_mentions:
        return MaestroGroupTurn(True, "mentioned")
    agent_ids = {_key(p.get("participant_id")) for p in snapshot.get("participants", []) if p.get("kind") == "agent"}
    if round_mentions & agent_ids:
        return MaestroGroupTurn(False, "not_mentioned")
    return None


def classification_messages(
    snapshot: dict[str, Any], own_instance_id: str, own: tuple[str, str], directory: AgentDirectory, limit: int = 12
) -> list[dict[str, str]]:
    """Pergunta curta ao modelo: de quem é o assunto da última mensagem do humano."""
    roster = [own, *_roster(snapshot, own_instance_id, directory)]
    human, _ = _round(snapshot, own_instance_id)
    history = "\n\n".join(f"{_author(e, own_instance_id, directory) or own[0]} disse:\n{_text(e)}" for e in _messages(snapshot)[-limit:])
    system = "\n".join(
        [
            "Você classifica uma conversa com vários agentes de IA. Não responda à conversa.",
            "Agentes e especialidades:",
            _roster_text(roster),
            "Diga quais agentes têm a especialidade necessária para responder à última mensagem do Humano.",
            "Use os nomes exatamente como na lista. Generalistas nunca são donos de assunto.",
            "Lista vazia quando a mensagem é cumprimento, agradecimento, despedida ou assunto que nenhum especialista cobre.",
            'Responda só com JSON, sem texto em volta: {"owners": ["Nome", ...]}',
        ]
    )
    user = f"{history}\n\nÚltima mensagem do Humano:\n{_text(human) if human else ''}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_owners(text: str, names: list[str]) -> tuple[str, ...] | None:
    """Nomes válidos da classificação, na grafia da lista; None quando não dá para ler."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        value = json.loads(match.group(0)) if match else None
    except ValueError:
        return None
    owners = value.get("owners") if isinstance(value, dict) else None
    if not isinstance(owners, list):
        return None
    canonical = {name.lower(): name for name in names}
    return tuple(dict.fromkeys(canonical[o.strip().lower()] for o in owners if isinstance(o, str) and o.strip().lower() in canonical))


def plan_group_turn(
    snapshot: dict[str, Any], own_instance_id: str, own: tuple[str, str], directory: AgentDirectory, owners: tuple[str, ...] | None
) -> MaestroGroupTurn:
    """Decisão a partir da classificação e de quem já falou na rodada."""
    if owners is None:
        return MaestroGroupTurn(True, "unclassified")
    name, specialty = own
    if name in owners:
        return MaestroGroupTurn(True, "own_topic", owners=owners)
    if owners:
        return MaestroGroupTurn(False, "other_specialist", owners=owners)
    _, answered = _round(snapshot, own_instance_id)
    if answered:
        return MaestroGroupTurn(False, "already_answered", owners=owners)
    generalists = [n for n, s in _roster(snapshot, own_instance_id, directory) if not s]
    if specialty and generalists:
        return MaestroGroupTurn(False, "generalist_present", owners=owners)
    return MaestroGroupTurn(True, "general", owners=owners)


def answer_messages(
    system_prompt: str,
    snapshot: dict[str, Any],
    own_instance_id: str,
    directory: AgentDirectory,
    limit: int = 20,
    may_stay_silent: bool = True,
) -> list[dict[str, str]]:
    """Mensagens para gerar a resposta: autor de cada fala, participantes e regras de resposta.

    Quem foi mencionado (``may_stay_silent=False``) tem que responder: as regras não oferecem silêncio.
    """
    _, answered = _round(snapshot, own_instance_id)
    spoke = list(dict.fromkeys(_author(e, own_instance_id, directory) for e in answered))
    me = _entry(directory, own_instance_id)[0]
    rules = [
        "Regras da conversa:",
        f"- Você é o {me}. Fale só por você; nunca se apresente nem responda como outro agente."
        if me != "Outro agente"
        else "- Fale só por você; nunca se apresente nem responda como outro agente.",
        "- Você está numa conversa com outros agentes; cada mensagem traz o nome de quem a disse.",
        "- Responda à última mensagem do Humano.",
        f"- Nesta rodada já responderam: {', '.join(n for n in spoke if n)}. Acrescente só o que é da sua especialidade"
        " e ainda não foi dito; nunca repita nem reformule o que eles disseram."
        if spoke
        else "- Você é o primeiro a responder nesta rodada.",
        f"- Se não tiver nada novo a acrescentar, escreva exatamente {SILENCE} e nada mais."
        if may_stay_silent
        else "- Você foi chamado pelo nome: responda, mesmo que brevemente.",
    ]
    roster = _roster_text(_roster(snapshot, own_instance_id, directory))
    system = "\n\n".join(
        p for p in (system_prompt, f"Participantes além de você:\n- Humano\n{roster}" if roster else "", "\n".join(rules)) if p
    )
    messages = [{"role": "system", "content": system}]
    for event in _messages(snapshot)[-limit:]:
        author = _author(event, own_instance_id, directory)
        if author is None:
            messages.append({"role": "assistant", "content": _text(event)})
        else:
            messages.append({"role": "user", "content": f"{author} disse:\n{_text(event)}"})
    return messages


def is_silence(text: str) -> bool:
    return (text or "").strip().strip(".").upper() == SILENCE


async def decide_group_turn(
    memory: Any,
    *,
    classify: Callable[[list[dict[str, str]]], Awaitable[str]],
    directory: AgentDirectory,
    system_prompt: str = "",
    history_limit: int = 20,
) -> MaestroGroupTurn:
    """Decide se este agente fala nesta vez. ``classify`` recebe mensagens OpenAI e devolve o texto do modelo.

    Falha da classificação (exceção ou JSON ilegível) não silencia: o agente responde com as regras de
    resposta, que ainda permitem ``[SILENCIO]``.
    """
    snapshot = await memory.snapshot()
    envelope = memory._envelope
    own_instance_id, own_participant_id = envelope["instance_agent_id"], envelope["recipient_participant_id"]
    own = _entry(directory, own_instance_id)
    decision = pre_decision(snapshot, own_instance_id, own_participant_id)
    if decision is None:
        names = [own[0], *(n for n, _ in _roster(snapshot, own_instance_id, directory))]
        try:
            owners = parse_owners(await classify(classification_messages(snapshot, own_instance_id, own, directory)), names)
        except Exception:  # noqa: BLE001 - classificação é auxiliar; a resposta ainda pode silenciar
            owners = None
        decision = plan_group_turn(snapshot, own_instance_id, own, directory, owners)
    if not decision.answer:
        return decision
    messages = answer_messages(
        system_prompt, snapshot, own_instance_id, directory, history_limit, may_stay_silent=decision.reason != "mentioned"
    )
    return MaestroGroupTurn(True, decision.reason, tuple(messages), decision.owners)
