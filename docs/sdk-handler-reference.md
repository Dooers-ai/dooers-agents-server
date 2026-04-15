# Agents server SDK — handler API reference

Python package: **`dooers`** (`pip install dooers-agents-server`).

Handlers are async generators with a fixed signature. The first argument is often named `incoming` or `on` (same object).

```python
async def agent_handler(incoming, send, memory, analytics, settings):
    yield send.text("Hello")
```

---

## Definições de objetos: atributos e métodos

### `incoming` (`AgentIncoming`)

Dataclass; **só atributos**, sem métodos próprios.

| Atributo | Tipo | Descrição |
|----------|------|-----------|
| `message` | `str` | Texto agregado de todas as partes `text` em `content` (atalho). |
| `content` | `list[ContentPart]` | Partes tipadas da mensagem recebida (ver abaixo). |
| `context` | `AgentContext` | Metadados do thread, utilizador e mensagem. |
| `form_data` | `dict \| None` | Valores submetidos num `form.response`; `None` se não for resposta a formulário. |
| `form_cancelled` | `bool` | `True` se o utilizador cancelou o formulário. |
| `form_event_id` | `str \| None` | ID do evento do formulário original a que esta resposta corresponde. |

### `incoming.context` (`AgentContext`)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `thread_id` | `str` | ID do thread atual. |
| `event_id` | `str` | ID do evento de utilizador que disparou este turno. |
| `organization_id` | `str` | ID da organização (pode ser string vazia). |
| `workspace_id` | `str` | ID do workspace (pode ser string vazia). |
| `user` | `User` | Utilizador (ver tabela seguinte). |
| `thread_title` | `str \| None` | Título do thread, se existir. |
| `thread_created_at` | `datetime \| None` | Data de criação do thread. |

### `incoming.context.user` (`User`)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `user_id` | `str` | ID do utilizador. |
| `user_name` | `str \| None` | Nome apresentável. |
| `user_email` | `str \| None` | Email. |
| `identity_ids` | `list[str]` | IDs de identidade associados. |
| `system_role` | `str` | Papel no sistema: `"admin"` ou `"user"`. |
| `organization_role` | `str` | `"owner"`, `"manager"` ou `"member"`. |
| `workspace_role` | `str` | `"manager"` ou `"member"`. |

### `incoming.content` — `ContentPart` (partes da mensagem)

Cada elemento é um dos modelos Pydantic abaixo (discriminador `type`).

**`TextPart`** (`type: "text"`)

| Campo | Tipo |
|-------|------|
| `text` | `str` |

**`AudioPart`** (`type: "audio"`)

| Campo | Tipo |
|-------|------|
| `data` | `bytes` |
| `mime_type` | `str` |
| `duration` | `float \| None` |
| `filename` | `str \| None` |

**`ImagePart`** (`type: "image"`)

| Campo | Tipo |
|-------|------|
| `data` | `bytes` |
| `mime_type` | `str` |
| `width`, `height` | `int \| None` |
| `filename` | `str \| None` |

**`DocumentPart`** (`type: "document"`)

| Campo | Tipo |
|-------|------|
| `data` | `bytes` |
| `mime_type` | `str` |
| `filename` | `str` |
| `size_bytes` | `int` |

---

### `send` (`AgentSend`)

Instância com **métodos** que devolvem `AgentEvent`. Não expõe atributos de estado úteis para o handler; use os métodos listados na secção 1.

**O que se faz com o retorno:** `yield send.<método>(...)` — cada chamada produz um `AgentEvent`:

| Campo em `AgentEvent` | Tipo | Significado |
|----------------------|------|-------------|
| `send_type` | `str` | Identificador do tipo de evento (ex.: `text`, `form`, `run_start`). |
| `data` | `dict` | Payload específico do tipo (texto, URLs, argumentos de tool, etc.). |

Os métodos estáticos `send.form_*` (ex.: `form_text`) **não** devolvem `AgentEvent`; devolvem `dict` para compor o argumento `elements` de `send.form(...)`.

