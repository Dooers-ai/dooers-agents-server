# WhatsApp

Guia para **criadores de agentes** que integram WhatsApp na plataforma Dooers.

A autenticação entre seu agente e os serviços WhatsApp da Dooers é **gerenciada automaticamente** quando você conecta uma instância no Studio. Não é necessário (nem recomendado) configurar chaves, tokens ou segredos manualmente no código do handler.

## Visão geral

| Caminho | Quando usar | Aparece no chat? | Envia ao celular? |
|---------|-------------|------------------|-------------------|
| **`yield send.whatsapp.*`** | Resposta dentro de uma conversa (usuário escreveu no WhatsApp) | Sim | Sim |
| **`WhatsAppClient`** | Operações programáticas (campanhas, tools, fluxos no handler) | Não automaticamente | Sim |

## Pré-requisitos (Studio)

1. No template do agente, habilitar **WhatsApp** e configurar a **Inbound HTTP URL** do seu agente (URL pública HTTPS que receberá mensagens encaminhadas pela plataforma). O caminho exato depende de como você expõe rotas HTTP — consulte a documentação do seu deploy ou o exemplo abaixo.
2. Em **Public Chats**, conectar uma instância WhatsApp (OAuth Meta ou fluxo manual) e informar o **PIN de 6 dígitos** exigido pela Meta.
3. Aguardar status **`ready`** antes de enviar mensagens.
4. Anotar o **instance id** (UUID) exibido no Studio — use em settings do agente ou no handler.

## Habilitar envio no agent server

```python
from dooers.agents.server import AgentConfig, AgentServer

agent_server = AgentServer(AgentConfig(
    database_type="postgres",
    database_name="my_agent",
    dooers_whatsapp_service=True,
))
```

Em deploys gerenciados pela Dooers, essa opção costuma já estar ativa. Em self-hosting, siga as instruções de deploy do seu ambiente para apontar o agente aos serviços WhatsApp da plataforma.

## Inbound (usuário → agente → console)

Quando um usuário envia mensagem no WhatsApp, a plataforma encaminha o evento para a **Inbound HTTP URL** configurada no Studio. Seu agente deve expor um endpoint que **valida a requisição com o SDK** (`verify_dooers_whatsapp_tool_inbound_with_persistence`) e despacha o handler.

**Nunca** desative ou contorne a verificação de assinatura em produção.

Implementação de referência: [`examples/fastapi_whatsapp_webhook.py`](../examples/fastapi_whatsapp_webhook.py).

**Importante:** cada conversa WhatsApp abre ou continua uma **thread própria** no console — não confunda com threads de teste no Studio.

## Respostas reativas — `yield send.whatsapp.*`

Quando a mensagem veio do WhatsApp (`incoming.context.channel == "whatsapp"`), o roteamento de destino costuma ser inferido automaticamente — você pode responder sem repetir telefone ou instance id:

```python
async def handler(incoming, send, memory, analytics, settings):
    yield send.run_start(agent_id="my-agent")
    if incoming.context.channel == "whatsapp":
        yield send.whatsapp.text(f"Olá! Recebi: {incoming.message}")
    else:
        yield send.text(incoming.message)
    yield send.run_end()
```

| Método | Descrição |
|--------|-----------|
| `send.whatsapp.text(...)` | Texto livre (janela 24h da Meta) |
| `send.whatsapp.template(...)` | Template aprovado pela Meta |
| `send.whatsapp.image(...)`, `.document(...)`, `.audio(...)`, `.contact(...)` | Mídia e contato |

Detalhes: [referência do handler](sdk-handler-reference.md#whatsapp-delivery-sendwhatsapp).

### Janela de 24 horas (Meta)

- Texto livre só dentro de **24h** após a última mensagem do usuário.
- Fora da janela: erro **131047** — use template aprovado.
- Template com idioma incorreto: erro **132001** — confira o idioma exato em `templates.list()`.

## Uso programático — `WhatsAppClient`

Use **dentro do handler** (ou de um `dispatch` ativo):

```python
from dooers.tools.whatsapp import WhatsAppClient

async def handler(incoming, send, memory, analytics, settings):
    instance_id = await settings.get("whatsapp_instance_id")
    wa = WhatsAppClient(instance_id)

    templates = await wa.templates.list(status="APPROVED")
    result = await wa.templates.send(
        to_e164="+5511999999999",
        name="hello",
        language="en",
    )

    yield send.text(f"Envio: {result}")
```

### Settings schema recomendado

```python
SettingsField(
    id="whatsapp_instance_id",
    type=SettingsFieldType.TEXT,
    label="WhatsApp instance ID",
    description="UUID da instância conectada em Public Chats.",
    visibility=SettingsFieldVisibility.CREATOR,
)
```

### Expor como tool de IA

```python
async def tool_send_template(to_e164: str, template_name: str, language: str):
    wa = WhatsAppClient(await settings.get("whatsapp_instance_id"))
    return await wa.templates.send(
        to_e164=to_e164,
        name=template_name,
        language=language,
    )
```

`WhatsAppClient` envia mensagens mas **não** grava na thread do chat. Para histórico visível no console, prefira `yield send.whatsapp.*`.

## Resposta de envio

Chamadas de envio retornam se a Meta **aceitou** o pedido (`accepted`), não se a mensagem foi **entregue** no aparelho (`delivered`). Trate falhas usando `ok`, `error_code` e `error_message` quando presentes.

## Troubleshooting

| Sintoma | O que verificar |
|---------|-----------------|
| Envio ignorado / sem efeito | Instância conectada no Studio? Status **`ready`**? |
| 401 no inbound | Reconecte a instância no Studio e use **Verify agent** |
| 404 no inbound | Inbound URL do template alinhada com as rotas HTTP do seu deploy |
| Template 132001 | Nome e idioma do template — use `templates.list()` |
| Texto 131047 | Fora da janela 24h — template ou aguarde reply do usuário |
| Reply no WhatsApp não aparece no console | Instância **`ready`**? Inbound URL acessível publicamente? Verifique logs do agente no horário do reply |

## Boas práticas de segurança

- Não commite secrets, PINs ou tokens Meta no repositório do agente.
- Exponha o endpoint inbound apenas via **HTTPS**.
- Valide sempre inbound com `verify_dooers_whatsapp_tool_inbound_with_persistence`.
- Limite tools de IA que enviam WhatsApp a números e templates autorizados pelo criador.

## Referências

- [`examples/fastapi_whatsapp_webhook.py`](../examples/fastapi_whatsapp_webhook.py)
- [`sdk-handler-reference.md`](sdk-handler-reference.md)
