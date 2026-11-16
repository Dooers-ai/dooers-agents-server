# Conectores

Guia para **criadores de agentes** que usam os conectores da organização (Google Agenda, GitHub) no handler.

A Dooers guarda as credenciais, aplica as permissões do Studio e executa as tools. O seu código recebe as tools no formato do framework e repassa a ela as chamadas do LLM. Nenhuma chave de provedor passa pelo agente.

## Pré-requisitos

- O conector está ativo na organização e liberado para o agente, com cada tool em "Sempre permitir", "Pedir aprovação" ou bloqueada.
- `DOOERS_CONNECTOR_SERVICE_URL` está definida. O Dooers Host já injeta essa variável; fora dele, quem roda o agente a define.
- A autenticação usa a chave de runtime do agente, como em `dooers.tools.rag`. Não há token para configurar.
- As chamadas acontecem **dentro do handler**. Fora de um turno, `tools()`, `call()` e `list()` levantam `ConnectorError("connectors precisa de um turno ativo do handler")`.

## Exemplo mínimo: OpenAI Chat Completions

```python
from openai import AsyncOpenAI
from dooers.tools import connectors

openai = AsyncOpenAI()


async def agent_handler(incoming, send, memory, analytics, settings):
    yield send.run_start()
    tools = await connectors.tools()
    messages = [
        {"role": "system", "content": "Você agenda consultas da clínica."},
        *await memory.get_history(limit=20, format="openai"),
    ]
    extra = {"tools": tools.openai()} if tools.names else {}

    while True:
        completion = await openai.chat.completions.create(model="gpt-4.1", messages=messages, **extra)
        message = completion.choices[0].message
        if not message.tool_calls:
            break
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            messages.append(await tools.run(call))

    yield send.text(message.content or "")
    yield send.run_end()
```

- `tools.run(call)` executa a chamada e devolve a mensagem `{"role": "tool", ...}` pronta para a próxima volta.
- `run` **nunca levanta** por erro de conector, rede ou permissão. O LLM recebe `status` e `message` e explica à pessoa.
- O chat mostra cada chamada como `tool.call` e `tool.result`.
- Se você mistura tools suas com as do conector, só passe a `run` as chamadas com `call.function.name in tools.names`. Um nome fora do conjunto volta como `denied`, sem chegar ao serviço.

## OpenAI Agents

```python
from agents import Agent, Runner

tools = await connectors.tools()
agent = Agent(name="Recepção", instructions="Você agenda consultas da clínica.", tools=tools.openai_agents())
result = await Runner.run(agent, input=incoming.message)
yield send.text(result.final_output)
```

Precisa de `pip install openai-agents`. Cada `FunctionTool` executa sozinha e devolve ao LLM o mesmo JSON que `run` devolve.

## LangChain e LangGraph

```python
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

tools = await connectors.tools()
graph = create_react_agent(ChatOpenAI(model="gpt-4.1"), tools=tools.langchain())
result = await graph.ainvoke({"messages": [("user", incoming.message)]})
yield send.text(result["messages"][-1].content)
```

Precisa de `pip install langchain-core`. Cada `StructuredTool` usa o JSON Schema do conector como `args_schema` e executa sozinha.

## Outros provedores

| Provedor | Definições | `run(call)` recebe | e devolve |
|----------|------------|--------------------|-----------|
| OpenAI Chat Completions | `tools.openai()` | um item de `message.tool_calls` | `{"role": "tool", "tool_call_id", "content"}` |
| OpenAI Responses | `tools.openai_responses()` | o item `function_call` | `{"type": "function_call_output", "call_id", "output"}` |
| Anthropic | `tools.anthropic()` | o bloco `tool_use` | `{"type": "tool_result", "tool_use_id", "content"}`, para o `content` de uma mensagem `user` |
| Gemini | `tools.gemini()`, em `types.Tool(function_declarations=...)` | o `function_call` | `{"function_response": {"name", "response"}}` |
| Qualquer um | `tools.specs` | a tupla `(nome, argumentos)` | o dicionário de resultado |

Para filtrar, use `connectors.tools(only=["agenda_recepcao"], read_only=True)`. O nome de cada tool para o LLM é `{conector}__{tool}`, por exemplo `agenda_recepcao__create_event`.

## `bind`: argumentos fixados no código

```python
tools = (await connectors.tools()).bind("agenda_recepcao__suggest_time", duration_minutes=45)
```

- Devolve um `ToolSet` novo. O argumento sai do schema que o LLM vê (inclusive de `required`) e o valor vai em toda chamada, acima do que o LLM mandar.
- Argumentos travados pelo admin no Studio vencem o `bind` e o LLM.
- Se a tool não está no conjunto (por exemplo, indisponível neste canal), `bind` não muda nada.

## `call`: sem LLM