---

### `memory` (`AgentMemory`)

Apenas **dois métodos públicos**; não há atributos públicos documentados (o `thread_id` é interno).

| Método | Assinatura | Retorno |
|--------|------------|---------|
| `get_history_raw` | `async def get_history_raw(self, limit: int = 50, order: "asc" \| "desc" = "asc", filters: dict[str, str] \| None = None)` | `list[ThreadEvent]` — eventos do thread na persistência. |
| `get_history` | `async def get_history(self, limit: int = 50, format: "openai" \| "anthropic" \| "google" \| "cohere" \| "voyage" = "openai", order: "asc" \| "desc" = "desc", filters: dict[str, str] \| None = None)` | `list[dict]` — mensagens já convertidas para o formato do `format` escolhido (apenas eventos `message` com conteúdo textual/medial resumido). |

**`filters`:** pares campo → valor para filtrar eventos na camada de persistência (ex.: `{"type": "message"}`, `{"actor": "user"}` conforme suportado pelo backend).

**`ThreadEvent` (objetos em `get_history_raw`)** — campos principais:

| Campo | Tipo | Notas |
|-------|------|--------|
| `id` | `str` | |
| `thread_id` | `str` | |
| `run_id` | `str \| None` | |
| `type` | ver `EventType` no código | `message`, `form`, `form.response`, etc. |
| `actor` | `str` | `user`, `assistant`, `system`, `tool` |
| `author` | `str \| None` | |
| `user` | `User` | |
| `content` | `list \| None` | Partes no formato wire S2C quando aplicável. |
| `data` | `dict \| None` | Dados extra do evento. |
| `created_at` | `datetime` | |
| `streaming`, `finalized`, `client_event_id` | opcionais | |

---

### `analytics` (`AgentAnalytics`) e `settings` (`AgentSettings`)

Também são objetos com **métodos** (async), sem atributos públicos para leitura direta.

**`analytics`**

| Método | Descrição |
|--------|-----------|
| `await analytics.track(event: str, data: dict \| None = None)` | Regista evento analítico com nome livre e dados opcionais. |
| `await analytics.like(target_type, target_id, reason=None, classification=None)` | Feedback positivo. |
| `await analytics.dislike(target_type, target_id, reason=None, classification=None)` | Feedback negativo. |

**`settings`**

| Método | Descrição |
|--------|-----------|
| `await settings.get(field_id: str)` | Valor de um campo do schema (inclui default do schema). |
| `await settings.get_all(exclude: list[str] \| None = None)` | Todos os valores (defaults + armazenados), com exclusões opcionais. |
| `await settings.set(field_id: str, value: Any)` | Atualiza e difunde conforme regras de visibilidade. |

Detalhes de analytics e schema de settings nas secções 4 e 5 abaixo.

---

## 1. `send` — events pushed to the frontend

`send` is an `AgentSend` instance. **Yield** (do not `await`) each event so the server can persist and broadcast it.

| Method | `send_type` (wire) | Purpose |
|--------|-------------------|---------|
| `send.text(text, author=None)` | `text` | Assistant message text; optional `author` overrides the default assistant name |
| `send.image(url, mime_type=None, alt=None, author=None)` | `image` | Image attachment |
| `send.document(url, filename, mime_type, author=None)` | `document` | File attachment |
| `send.audio(url, mime_type, duration=None, author=None)` | `audio` | Audio attachment |
| `send.tool_call(name, args, display_name=None, id=None)` | `tool_call` | Tool invocation (UI + history) |
| `send.tool_result(name, result, args=None, display_name=None, id=None)` | `tool_result` | Tool output (pair with `tool_call` via matching `id` when used) |
| `send.tool_transaction(name, args, result, display_name=None, id=None)` | `tool_transaction` | Combined tool call + result in one event |
| `send.run_start(agent_id=None)` | `run_start` | Marks run start; creates a run record and links the user message |
| `send.run_end(status="succeeded", error=None)` | `run_end` | Ends run (`status`: `succeeded` \| `failed`) |
| `send.update_user_event(event_id, content)` | `event_update` | Updates an existing user event’s content (list of content-part dicts) |
| `send.update_thread(title=None)` | `thread_update` | Updates thread metadata (e.g. title) |

