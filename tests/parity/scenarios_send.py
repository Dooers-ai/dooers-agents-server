"""Scenarios for the ``send.*`` functions a handler yields (one function per scenario).

Each value of a public ``Literal`` parameter that changes what the client renders has its
own scenario too: every ``chart_type`` (``chart`` is ``bar``), every ``form_text``
``input_type`` and every form element builder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dooers.agents.server.protocol.models import User
from tests.parity.model import (
    FORM_EVENT_REF,
    OTHER_AGENT_ID,
    QUEUES,
    Handler,
    Scenario,
    ScenarioInput,
    Turn,
    capture_histories,
    capture_incoming,
)
from tests.parity.samples import pdf_bytes, png_bytes

FORM_RESPONSE_FIELDS = ("message", "content", "form_data", "form_cancelled", "form_event_id")


def _reply(build: Callable[[Any, Any], list[Any]]) -> Handler:
    """Handler that wraps the events from ``build(on, send)`` in one run."""

    async def handler(on, send, memory, analytics, settings):
        yield send.run_start()
        for event in build(on, send):
            yield event
        yield send.run_end()

    return handler


def _form_elements(send: Any) -> list[dict[str, Any]]:
    options = [{"value": "basico", "label": "Básico"}, {"value": "pro", "label": "Pro"}]
    return [
        send.form_text("nome", label="Nome", order=1, required=True, placeholder="Seu nome", input_type="text"),
        send.form_textarea("obs", label="Observações", order=2, rows=4, default="Nada"),
        send.form_select("plano", label="Plano", options=options, order=3, default="pro", placeholder="Escolha"),
        send.form_radio("contato", label="Contato", options=options, order=4, variant="button"),
        send.form_checkbox("extras", label="Extras", options=options, order=5, default=["pro"]),
        send.form_file("anexo", label="Anexo", upload_url="/uploads", order=6, accept="application/pdf", multiple=True),
    ]


async def _form_round_trip(on, send, memory, analytics, settings):
    """First turn asks with a form; the ``form.response`` turn records what the handler got."""
    yield send.run_start()
    if on.form_data is not None or on.form_cancelled or on.form_event_id is not None:
        capture_incoming(on, FORM_RESPONSE_FIELDS)
        yield send.text("Tudo bem, cancelado." if on.form_cancelled else "Cadastro concluído.")
    else:
        yield send.form(
            "Complete seu cadastro",
            [
                send.form_text("nome", label="Nome", required=True),
                send.form_select("plano", label="Plano", options=[{"value": "pro", "label": "Pro"}]),
            ],
            submit_label="Enviar",
            cancel_label="Cancelar",
            size="small",
        )
    yield send.run_end()


async def _run_failed(on, send, memory, analytics, settings):
    yield send.run_start()
    yield send.text("Não consegui consultar o pedido.")
    yield send.run_end(status="failed", error="Serviço de pedidos indisponível")


async def _run_other_agent(on, send, memory, analytics, settings):
    yield send.run_start(agent_id=OTHER_AGENT_ID)
    yield send.text("Aqui é o agente de cobrança.")
    yield send.run_end()


_PLAN_OPTIONS = [{"value": "basico", "label": "Básico"}, {"value": "pro", "label": "Pro"}]

#: One scenario per ``form_text`` ``input_type`` (the ``Literal`` in ``AgentSend.form_text``).
FORM_TEXT_INPUT_TYPES = ("text", "password", "email", "number", "date")


def _form_text_elements(input_type: str) -> Callable[[Any], list[dict[str, Any]]]:
    return lambda send: [send.form_text("campo", label="Campo", required=True, placeholder="Digite", input_type=input_type)]


#: One scenario per form element builder other than ``form_text`` (covered by
#: ``FORM_TEXT_INPUT_TYPES``). Radio and checkbox send both ``variant`` values.
FORM_ELEMENT_BUILDERS: dict[str, Callable[[Any], list[dict[str, Any]]]] = {
    "textarea": lambda send: [send.form_textarea("obs", label="Observações", rows=4, default="Nada", placeholder="Escreva")],
    "select": lambda send: [send.form_select("plano", label="Plano", options=_PLAN_OPTIONS, default="pro", placeholder="Escolha")],
    "radio": lambda send: [
        send.form_radio("contato", label="Contato", options=_PLAN_OPTIONS, default="pro", variant="native"),
        send.form_radio("prazo", label="Prazo", options=_PLAN_OPTIONS, order=1, variant="button"),
    ],
    "checkbox": lambda send: [
        send.form_checkbox("extras", label="Extras", options=_PLAN_OPTIONS, default=["pro"], variant="native"),
        send.form_checkbox("canais", label="Canais", options=_PLAN_OPTIONS, order=1, variant="button"),
    ],
    "file": lambda send: [send.form_file("anexo", label="Anexo", upload_url="/uploads", accept="application/pdf", multiple=True)],
}


def _single_form(elements: Callable[[Any], list[dict[str, Any]]]) -> Handler:
    return _reply(lambda on, send: [send.form("Preencha o campo", elements(send), submit_label="Enviar")])


_MONTHLY_SALES = [{"mes": "jan", "vendas": 10, "meta": 12}, {"mes": "fev", "vendas": 14, "meta": 12}]

#: ``chart_type`` -> (data, x_key, y_keys) for the chart types other than ``bar`` (scenario ``chart``).
CHART_VARIANTS: dict[str, tuple[list[dict[str, Any]], str, list[str]]] = {
    "bar_horizontal": (_MONTHLY_SALES, "mes", ["vendas", "meta"]),
    "stacked_bar": (_MONTHLY_SALES, "mes", ["vendas", "meta"]),
    "line": (_MONTHLY_SALES, "mes", ["vendas", "meta"]),
    "area": (_MONTHLY_SALES, "mes", ["vendas", "meta"]),
    "pie": ([{"plano": "basico", "clientes": 30}, {"plano": "pro", "clientes": 12}], "plano", ["clientes"]),
    "donut": ([{"plano": "basico", "clientes": 30}, {"plano": "pro", "clientes": 12}], "plano", ["clientes"]),
    "scatter": ([{"preco": 10, "vendas": 40}, {"preco": 20, "vendas": 25}], "preco", ["vendas"]),
}


def _chart(chart_type: str) -> Handler:
    data, x_key, y_keys = CHART_VARIANTS[chart_type]
    return _reply(
        lambda on, send: [
            send.chart(
                chart_type=chart_type,
                data=[dict(row) for row in data],
                x_key=x_key,
                y_keys=list(y_keys),
                title=f"Gráfico {chart_type}",
                series=[send.chart_series(key, label=key.capitalize()) for key in y_keys],
            )
        ]
    )


#: Media URLs that answer (``ScenarioInput.media``): in Maestro mode the SDK uploads these files.
#: The PNG and the PDF are valid, as Maestro's content inspection requires (OGG has no structure check).
ONLINE_MEDIA: dict[str, tuple[bytes, str]] = {
    "https://cdn.example.com/online/grafico.png": (png_bytes(), "image/png"),
    "https://cdn.example.com/online/resposta.ogg": (b"OggS\x00parity-audio", "audio/ogg"),
    "https://cdn.example.com/online/relatorio.pdf": (pdf_bytes(), "application/pdf"),
}


async def _media_upload(on, send, memory, analytics, settings):
    """Turn 1 sends files from URLs that answer; turn 2 reads them back in the history."""
    yield send.run_start()
    if on.message == "Resuma a conversa":
        await capture_histories(memory)
        yield send.text("Resumo pronto.")
    else:
        image, audio, document = ONLINE_MEDIA
        yield send.image(image, mime_type="image/png", alt="Gráfico de vendas")
        yield send.audio(audio, mime_type="audio/ogg", duration=3.5)
        yield send.document(document, filename="relatorio.pdf", mime_type="application/pdf")
    yield send.run_end()


def _form_response_turns(data: dict[str, Any]) -> tuple[Turn, ...]:
    return (
        Turn(message="Quero me cadastrar"),
        Turn(message="", event_type="form.response", data=data),
    )


SEND_SCENARIOS: dict[str, Scenario] = {
    "text": Scenario(
        handler=_reply(lambda on, send: [send.text("Olá! Como posso ajudar?")]),
        incoming=ScenarioInput(),
    ),
    "text_author": Scenario(
        handler=_reply(lambda on, send: [send.text("Seu boleto foi gerado.", author="Agente Financeiro")]),
        incoming=ScenarioInput(),
    ),
    "image": Scenario(
        handler=_reply(
            lambda on, send: [send.image("https://cdn.example.com/grafico.png", mime_type="image/png", alt="Gráfico de vendas")]
        ),
        incoming=ScenarioInput(),
    ),
    "document": Scenario(
        handler=_reply(
            lambda on, send: [
                send.document(
                    "https://cdn.example.com/relatorio.pdf",
                    filename="relatorio.pdf",
                    mime_type="application/pdf",
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    "audio": Scenario(
        handler=_reply(lambda on, send: [send.audio("https://cdn.example.com/resposta.ogg", mime_type="audio/ogg", duration=3.5)]),
        incoming=ScenarioInput(),
    ),
    "media_upload": Scenario(
        handler=_media_upload,
        incoming=ScenarioInput(turns=(Turn(message="Me mande o relatório"), Turn(message="Resuma a conversa")), media=ONLINE_MEDIA),
        history_formats=("openai", "anthropic"),
    ),
    "contact": Scenario(
        handler=_reply(
            lambda on, send: [
                send.contact(
                    display_name="Suporte Dooers",
                    vcard="BEGIN:VCARD\nVERSION:3.0\nFN:Suporte Dooers\nEND:VCARD",
                    phones=[{"phone": "+551130000000", "type": "WORK"}],
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    "template": Scenario(
        handler=_reply(
            lambda on, send: [
                send.template(
                    "order_update",
                    "pt_BR",
                    template_components=[{"type": "body", "parameters": [{"type": "text", "text": "42"}]}],
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    "reasoning": Scenario(
        handler=_reply(lambda on, send: [send.reasoning("Vou verificar o status do pedido antes de responder.")]),
        incoming=ScenarioInput(),
    ),
    "tool_call": Scenario(
        handler=_reply(lambda on, send: [send.tool_call("buscar_pedido", {"pedido": "42"}, display_name="Buscar pedido", id="call-42")]),
        incoming=ScenarioInput(),
    ),
    "tool_result": Scenario(
        handler=_reply(
            lambda on, send: [
                send.tool_result(
                    "buscar_pedido",
                    {"status": "enviado"},
                    args={"pedido": "42"},
                    display_name="Buscar pedido",
                    id="call-42",
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    "tool_transaction": Scenario(
        handler=_reply(
            lambda on, send: [
                send.tool_transaction(
                    "buscar_pedido",
                    {"pedido": "42"},
                    {"status": "enviado"},
                    display_name="Buscar pedido",
                    id="call-42",
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    "form": Scenario(
        handler=_reply(
            lambda on, send: [
                send.form(
                    "Confirme seus dados",
                    _form_elements(send),
                    submit_label="Confirmar",
                    cancel_label="Agora não",
                    size="large",
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    "form_response_submit": Scenario(
        handler=_form_round_trip,
        incoming=ScenarioInput(
            flow="websocket",
            turns=_form_response_turns({"form_event_id": FORM_EVENT_REF, "cancelled": False, "values": {"nome": "Ana", "plano": "pro"}}),
        ),
    ),
    "form_response_cancel": Scenario(
        handler=_form_round_trip,
        incoming=ScenarioInput(
            flow="websocket",
            turns=_form_response_turns({"form_event_id": FORM_EVENT_REF, "cancelled": True, "values": {}}),
        ),
    ),
    "chart": Scenario(
        handler=_reply(
            lambda on, send: [
                send.chart(
                    chart_type="bar",
                    data=[
                        {"mes": "jan", "vendas": 10, "meta": 12},
                        {"mes": "fev", "vendas": 14, "meta": 12},
                    ],
                    x_key="mes",
                    y_keys=["vendas", "meta"],
                    title="Vendas",
                    message="Vendas do bimestre",
                    size="large",
                    series=[
                        send.chart_series("vendas", label="Vendas", color="#2563eb"),
                        send.chart_series("meta", label="Meta"),
                    ],
                    x_label="Mês",
                    y_label="Unidades",
                )
            ]
        ),
        incoming=ScenarioInput(),
    ),
    # AgentConfig.allowed_content_types: production answers a disallowed message with a denial run; the handler never runs.
    # The run is not observed: a run of the agent's own id reads back from Maestro as agent_id None (legacy_model.py).
    "content_policy_denied": Scenario(
        handler=_reply(lambda on, send: [send.text("O handler não deveria rodar.")]),
        incoming=ScenarioInput(config={"allowed_content_types": ("image",)}),
    ),
    "content_policy_denied_message": Scenario(
        handler=_reply(lambda on, send: [send.text("O handler não deveria rodar.")]),
        incoming=ScenarioInput(
            config={
                "allowed_content_types": "image, document",
                "content_policy_denial_message": "Só aceito {allowed}, mas chegou {offenders}.",
            },
        ),
    ),
    "content_policy_allowed": Scenario(
        handler=_reply(lambda on, send: [send.text("Mensagem de texto aceita.")]),
        incoming=ScenarioInput(config={"allowed_content_types": ("text",)}),
    ),
    "run_failed": Scenario(handler=_run_failed, incoming=ScenarioInput(observe=frozenset({"runs"}))),
    "run_other_agent": Scenario(handler=_run_other_agent, incoming=ScenarioInput(observe=frozenset({"runs"}))),
    "update_user_event": Scenario(
        handler=_reply(
            lambda on, send: [send.update_user_event(on.context.event_id, [{"type": "text", "text": "Olá (mensagem revisada)"}])]
        ),
        incoming=ScenarioInput(),
    ),
    "update_thread_title": Scenario(
        handler=_reply(lambda on, send: [send.update_thread(title="Pedido 42")]),
        incoming=ScenarioInput(thread_title="Nova conversa", observe=frozenset({"threads"})),
    ),
    "update_thread_queue": Scenario(
        handler=_reply(lambda on, send: [send.update_thread(queue="suporte")]),
        incoming=ScenarioInput(queues=QUEUES, observe=frozenset({"threads"})),
    ),
    "add_participant": Scenario(
        handler=_reply(
            lambda on, send: [
                send.add_participant(
                    "user-2",
                    user_name="Bia Lima",
                    user_email="bia@example.com",
                    identity_ids=["idp-bia"],
                )
            ]
        ),
        # Core knows the person the handler adds (its connection context describes every human of the thread).
        incoming=ScenarioInput(
            observe=frozenset({"threads"}),
            core_people=(User(user_id="user-2", user_name="Bia Lima", user_email="bia@example.com", identity_ids=["idp-bia"]),),
        ),
    ),
    **{f"chart_{chart_type}": Scenario(handler=_chart(chart_type), incoming=ScenarioInput()) for chart_type in CHART_VARIANTS},
    **{
        f"form_text_{input_type}": Scenario(handler=_single_form(_form_text_elements(input_type)), incoming=ScenarioInput())
        for input_type in FORM_TEXT_INPUT_TYPES
    },
    **{
        f"form_{builder}": Scenario(handler=_single_form(elements), incoming=ScenarioInput())
        for builder, elements in FORM_ELEMENT_BUILDERS.items()
    },
}