```python
from dooers.tools.connectors import ConnectionRequired, ConnectorError

try:
    resultado = await connectors.call(
        "agenda_recepcao",
        "create_event",
        {"summary": "Consulta Ana", "start": "2026-11-17T10:00", "end": "2026-11-17T10:45"},
    )
    yield send.text(f"Agendado: {resultado['content']['id']}")
except ConnectionRequired as exc:
    yield send.text(f"Conecte a agenda para continuar: {exc.connect_url}")
except ConnectorError as exc:
    yield send.text(f"Não consegui agendar: {exc.message}")
```

- Devolve o corpo do serviço: `status`, `content`, `truncated` e `message`.
- Qualquer status diferente de `ok` levanta a exceção da tabela abaixo. Todas herdam de `ConnectorError` e trazem `status` e `message`.
- Usa o mesmo cartão e a mesma espera de `run`, mas não grava `tool.call` nem `tool.result` no chat.

## `list`: o que o agente pode usar agora

```python
for conector in await connectors.list():
    print(conector["slug"], conector["tools"], conector["unavailable"])
```

```python
[{"slug": "agenda_recepcao", "name": "Agenda Recepção", "type": "google_calendar",
  "tools": ["list_events", "suggest_time"],
  "unavailable": [{"tool": "create_event", "reason": "needs_approval_without_presence"}]}]
```

`unavailable` explica por que uma tool não veio em `tools()`:

| `reason` | Por quê |
|----------|---------|
| `needs_approval_without_presence` | A tool está em "Pedir aprovação" e não há pessoa no chat para aprovar. |
| `person_required` | O conector usa a conta de cada pessoa, e não há pessoa comprovada nem `on_behalf_of`. |
| `blocked` | Bloqueada pela organização, pelo agente ou pela pessoa. |

## Conectar e aprovar no chat

Quando falta conectar a conta ou a tool pede aprovação, o SDK põe um cartão no chat, espera a pessoa e repete a chamada sozinho. O seu código não trata a retomada.

- O prazo padrão é de 180 segundos para conectar e 120 para aprovar. `interaction_timeout` em `tools()` ou `call()` substitui os dois.
- Mantenha `interaction_timeout` em **até 240 segundos**. Durante a espera o socket do chat fica em silêncio, e o cliente derruba a conexão depois de 300 segundos sem resposta.
- Só a pessoa que está no chat aprova. A conta da empresa só é conectada por dono ou gerente da organização; para os outros, o cartão pede para chamar um admin.

## Status

| `status` | O que aconteceu | `call` levanta |
|----------|-----------------|----------------|
| `ok` | Executou. O resultado vem em `content`, com `truncated: true` se foi cortado. | nada, devolve o corpo |
| `connection_required` | Falta conectar a conta e não há pessoa no chat. `connect_url` leva à página de conexão, e o agente pode mandar o link. | `ConnectionRequired` (`exc.connect_url`) |
| `declined` | A pessoa recusou. | `ApprovalDeclined` |
| `interaction_timeout` | A pessoa não respondeu a tempo. | `InteractionTimeout` |
| `denied` | Sem permissão: organização, agente, pessoa ou tool fora do conjunto. | `PermissionDenied` |
| `invalid_arguments` | Argumentos inválidos, inclusive JSON malformado vindo do LLM. | `InvalidArguments` |
| `provider_error` | Erro do provedor, do serviço de conectores ou de rede. | `ConnectorError` |
| `error` | Chamada fora do turno ou `DOOERS_CONNECTOR_SERVICE_URL` ausente. | `ConnectorError` |

`tools()` e `list()` levantam `ConnectorError` quando o serviço não responde. Se o agente deve seguir sem conectores, trate a exceção.

## Canais sem chat web

No WhatsApp, em agendamentos e em `dispatch()`, não há ninguém no chat para aprovar ou conectar.

- As tools em "Pedir aprovação" **não aparecem**, porque não há quem aprove. Para o agente agir sozinho, o admin precisa colocar a tool em "Sempre permitir". `connectors.list()` mostra essas tools em `unavailable` com `needs_approval_without_presence`.
- Uma conta que falta conectar volta como `connection_required`, com `connect_url`.
- Para usar a conta pessoal de alguém, a pessoa autoriza o agente em "Minhas conexões", na seção "Agentes que podem usar sem eu estar no chat", e o código passa quem ela é:

```python
tools = await connectors.tools(on_behalf_of=tarefa.criada_por)
```

- O SDK **nunca** deduz a pessoa do canal ou dos metadados. Sem `on_behalf_of`, nenhuma conta pessoal é usada.
- Só passe `on_behalf_of` com a identidade da pessoa conhecida com segurança, como o `user_id` gravado quando uma pessoa logada criou a tarefa. No WhatsApp, quem decide o remetente é o agente: se ele errar, usa a conta de uma pessoa para responder a outra.
