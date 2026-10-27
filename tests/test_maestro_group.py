import asyncio

from dooers.agents.server.maestro.group import (
    SILENCE,
    classification_messages,
    decide_group_turn,
    is_silence,
    parse_owners,
    plan_group_turn,
    pre_decision,
)

FIN, JUR, GEN, GEN2 = (f"{c * 8}-0000-4000-8000-00000000000{i}" for i, c in enumerate("abcd", 1))
DIRECTORY = {
    FIN: {"name": "Agente Financeiro", "specialty": "finanças empresariais"},
    JUR: {"name": "Agente Jurídico", "specialty": "direito empresarial"},
    GEN: {"name": "Agente Maestro", "specialty": ""},
    GEN2: {"name": "Agente Maestro 2", "specialty": ""},
}
PART = {FIN: "p-fin", JUR: "p-jur", GEN: "p-gen", GEN2: "p-gen2"}
HUMAN = {"kind": "human", "principal_id": "h", "participant_id": "p-h", "display_name": "Humano"}


def agent(instance):
    return {
        "kind": "agent",
        "instance_agent_id": instance,
        "principal_id": instance,
        "participant_id": PART[instance],
        "display_name": "Agente",
    }


def message(event_id, actor, text, mentions=()):
    return {
        "event_id": event_id,
        "type": "message.created",
        "actor": actor,
        "payload": {"content": [{"type": "text", "text": text}], "mentions": list(mentions)},
    }


def snapshot(events, members=(FIN, JUR, GEN, GEN2), trigger=None):
    participants = [{**HUMAN, "state": "active"}] + [
        {"kind": "agent", "state": "active", "instance_agent_id": m, "principal_id": m, "participant_id": PART[m]} for m in members
    ]
    return {"participants": participants, "events": events, "trigger_event": trigger or events[-1]}


def own(instance):
    return (DIRECTORY[instance]["name"], DIRECTORY[instance]["specialty"])


def test_fixed_rules_need_no_model():
    question = message("e1", HUMAN, "o que é DRE?")
    assert pre_decision(snapshot([question], members=(FIN,)), FIN, PART[FIN]).reason == "solo"
    mention = message("e1", HUMAN, "@Agente Jurídico prazo de aviso?", mentions=[PART[JUR]])
    assert pre_decision(snapshot([mention]), JUR, PART[JUR]).reason == "mentioned"
    assert pre_decision(snapshot([mention]), FIN, PART[FIN]).answer is False
    assert pre_decision(snapshot([question]), FIN, PART[FIN]) is None


def test_round_turn_triggered_by_previous_agent_still_classifies():
    events = [message("e1", HUMAN, "o que é DRE?"), message("e2", agent(GEN), "DRE é...")]
    assert pre_decision(snapshot(events), FIN, PART[FIN]) is None
    asked = [message("e1", HUMAN, "x"), message("e2", agent(GEN), "@Agente Financeiro confirma?", mentions=[PART[FIN]])]
    assert pre_decision(snapshot(asked), FIN, PART[FIN]).reason == "mentioned"


def test_specialist_owns_its_topic_and_others_yield():
    events = [message("e1", HUMAN, "what is a cash flow statement?")]
    owners = ("Agente Financeiro",)
    assert plan_group_turn(snapshot(events), FIN, own(FIN), DIRECTORY, owners).answer is True
    for other in (JUR, GEN, GEN2):
        assert plan_group_turn(snapshot(events), other, own(other), DIRECTORY, owners).reason == "other_specialist"


def test_general_topic_goes_to_the_first_generalist_only():
    events = [message("e1", HUMAN, "valeu, obrigado!")]
    assert plan_group_turn(snapshot(events), JUR, own(JUR), DIRECTORY, ()).reason == "generalist_present"
    assert plan_group_turn(snapshot(events), GEN, own(GEN), DIRECTORY, ()).answer is True
    answered = [*events, message("e2", agent(GEN), "Por nada!")]
    assert plan_group_turn(snapshot(answered), GEN2, own(GEN2), DIRECTORY, ()).reason == "already_answered"


def test_general_topic_without_generalists_goes_to_the_first_agent():
    events = [message("e1", HUMAN, "receita de bolo?")]
    assert plan_group_turn(snapshot(events, members=(FIN, JUR)), FIN, own(FIN), DIRECTORY, ()).answer is True


def test_unreadable_classification_answers_with_silence_still_possible():
    events = [message("e1", HUMAN, "x")]
    assert plan_group_turn(snapshot(events), FIN, own(FIN), DIRECTORY, None).reason == "unclassified"


def test_parse_owners_keeps_only_known_names():
    names = ["Agente Financeiro", "Agente Jurídico"]
    assert parse_owners('{"owners": ["agente financeiro", "Outro"]}', names) == ("Agente Financeiro",)
    assert parse_owners('Claro! {"owners": []}', names) == ()
    assert parse_owners("não sei", names) is None


def test_classification_prompt_lists_agents_and_the_question():
    events = [message("e1", HUMAN, "preço e campanha?")]
    messages = classification_messages(snapshot(events), FIN, own(FIN), DIRECTORY)
    assert "- Agente Financeiro: finanças empresariais" in messages[0]["content"]
    assert "- Agente Maestro: generalista" in messages[0]["content"]
    assert messages[1]["content"].endswith("Última mensagem do Humano:\npreço e campanha?")


def test_decide_group_turn_end_to_end_with_fake_model():
    class Memory:
        def __init__(self, snap, instance):
            self._snap = snap
            self._envelope = {"instance_agent_id": instance, "recipient_participant_id": PART[instance]}

        async def snapshot(self):
            return self._snap

    events = [message("e1", HUMAN, "o que é renda fixa?"), message("e2", agent(GEN), "Renda fixa é...")]

    async def classify(_messages):
        return '{"owners": ["Agente Financeiro"]}'

    turn = asyncio.run(
        decide_group_turn(Memory(snapshot(events), FIN), classify=classify, directory=DIRECTORY, system_prompt="Você é o Financeiro.")
    )
    assert turn.answer and turn.reason == "own_topic"
    assert turn.messages[-1] == {"role": "user", "content": "Agente Maestro disse:\nRenda fixa é..."}
    assert "Nesta rodada já responderam: Agente Maestro." in turn.messages[0]["content"]
    assert SILENCE in turn.messages[0]["content"]

    yielded = asyncio.run(decide_group_turn(Memory(snapshot(events), JUR), classify=classify, directory=DIRECTORY))
    assert yielded.answer is False and yielded.messages == ()

    async def broken(_messages):
        raise RuntimeError("model down")

    fallback = asyncio.run(decide_group_turn(Memory(snapshot(events), JUR), classify=broken, directory=DIRECTORY))
    assert fallback.answer and fallback.reason == "unclassified"


def test_silence_marker():
    assert is_silence("[SILENCIO]") and is_silence(" [silencio]. ")
    assert not is_silence("Renda fixa é... [SILENCIO]")