### Forms

| Method | Notes |
|--------|--------|
| `send.form(message, elements, submit_label="Send", cancel_label="Cancel", size="medium")` | Renders a form; `elements` are dicts from the helpers below |
| `send.form_text(...)`, `send.form_textarea(...)`, `send.form_select(...)`, `send.form_radio(...)`, `send.form_checkbox(...)`, `send.form_file(...)` | **Static helpers** — they return element dicts, not `AgentEvent`s |

`size` is one of `small`, `medium`, `large`.

---

## 2. `incoming` — what the user sent

Campos completos, tipos de `ContentPart`, `AgentContext` e `User`: ver **[Definições de objetos](#definições-de-objetos-atributos-e-métodos)** acima.

---

## 3. `memory` — conversation history (persistence-backed)

Assinaturas completas e `ThreadEvent`: ver a subsecção **`memory` (`AgentMemory`)** em [Definições de objetos](#definições-de-objetos-atributos-e-métodos) acima.

`AgentMemory` uses the same database as the chat UI.

**Outside a handler** (e.g. cron, webhook without streaming):

```python
memory = await agent_server.memory(thread_id)
messages = await memory.get_history(limit=20, format="openai")
```

---

## 4. `analytics` — custom events, feedback, persistence

Requires analytics to be enabled in server config. Events are batched, can be **persisted** (DB) and optionally delivered via **webhook**; subscribers on the client can receive `analytics.event` frames.

| Method | Description |
|--------|-------------|
| `await analytics.track(event, data=None)` | Custom event name + optional dict payload |
| `await analytics.like(target_type, target_id, reason=None, classification=None)` | Positive feedback (`target_type`: e.g. `"event"`, `"run"`, `"thread"`) |
| `await analytics.dislike(...)` | Negative feedback |

Built-in automatic event names (for orientation) include: `thread.created`, `message.c2s`, `message.s2c`, `tool.called`, `error.occurred`, `feedback.like`, `feedback.dislike`.

**Outside a handler:**

```python
analytics = await agent_server.analytics(agent_id, thread_id="t1")
await analytics.track("custom.event", {"key": "value"})
```

---

## 5. `settings` — agent configuration (persistence + UI)

Backed by `settings_schema` on `AgentConfig`. Values are stored **per `agent_id`** (mesma camada de persistência que threads/eventos).

| Method | Description |
|--------|-------------|
| `await settings.get(field_id)` | Single field (default from schema if unset) |
| `await settings.get_all(exclude=None)` | Merged defaults + stored values |
| `await settings.set(field_id, value)` | Updates storage and broadcasts to subscribed clients (respecting visibility rules) |

### 5.1 Configurar o schema no servidor (formulário = reflexo do schema)

1. **Definir um único `SettingsSchema`** com os campos que queres no painel (labels, tipos, defaults, grupos).
2. **Passar o schema em `AgentConfig`** ao criar o `AgentServer`.
3. No **handler**, usar `await settings.get(...)` / `get_all()` / `set()` — os mesmos valores que o frontend edita (exceto campos `INTERNAL`, só no servidor).

```python
from dooers import (
    AgentConfig,
    AgentServer,
    SettingsSchema,
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsFieldVisibility,
    SettingsSelectOption,
)

settings_schema = SettingsSchema(
    version="1.0",  # opcional
    fields=[
        SettingsField(
            id="model",
            type=SettingsFieldType.SELECT,
            label="Modelo",
            value="gpt-4o-mini",
            options=[
                SettingsSelectOption(value="gpt-4o-mini", label="GPT-4o Mini"),
                SettingsSelectOption(value="gpt-4o", label="GPT-4o"),
            ],
        ),
        SettingsFieldGroup(
            id="advanced",
            label="Avançado",
            collapsible="closed",  # "open" | "closed" | omitir = não colapsável
            fields=[
                SettingsField(
                    id="temperature",
                    type=SettingsFieldType.NUMBER,
                    label="Temperature",
                    value=0.7,
                    min=0.0,
                    max=2.0,
                ),
            ],
        ),
        SettingsField(
            id="api_key",
            type=SettingsFieldType.PASSWORD,
            label="API Key",
            visibility=SettingsFieldVisibility.INTERNAL,  # só no handler, não vai no snapshot WS
        ),
    ],
)

agent_server = AgentServer(
    AgentConfig(
        database_type="postgres",
        database_name="mydb",
        settings_schema=settings_schema,
        # ...
    )
)
```

**Regras importantes**

- Cada **`id`** de campo deve ser **único** em todo o schema (incluindo campos dentro de grupos).
- **`value`** no `SettingsField` é o **default** quando ainda não há nada persistido; valores gravados substituem o default.
- Referência completa de parâmetros, grupos e fluxo WebSocket: **[agent-settings.md](./agent-settings.md)**.

### 5.2 Tipos de campo (`SettingsFieldType`) → controlos no front

No TypeScript, **`item.type`** é a string do wire (ex.: `"text"`, `"select"`). Em Python usa-se o enum (`SettingsFieldType.TEXT`, …).

| Wire (`item.type`) | Uso típico | Parâmetros extra no schema |
|--------------------|------------|----------------------------|
| `text` | Input uma linha | `placeholder` |
| `number` | Número | `min`, `max` |
| `select` | Dropdown | **`options`**: `SettingsSelectOption(value=..., label=...)` |
| `checkbox` | Boolean | — |
| `textarea` | Texto longo | `placeholder`, `rows` |
| `password` | Segredo (mascarado) | `placeholder` |
| `email` | Email | `placeholder` |
| `date` | Data | — |
| `image` | Imagem (muitas vezes só leitura) | `src`, `width`, `height` |
| `file` | Um ficheiro | `upload_url`, `accept` (ver `useSettingsFileUpload` no cliente) |
| `file_multi` | Vários ficheiros | `upload_url`, `accept` |

Campos podem ter **`user_editable`** / **`readonly`** no modelo — respeita-os ao desativar controlos e ao chamar `patchField`.

### 5.3 Visibilidade (`SettingsFieldVisibility`)

| Valor | Quem vê / edita |
|-------|------------------|
| **`USER`** | Configuração de **runtime** (default). Incluído no snapshot quando o cliente subscreve com `audience: "user"`. |
| **`CREATOR`** | **Studio / builder**: snapshot com `audience: "creator"` (regras de permissão no servidor; opcional `agent_owner_user_id`). Com esta audiência, o servidor inclui campos `CREATOR` **e** `USER`. |
| **`INTERNAL`** | **Só no handler** (`settings.get` / `get_all` / `set`). Não entra no snapshot WebSocket; patches do cliente são rejeitados. |

### 5.4 Montar o formulário no frontend (`dooers-agents-client`)

O cliente **não** traz um único componente de “formulário de settings”; recebes **`fields`** (lista de campos e grupos) e **renderizas** conforme o `type`. O **`agent_id`** já está no `AgentClient` / provider — o SDK envia `settings.subscribe` com esse id.

**Fluxo**

1. Garantir que o utilizador está ligado ao WebSocket com o mesmo **`agent_id`** que no servidor.
2. Chamar **`subscribe({ audience: 'user' })`** (ou `'creator'` para campos de studio) ao montar o ecrã; no unmount, **`unsubscribe()`**.
3. O servidor responde com **`settings.snapshot`** (schema + valores atuais). O store expõe **`fields`** e **`updatedAt`**.
4. Para gravar: **`patchField(fieldId, value)`** — envia `settings.patch`; o servidor valida, persiste e difunde `settings.patch` aos subscritores.

**Hook React (exemplo)**

```tsx
import { useEffect } from 'react'
import { useSettings } from 'dooers-agents-client'
import type { SettingsField, SettingsFieldGroup, SettingsItem } from 'dooers-agents-client'
import { isSettingsFieldGroup } from 'dooers-agents-client'

function SettingsForm() {
  const { fields, updatedAt, isLoading, subscribe, unsubscribe, patchField } = useSettings()

  useEffect(() => {
    subscribe({ audience: 'user' })
    return () => unsubscribe()
  }, [subscribe, unsubscribe])

  // Para Studio / campos CREATOR:
  // subscribe({ audience: 'creator', agentOwnerUserId: ownerUserIdFromPlatform })

  return (
    <div>
      {isLoading && <p>A carregar…</p>}
      {fields.map((item: SettingsItem) =>
        isSettingsFieldGroup(item) ? (
          <fieldset key={item.id}>
            <legend>{item.label}</legend>
            {item.fields.map((f) => (
              <YourSettingsControl key={f.id} field={f} onChange={patchField} />
            ))}
          </fieldset>
        ) : (
          <YourSettingsControl key={item.id} field={item} onChange={patchField} />
        )
      )}
    </div>
  )
}

// Implementa `YourSettingsControl`: switch (field.type) e onChange(field.id, novoValor).
```

**Renderização por item**

- Se **`isSettingsFieldGroup(item)`** é verdadeiro: renderizar um bloco (título `item.label`, opcionalmente colapsável com `item.collapsible`) e iterar **`item.fields`**.
- Caso contrário, **`item` é `SettingsField`**: fazer **`switch (item.type)`** e mapear para controlos nativos:
  - `text` → input; `textarea` → `<textarea rows={item.rows} />`; `number` → input numérico com `min`/`max`;
  - `select` → `<select>` com `item.options`;
  - `checkbox` → checkbox; `password` → input com máscara; `email` / `date` → inputs adequados;
  - `image` → `<img src={item.src ?? ''} ... />` (geralmente só leitura).
  - `file` / `file_multi` → usar **`useSettingsFileUpload`** com `upload_url` do campo, depois `patchField` com o valor acordado (metadata / URL).
- Usar **`item.label`**, **`item.placeholder`**, **`item.readonly`** para desativar edição quando necessário.

**Protocolo (resumo)**

| Direção | Frame | Payload relevante |
|---------|--------|---------------------|
| C2S | `settings.subscribe` | `agent_id`, `audience` (`user` \| `creator`), `agent_owner_user_id` opcional |
| S2C | `settings.snapshot` | Schema filtrado + valores |
| C2S | `settings.patch` | `field_id`, `value` |

Sem `settings_schema` no `AgentConfig`, subscrições/patches podem responder **`NOT_CONFIGURED`**.

Documentação alargada (incl. `WorkerClient` naming, seed, persistência): **[agent-settings.md](./agent-settings.md)**.

---

## 6. `AgentServer` — other runtime features

| API | Use |
|-----|-----|
| `await agent_server.dispatch(...)` | Run the same handler outside WebSocket (REST, webhooks); returns a stream of `AgentEvent` |
| `await agent_server.repository()` | **Direct persistence**: threads, events, runs, settings CRUD |
| `await agent_server.upload(data, filename, mime_type)` | Stage a binary for a later `event.create` (returns reference id) |
| `await agent_server.memory(thread_id)` | Memory without going through `dispatch` |
| `await agent_server.settings(agent_id)` | Settings without a full handler run |
| `await agent_server.analytics(agent_id, thread_id=...)` | Analytics outside handlers |

Database backends (PostgreSQL, SQLite, Cosmos) hold threads, events, runs, settings, and analytics tables when those features are enabled.

---

## Quick map

| Parameter | Role |
|-----------|------|
| `incoming` | User message, context, optional form response |
| `send` | Yield UI/history events (`send.*`) |
| `memory` | Thread history for LLM or inspection |
| `analytics` | Track + feedback + server-side analytics pipeline |
| `settings` | Agent config get/set with schema |

For WebSocket client behavior and frames, see the **dooers-agent-client** README and protocol docs in that package.
