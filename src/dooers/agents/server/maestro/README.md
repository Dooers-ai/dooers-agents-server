# Admissão Maestro local (E05-06)

`MaestroConfig` e `MaestroDeliveryAdapter` são exportados de `dooers.agents.server`.
Os ports e tipos de admissão ficam em `dooers.agents.server.maestro`. Instale o extra
`maestro`; o extra `maestro-contracts` continua suficiente apenas para conformidade.
Imports/configuração legados não carregam cryptography/jsonschema/ada-url.

Este módulo admite uma entrada **já persistida**. Não contém cliente de contexto,
resolvedor core implementado, handler, heartbeat, sink ou journal. As implementações
correspondentes pertencem a E05-07/08/09. Configuração/versão/ports injetados não
anunciam suporte end-to-end: `adapter.capabilities == ()`.

## Autoridade por servidor

`AgentConfig.thread_management` permanece `agent` por padrão; `maestro=None`.
`thread_management="maestro"` exige a dataclass validada no construtor e novamente
no startup de `AgentServer`. Não há autodetecção pelo formato de thread ID, migração
implícita de histórico ou chamada remota escondida em `dispatch`/`ingest`.

O modo escolhe a autoridade de histórico **dessa instância de AgentServer**. Para
conservar entradas legadas no mesmo processo, use outra instância configurada
`agent` e mantenha seus endpoints separados. Configuração Maestro presente em
modo `agent` não muda os fluxos legados nem monta endpoint automaticamente.

No servidor Maestro, `dispatch`, `ingest`, `memory`, `repository` e `chat_upload`
retornam `ThreadManagementModeError` antes de inicializar/acessar persistência.
O guard de `HandlerPipeline.setup` é uma defesa complementar. O router WS rejeita
thread list/subscribe/unsubscribe/delete/participants.add, event create/list,
thread artifacts e feedback antes de ler/escrever histórico. Settings, analytics,
connect e ping continuam com as validações/autorização existentes. Não iniciar
cleanup de threads convidadas no modo novo.

`database_type` ainda é obrigatório: `server.database()`, `server.settings()` e
`server.analytics()` conservam a persistência privada. `repository()` também
oferecia settings; no modo novo use `server.settings()` com `settings_schema`,
ou `server.database()` para dados privados de aplicação. `upload()` é privado;
`chat_upload()` não é upload de artefato Maestro. Não há gravação de thread/evento/
run canônico local pela admissão. APIs privadas de baixo nível não são uma barreira
contra código arbitrário da própria aplicação operadora.

## Configuração e montagem

Configuração imutável exige `runtime_endpoint` completo incluindo prefixo e
`/maestro/deliveries`, `callback_base_url`, `core_base_url`, organização, workspace
(nullable, sem string vazia), instância lógica, runtime, `trusted_public_keys`
(mapping kid → PEM público) e `credential_resolver`. O resolvedor confiável recebe
`MaestroCredentialRequest`, retorna `MaestroInstanceCredential` e será consumido
pelo cliente E05-07/08; admissão não o chama nem reutiliza bearer de dispatch.

O primeiro perfil é exclusivamente local: `allow_local_http=True`, scheme HTTP,
host exato `127.0.0.1`, porta explícita; sem userinfo/query/fragment/path traversal/
encoding/alias. `core_base_url` aceita origem ou prefixo. HTTPS/produção exige
adapter de transporte/egress revisado; trocar a URL não ativa suporte inexistente.
Chaves/credenciais ficam fora de logs, Git e exemplos. Keyset é copiado/imutável,
RSA pública de 2048 a 8192 bits, até 16 kids. Revogar/trocar keyset exige reconstruir
o adapter sob supervisão; não há remote discovery ou refresh automático.

O adapter é aplicação ASGI, sem dependência de FastAPI/Uvicorn no runtime SDK:

```python
# config vem de configuração privada validada do operador.
from dooers.agents.server import MaestroDeliveryAdapter

adapter = MaestroDeliveryAdapter(config, lease_gate=current_lease_gate, executor=executor)
# FastAPI/Starlette: app.mount("/api/dev/agent", adapter)
# config.runtime_endpoint == "http://127.0.0.1:8790/api/dev/agent/maestro/deliveries"
```

Um host ASGI também pode servir `adapter` diretamente, preservando o caminho
completo. O servidor hospedeiro deve fazer bind em `127.0.0.1`, por exemplo
`uvicorn.run(adapter, host="127.0.0.1", port=8790, lifespan="off", proxy_headers=False)`.
O SDK não abre socket/porta nem controla o bind do framework. Não montar sob URL
pública ou iniciar `0.0.0.0` usando esse perfil HTTP. `Host`/forwarded headers não
selecionam audience, callback ou identidade; comparação usa config exata.

## Admissão e confirmação

1. Limitar método/path/body/headers/tempo; rejeitar Origin/Cookie/content encoding,
   headers de credencial duplicados e fila local quando concorrência estiver cheia.
2. Verificar assinatura **dos dois JWTs** antes de interpretar claims/envelope como
   confiáveis. Header fechado RS256/kid/typ; segmentos/base64url/UTF-8/JSON estritos,
   sem members duplicados. Dispatch e capability têm tipos diferentes. Não aceitar
   bearer humano, instância/core, OTEL ou token sem assinatura.
3. Usar diretamente `Delivery.DispatchBinding` do bundle E05-01. Essa autoridade
   valida TTL≤300, issuer fixo, audience completa, instante sem leeway positivo,
   binding de todos os IDs/fence/deadline e body SHA-256 canônico. A canonicalização
   é a do validator congelado, inclusive Unicode e números integrais; não há
   algoritmo paralelo no adapter. Verificar adicionalmente org/workspace da config.
4. Derivar deadline monotônico uma vez. Antes de qualquer contexto/handler,
   `MaestroLeaseGate.admit(admission)` precisa verificar token de instância próprio,
   autorização core/membership, todos os bindings, estado/cancelamento e lease/fence
   **correntes no Maestro**. Assinatura sozinha não concede essa autoridade.
5. O gate retorna `MaestroCurrentLease`, ou `MaestroConfirmedResult` ao reconciliar
   duplicata terminal/suspensa já persistida. A primeira opção exige execução/fence
   iguais, pelo menos 20 s de lease restante e lease≤deadline antes de executor.
   Capability ainda precisa estar válida após o gate. Sem tempo seguro, não iniciar.
6. `MaestroExecutor.execute(admission, lease)` executa inline, sem pipeline/setup/
   dispatch/ingest legado. Deve buscar contexto autorizado, renovar lease, observar
   cancel/fence/deadline e confirmar `complete`/`suspend` autenticado antes de retornar.
   É port confiável do operador, não callback de conteúdo arbitrário do usuário.
7. `MaestroConfirmedResult.confirmed` tem default **False**. True é permitido só
   após ACK autenticado ou reconciliação do commit Maestro. O adapter exige objeto
   confirmado, schema `Public.CallbackResponse`, delivery/execution iguais e evento
   confirmado para responded/waiting_input. Um DTO válido não prova commit; testes
   do executor futuro devem comprovar a promessa do port. Resposta/ACK ausente,
   indeterminada ou não canônica retorna erro, nunca 200 genérico.

Ausência de gate **ou** executor falha fechado com 503 após autenticação. Nenhuma
implementação fake é instalada por padrão. Ports que retornam confirmação nos
testes são identificados como fixtures, não evidência de execução real. Não existe
HTTP202, fallback legado, dedupe em RAM como autoridade ou background após resposta.
Replay concorrente requer o mesmo gate durável: o limite local não é lease global.

O adapter usa tarefas somente durante a requisição para observar desconexão;
cancela e aguarda ambas antes de responder. Cancelamento de ports/handler é
**cooperativo**. Código que bloqueie o loop ou suprima cancelamento pode impedir o
encerramento; o adapter não promete matar ferramenta externa nem desfazer efeitos.
Fence no Maestro continuará indispensável. SDK não fabrica `silent` em exceção,
timeout ou cancelamento. Os erros públicos são fechados e não serializam exceptions.

## Orçamento local

Todos os campos em segundos, exceto limites de payload em bytes:

| Campo | Default/teto local |
| --- | --- |
| network_timeout_seconds | 5 / 10 |
| request_timeout_seconds | 340 |
| execution_deadline_seconds | 300 |
| completion_budget_seconds | 30 |
| safety_margin_seconds | 10 |
| admission_timeout_seconds | 20 |
| lease_seconds / heartbeat_seconds | 60 / 15 |
| max_concurrency | 2, por adapter/processo, sem fila |
| max_dispatch_bytes / max_content_bytes / max_output_bytes | 65536 cada |

A reserva de conclusão está **dentro** do deadline da execução. Antes do executor,
saldo deve exceder conclusão+margem. Body tem timeout agregado de leitura; requisição
inclui gate+executor; gate tem timeout próprio. Conteúdo/saída/heartbeat e credencial
são parâmetros consumíveis pelos módulos seguintes, não funcionalidades que este
adapter já execute. Seu executor deverá aplicar o orçamento completo ADR0003.

## Verificação

`uv sync --locked --extra maestro --extra dev` instala testes de forma reproduzível.
`uv run --locked --extra maestro --extra dev pytest tests/test_maestro_admission.py`
usa Uvicorn **somente no extra dev**, socket loopback real e chaves sintéticas.
Sem nenhum provider/LLM/cloud. O smoke instalado em `tests/maestro_admission_package_smoke.py`
exercita o wheel fora do checkout sem pytest/Uvicorn ou dependências de teste.

O perfil criptográfico usa PKCS1v1.5/SHA256 porque o contrato fixa RS256. A operação
`RSAPublicKey.verify` e o carregamento PEM seguem a [documentação oficial cryptography](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/).
A versão executada está fixa em 48.0.0 no extra; não depende da versão latest da documentação.

## Contexto e memória autorizada (E05-07)

`MaestroContextClient(config, admission, current_lease)` é construído pelo executor
**depois** do gate autenticado. `MaestroMemory(client)` fornece `get_thread()`,
`get_history_raw()`, `get_history()` e `list_artifacts()` sem consultar ou escrever
histórico na persistência legada. Estes componentes não executam handler,
heartbeat, callback, provider ou hidratação; capabilities continuam vazias.

O cliente usa somente a URL de callback confiável do `MaestroConfig`, fixa
`127.0.0.1`, porta e prefixo, e faz `GET /deliveries/:id/context`. Envia
`Authorization: Bearer <token da instância core>` separado de
`Maestro-Execution-Capability`. O resolvedor privado recebe organização/workspace,
instância e runtime exatos, audiência `dooers-service-maestro` e apenas
`deliveries:read`/`context:read`. Nenhum token de dispatch é reutilizado.
A inspeção local de claims impede confusão de identidade; a assinatura do token
core e a autorização atual são verificadas pelo serviço remoto. O resolvedor é
port confiável do operador, não uma função alimentada por dados do handler.

Não há cache de memória/credencial, proxy de ambiente, redirect, cookie jar
persistente, retry ou discovery. Um cliente HTTP efêmero por página evita cookies
entre operações. Timeout inclui resolvedor+rede, é limitado por deadline residual,
reserva de conclusão, lease e capability. Resposta precisa ser JSON estrito e
canônico (bundle E05-01 único), sem encoding comprimido; limite de página padrão
1 MiB (máximo configurável 4 MiB). Erros retornam somente códigos fechados, sem
corpo remoto/URL/segredo. 401/403/404/409/410 encerram leituras naquela execução.
Validade da credencial é o mínimo entre `exp` assinado e metadata do resolvedor,
revalidada também depois de receber o corpo.

Paginação usa o `context_cursor` do envelope na primeira requisição, depois
`history.next_cursor`; cada resposta conserva o mesmo `context_cursor` e
`history.watermark == context_cutoff_sequence`. Páginas são cronológicas,
sequências estritamente crescentes e IDs não repetidos, inclusive entre páginas.
IDs de delivery/thread/gatilho, raiz e associação ativa da instância destinatária
são conferidos. Qualquer evento além do corte, loop de cursor, mudança do gatilho,
escopo divergente ou erro numa página descarta a leitura inteira. Comparação de
sequências decimais usa comprimento/ordem lexical, sem perder precisão em float.
O default aceita até 20 páginas e 8 MiB por leitura; limites maiores requerem
configuração explícita (máximos 100 páginas/32 MiB). Excesso falha, não trunca
histórico silenciosamente. Cada chamada futura repete autorização remota. Dados
já entregues ao handler não podem ser apagados retroativamente.

`get_history_raw(limit=50, order='asc', filters=None)` retorna eventos públicos
Maestro originais como dicionários com actor, IDs, sequência, correlação e payload,
sem conversão para `ThreadEvent` legado. `get_history()` usa por padrão as últimas
50 mensagens, devolvidas cronologicamente. Filtros locais aceitos: `type`,
`actor_kind`, `instance_agent_id`; nunca modificam o escopo/corte remoto. Limit é
1–1000. `get_thread()` retorna `MaestroThreadView` (IDs, workspace nulo,
participantes e corte), **não** `Thread` legado: o contrato ContextResponse não
fornece título, permissões ou timestamps da thread. Campos ausentes não são
inventados. Participantes representam a resposta autorizada mais recente.

Conversores `openai`, `anthropic`, `google`, `cohere`, `voyage`, `langchain`,
`openai_completions` e `openai_responses` são projeções **textuais**. Incluem prefixo
JSON com autoria canônica/ID/sequence; somente respostas da própria instância usam
assistant/model/CHATBOT. Humanos e outros agentes usam papel de entrada user/USER
com identidade explícita; esse papel do provedor não declara que o autor é humano.
Nenhum participante/system event vira instrução system. Sem fusão de autores.
Perdem-se estrutura multimodal, campos de correlação fora do prefixo e eventos
que não sejam message.created; o bruto permanece acessível. Não fazem chamada a
provedor nem comprovam sua aceitação em qualquer versão externa.

`list_artifacts()` retorna referências `MaestroArtifactReference` extraídas das
páginas autorizadas, com artifact_id/event_id/sequence/actor e metadata disponível.
Ordem mais recente primeiro; `direction='out'` significa a própria instância e
`in` todos os demais. Cursor numérico local é apenas offset sobre a mesma leitura
fixa, não credencial nem cursor de download. Cada chamada revalida o snapshot.
Não retorna URLs/bytes/status inventados, não acessa UploadStore nem segue URLs de
conteúdo. Os anexos da mensagem de entrada chegam ao handler com bytes e link
temporário (seção "Arquivos da entrada e link temporário").

### Uso pelo executor E05-08

```python
client = MaestroContextClient(config, admission, current_lease)
memory = MaestroMemory(client)
async with maestro_handler_context(memory, private_resolver) as prepared:
    # E05-08 cria send/sink, heartbeat e execução inline durável.
    # A assinatura permanece handler(on, send, memory, analytics, settings).
    # Consumir o generator nesta mesma task para usar contexto privado:
    async for output in handler(
        prepared.on, send, prepared.memory, prepared.analytics, prepared.settings
    ):
        await sink.accept(output)
```

O exemplo indica ports futuros; `send`/`sink`/handler não são implementados por
E05-07. `MaestroPrivateResolver` recebe `MaestroPrivateScope` apenas da identidade
validada destinatária (org/workspace/instance), nunca do autor ou infraestrutura.
Retorna resources vinculados exatamente a esse scope: persistence privado,
settings e analytics. O operador precisa garantir segregação real do store;
rotular um banco arbitrário com scope não o torna isolado. Runtime/replicas da
mesma instância podem compartilhar o store privado. O SDK volta a consultar o
Maestro depois do I/O de resources, antes de expor input.

`on.context.agent_id` é a instância destinatária. `on.context.maestro` contém modo,
delivery/phase/execution/attempt/fence, destinatário/runtime, gatilho original,
origem da invocação, corte, actor completo, causa/raiz/depth, prazos, resume e sinal
cooperativo de cancelamento. Orçamento causal restante é `None`, pois não está no
wire; não se inventa número.

`AgentIncoming` e `AgentContext` têm os valores de produção (`HandlerPipeline.execute`),
exceto os papéis do usuário (primeiro item):

- `user`: humano vira `User(user_id=principal_id, user_name=display_name,
  user_email=email, identity_ids=identity_ids)`. Nome ou e-mail vazio vira `None`, como
  em produção. E-mail e `identity_ids` vêm do contexto da entrega (actor; senão, o
  participante). Esses campos existem só no contexto do agente, nunca no que vai ao
  navegador. **Diferença até o plano 5:** `system_role`, `organization_role` e
  `workspace_role` ficam nos padrões do `User` (`"user"`, `"member"`, `"member"`) e
  `connection_type` fica `"dashboard"`. Em produção, eles vêm do `ConnectionContext` do
  Core; o contexto da entrega do Maestro ainda não os traz. Um handler que confere o papel
  (por exemplo `organization_role == "owner"`) vê o padrão no Maestro. Contato externo vira
  `User(user_id=external_id, user_name=display_name)`, como o contato do WhatsApp em
  produção. Outro agente (ou sistema) vira `User(user_id="")`; o autor real fica em
  `on.context.maestro.actor`.
- `workspace_id`: `""` no escopo pessoal. `channel`: canal da thread, padrão
  `"dooers-platform"`. Um contato externo nunca fica com `"dooers-platform"`: sem
  canal na thread, vale o canal do contato (`"whatsapp"` ou `"public_chat"`; o plano 6
  completa o chat público), e o RAG não age em nome dele.
- `thread_title`, `thread_created_at`, `queue` e `channel_meta`: do `thread.created`
  e dos `thread.updated` até o corte. Título vazio vira `None`. Thread sem
  `thread.created` (criada por agente, sem dono) fica com os padrões de produção.
- `queues`: catálogo de filas do workspace vindo de `MaestroPrivateResources.queues`
  (itens `WorkspaceQueueCatalog`, cópia por execução). O Core não tem rota que o agente
  possa chamar para esse catálogo; o operador o fornece. Padrão vazio.
- `message`: a regra de `_extract_message` de produção (textos e cartões de contato
  unidos por espaço; sem texto, `[image]`, `[audio]`, `[document: nome]`).
- Partes: `text` → `TextPart`; `attachment` → `ImagePart`, `AudioPart` ou
  `DocumentPart` com bytes e link temporário; `media_link` → a parte de produção do
  mesmo tipo só com `url` (`data=b""`); `contact` → `ContactPart`.

`AgentContext.maestro` é uma property, não um campo. No modo Maestro, a instância passa
para uma subclasse da execução, com os mesmos campos e o nome `AgentContext`, que guarda
os metadados na classe. Assim `vars`, `asdict`, `repr` e igualdade ficam iguais aos de
produção. `dataclasses.replace`, `copy` e `deepcopy` mantêm `context.maestro` (o mesmo
objeto, com o sinal de cancelamento vivo). `pickle` grava o `AgentContext` de produção
sem os metadados, que só existem durante a execução.

Form de produção (`send.form` sem `suspension`): a resposta é o evento `form.responded`,
que o Maestro entrega ao agente autor do form numa entrega nova. O handler recebe
`form_data=values`, `form_cancelled=cancelled` e `form_event_id` do `form.created`;
o cancelamento também invoca o handler, como em produção. `message` é `""` e `content`
é `[TextPart(text="")]`, como em produção. O SDK confere que o `form.created` citado é
deste agente; senão, `CONTEXT_INVALID`.

Resume (form por `suspension`, extra do Maestro) conserva `trigger_event_id`, muda
origem/corte para interaction.responded aceito e entrega `on.form_data`; não reabre
generator. Exige IDs de interaction, phase/delivery/resposta e action compatíveis;
cancel/reject não invocam handler. `form_cancelled=False`; `form_event_id` vem do
contexto privado da interação. A referência correta é `on.context.maestro.resume`.
Nenhuma capability de interação é anunciada por essa construção tipada.

`maestro_handler_context` faz bind e restaura RAG/WhatsApp/contexto Maestro em
finally, inclusive erro, cancelamento e falha antes do generator. RAG/Maestro usam
a task atual e não propagam automaticamente para child tasks. O token de RAG fica
em `maestro_handler_context` (`bind_runtime_context()` devolve o token e
`reset_runtime_context(token)` o restaura); `AgentIncoming` nunca guarda o token. No
modo legado, `AgentIncoming` faz o binding e descarta o token, como a produção, e
mantém os campos, `asdict`, `deepcopy`, `pickle`, `repr` e igualdade de produção.
Nenhum span/OTEL é iniciado aqui; quando
E05-08 instrumentar a execução, deve encerrar seu tracker no mesmo finally. A
integração de export OTEL/provider permanece fora desta tarefa.

Cancelamento chama `client.close()` e o sinal pode ser passado pelo executor em
`maestro_handler_context(..., cancellation=signal)`. O finally fecha o cliente e
impede leituras futuras, mesmo após retorno normal. `bind_cancellation()` permite
propagar um sinal externo; não revoga dados previamente recebidos nem efeitos.

Heartbeat E05-08 deve usar `update_lease()` somente após ACK autenticado atual e
`update_capability()` para o header `Maestro-Execution-Capability` renovado. Este
último verifica RS256/keyset confiável + schema canônico, mesmos bindings/ops/
deadline, nova jti, iat não regressivo e exp crescente/vigente. Não substitui token
por string não validada nem se prende à capability inicial. Nova lease mantém
execution/fence e prazo não regressivo dentro do deadline; fechar/cancelar cliente
não pode ser desfeito pela renovação. Nenhum heartbeat é disparado automaticamente.

Testes E05-07 usam um servidor HTTP real **de fixtures** em loopback com JWTs
sintéticos. Validam o consumo/limites do contrato, não autenticação ou persistência
reais do futuro endpoint E05-04, nem execução end-to-end/LLM/provider.

O binding WhatsApp no scope Maestro também possui guarda opt-in de task e validade,
consultada **antes** de devolver persistence/agent_id. Mesmo que uma child task
herde os ContextVars, não pode obter esses recursos fora da task autorizada ou
depois da saída. A guarda é desativada/restaurada no finally. Binding legado sem
guarda mantém assinatura, tupla de dois tokens e comportamento anterior. Essa
guarda não encerra tasks externas nem desfaz referências/efeitos já obtidos.

### Arquivos da entrada e link temporário

Cada anexo da mensagem de entrada é baixado (bytes) e recebe um link temporário do
Maestro (`url`), como a URL assinada de produção. O link vale sem login, para um só
arquivo, e serve ao handler e ao provedor de LLM. O SDK pede o link em
`POST /executions/{execution_id}/artifacts/{artifact_id}/links` (resposta
`{url, expires_at}`), com a credencial da instância e a capability da execução.
`LinkResolver` (`links.py`) guarda um link por arquivo durante a execução e pede outro
quando faltam menos de 60 s para expirar. O link é um bearer: nunca entra em log nem em
`repr`.

Requisito de configuração do Maestro: `ARTIFACT_LINKS_ENABLED=true` e
`MAESTRO_ARTIFACT_LINK_KEY` (base64, no mínimo 32 bytes). Sem isso, a capability não traz
`artifact.link.issue` ou a rota responde 404/403/`UNSUPPORTED_CAPABILITY`. Nesse caso,
o SDK levanta `MaestroArtifactLinkUnavailable` (código `UNSUPPORTED_CAPABILITY`), grava
um log `error` com o nome das duas configurações e não invoca o handler: a entrega
falha (HTTP 422). O SDK nunca entrega ao handler uma parte de mídia sem link ou com
link falso. O Maestro só emite link para arquivo ligado à thread até o corte do
contexto; os anexos da entrega se qualificam.

## Executor textual E05-08

A composição local agora implementa os ports de heartbeat, contexto, sink e
executor inline. Mantenha o `MaestroConfig` e o resolvedor privado como configuração
confiável da aplicação e monte a aplicação ASGI no caminho de `runtime_endpoint`:

```python
from dooers.agents.server.maestro import create_text_delivery_adapter

async def handler(on, send, memory, analytics, settings):
    if on.message == "ignorar":
        yield send.silence("not_relevant")
        return
    yield send.run_start()
    yield send.text("Uma resposta completa")
    yield send.run_end()

app = create_text_delivery_adapter(maestro_config, handler, private_resolver)
```

O endpoint começa validando assinatura/bindings do dispatch. O gate faz heartbeat
com token core da própria instância (`processing:write`) + capability separada e
obtém a lease atual antes de carregar contexto/handler. O prazo inicial usado pelo
cliente do gate é somente um limite de rede; não autoriza execução. Renovação do
header é opcional quando a exp atual já cobre o novo intervalo. Quando presente,
exige assinatura/keyset, bindings/ops/deadline estáveis, jti nova, iat não regressivo
e exp maior/vigente. `MaestroCurrentLease.execution_capability` é um campo interno
aditivo, oculto no repr, validado novamente pelo adapter/executor. O ACK pode
atravessar a exp antiga se tiver confirmado uma renovação válida. Não se guarda
capability em cache global nem se entrega token operacional em `on.context`.

`MaestroCallbackClient` é também o cliente usado por `MaestroMemory`: heartbeat,
leituras e saídas compartilham lease/capability correntes. Cada request resolve sua
própria credencial com scopes mínimos; saída usa `responses:write`, complete usa
`processing:write`. HTTP loopback é explícito, sem proxy ambiental, redirect,
retry do transporte, cookie herdado ou URL escolhida pelo input. Respostas e
requests são limitados, validados e conferidos contra a identidade da execução.
O contrato é compilado no startup e reutilizado nas invocações.

Cada `send.text` é uma mensagem final independente. O sink aloca `output:0`,
`output:1`, etc. por ordem semântica da fase. A chave HTTP é
`output:<delivery_id>:<phase_id>:<ordinal>`; attempt/execution/fence não entram na
chave. `MaestroOutputIntent` conserva bytes imutáveis e SHA-256 local para
identificar repetição; esse hash não é enviado como autoridade nem substitui o
hash/ledger canônico do Maestro. Um ACK de saída perdido permite **uma** repetição
limitada da mesma intenção, ID, corpo e chave. Uma segunda incerteza interrompe a
invocação para recuperação durável. Conflito de conteúdo recebe erro permanente;
não reinicia o handler nem gera UUID novo. O teto local é 998 saídas por fase (o
teto do servidor do Maestro); repetir uma intenção já criada não consome novo slot.

Só o retorno normal do handler sem mensagem produz `silent/implicit_empty`.
`send.silence()` usa `explicit_silent` por padrão ou um reason_code válido fornecido
pelo criador. Silêncio depois de mensagem ou mensagem depois de silêncio é erro;
saída parcial já confirmada é preservada, com `failed` quando a falha pode ser
confirmada. Exceção e timeout usam códigos fechados (`handler_error`,
`handler_timeout`), sem expor a exception. `run_start/run_end` enviam progresso por
heartbeat e gravam uma saída `run.update` cada um (o `run_end` sem run atual não grava
nada, como na produção). `run_end(failed)` é o status do run, não falha a fase. Uma
fase só com `run.update` termina como `responded`, porque o Maestro recusa `silent`
com saídas gravadas. Por isso, `send.silence()` depois de saídas só `run.update` (por
exemplo `run_start`, `silence`, `run_end`) é aceito e a fase termina `responded`, sem
mensagem. `run_start(agent_id=<o próprio agente>)`, como a produção faz, grava
`run.update` sem `agent_id` (o Maestro grava o agente que executa). O run atual só muda
quando o Maestro aceita o `run.update`.

Completar ocorre dentro da requisição, após fechar o generator e parar/aguardar o
heartbeat, com reserva de 30 s e margem de 10 s dentro do deadline canônico. HTTP
200 requer ACK validado de `complete`, inclusive quando o outcome é `failed`.
Nenhum ACK, timeout, revogação, HTTP técnico ou estado terminal rejeitado fabrica
200/silêncio. Complete não recebe retry cego após ACK perdido. O worker deve
consultar o resultado SQL confiável e reconciliar antes de nova tentativa. Não há
GET de resultado criado pelo SDK, tarefa posterior a HTTP ou resposta 202.

Heartbeat padrão é 15 s. Falha de renovação cancela a task do handler, propaga o
sinal cooperativo e fecha os recursos no finally. Cancelamento externo/disconnect
continua propagado mesmo concorrendo com a falha de heartbeat. Threads bloqueadas,
tasks criadas pelo código do criador e efeitos externos não cooperativos não são
interrompidos de modo forçado; o SDK fecha suas próprias tasks/clientes e o fence
remoto impede novas saídas. Não promete desfazer efeitos já iniciados. Nenhum span
OTEL/export task é iniciado por este executor; a instrumentação/exportação futura
precisa respeitar esse lifecycle e não herdar o export em background do legado.

`MaestroSend` tem a superfície de produção de `AgentSend`, com os extras do Maestro
(`silence`, `handoff`, `artifact`, `open_text`, `form(..., suspension=...)` e outros).
Cada `send.*` de produção grava uma saída (`outputs.output_for`, índice 4.4).
`send.image`/`audio`/`document` baixam a URL (`fetch_media`) e enviam o arquivo como
artefato; se o download falha, o tipo sai da política de produção ou o Maestro recusa
o arquivo, ou se o upload passa do tempo ou o Maestro está indisponível (5xx), a mensagem
leva a parte `media_link` com a URL original e um log `warning` com o motivo. O tempo do
upload cresce com o tamanho do arquivo (1 MiB/s), com `artifact_timeout_seconds` como piso.
A parte enviada usa o `mime_type` que o Maestro gravou (MIME canônico), não o declarado.
Uma URL `data:` é decodificada no local (base64 tolerante como nos navegadores, ou
percent-encoding), com a mesma política de tipo (MIME da própria URL). Uma URL `data:`
maior que `ceil(max_artifact_bytes * 4 / 3) + 4096` caracteres é recusada antes de qualquer
cópia (`too_large`), inclusive percent-encoding perto do limite.
Só `send.whatsapp.*` continua recusado (`UNSUPPORTED_CAPABILITY`, plano 5).

Como em produção, `send.update_thread` e `send.add_participant` não falham o handler. Se o
Maestro recusa a saída `thread.update` ou `participant.add` com 403, 404, 422, 409 de regra
(por exemplo `THREAD_ARCHIVED`, `THREAD_DELETING`) ou 429 `QUOTA_EXCEEDED` (limite de
membros), o sink grava um log `warning`, não consome o ordinal e o handler continua; o
cliente fica aberto. Os conflitos da execução (409 `LEASE_STALE`, `EXECUTION_TERMINAL`,
`IDEMPOTENCY_CONFLICT`), 401, 429 `RATE_LIMITED`, 5xx e falha de transporte continuam a
parar a execução. O mesmo vale para a saída `run.update` (`run_start`/`run_end`)
recusada com 403 `FORBIDDEN` `multi_agent_thread` (só esse caso). `send.add_participant` sem `user_id` (só e-mail ou
vazio) não gera saída e grava um log `warning`. `update_thread(queue="")` envia `queue: null`
(o Maestro grava `queue || null`; a produção tira a fila).

Recibo de saída: `message.create` e os eventos de conteúdo (`reasoning`, `tool_*`, `chart`,
`form`) exigem o valor pedido; diferença fecha o cliente e deixa o resultado incerto. Em
`thread.update`, `participant.add` (`kind` humano e `principal_id` igual ao `user_id`),
`run.update` e `message.update`, o Maestro pode normalizar o valor: a saída já está gravada,
o valor gravado vale e o sink grava um log `warning`.

Divergência da produção: o contrato só aceita `http`/`https` na parte `media_link`.
Com uma URL `data:` recusada (malformada, grande demais ou fora da política de tipo) ou
com outro esquema (`ftp:`, `file:` e outros), não há `media_link` possível: a execução
falha com `handler_contract_error`. Em produção, `send.image`/`audio`/`document` só
guardam a URL e nunca falham por causa dela. Recursos
privados RAG/WhatsApp/settings/analytics continuam vinculados pela camada E05-07.
`AgentSend` e o pipeline legado não foram alterados por E05-08.

### Fronteiras de retry e próxima tarefa

O adapter impede duas invocações simultâneas do mesmo execution/fence **no mesmo
processo** e aplica backpressure 2, sem fila local. Heartbeat verifica ownership
corrente no Maestro, mas não reserva uma invocação Python cross-réplica. O worker
E05-03 deve **nunca reenviar automaticamente a mesma execução viva**: reconciliar
ou invalidar ownership antes de autorizar tentativa nova. Dedupe durável de
invocação/resultado e recuperação de outputs antes de reexecutar cabem a E05-09,
usando o journal privado compartilhado existente como única autoridade.

E05-06 aceita `MaestroConfirmedResult` de um gate confiável para resultado terminal
já confirmado. O gate padrão E05-08 usa heartbeat e **não** consulta/confirma
terminais. Duplicata terminal hoje recebe rejeição, e o worker consulta o resultado
SQL. ACK de complete perdido não autoriza o SDK a inventar confirmação. Portanto,
M07-004 (retorno de resultado terminal em duplicata) e exclusão cross-réplica ainda
precisam da integração de E05-09/E05-11. Identidade estável de mensagem não resolve
reexecução de ferramenta nem garante resposta determinística de um LLM.

As capabilities anunciadas continuam vazias até prova conjunta C2. Os testes desta
tarefa usam sockets HTTP locais com fixtures de callback e chaves sintéticas;
não demonstram core/PG Maestro reais, provider, escala a zero, LLM, publicação ou
deploy. O smoke de wheel instalado comprova resposta confirmada e recusa de falso
sucesso após ACK de complete perdido, separado do checkout e sem pytest/Uvicorn.

Execução entre réplicas, retry e efeitos exigem a composição durável E05-09 em [journal.md](journal.md). A factory textual E05-08 conserva seu comportamento de ensaio; seu guard em memória não reserva invocação entre processos.

## Incremental local (E09-03)

`MaestroConfig(product_capabilities=("stream.delta",))` é uma decisão explícita
**do host confiável**, depois de verificar o ambiente/capability de produto.
O default é vazio. Não aceitar essa configuração de prompt, payload de agente,
schema ou número de versão. Descoberta/negociação real e ativação integrada
pertencem a E09-10; este SDK não anuncia nem ativa a capability no serviço.
Autorização `delivery.respond`, token da instância e lease/fence atuais continuam
obrigatórios em cada callback. `422` do serviço não vira ACK nem fallback exitoso.

```python
async def handler(on, send, memory, analytics, settings):
    output = send.open_text()
    await output.delta("Olá ")
    await output.delta("mundo")
    await output.finish()  # uma mensagem canônica, via /outputs existente
    yield send.text("Outra mensagem completa")  # comportamento anterior
```

O handle é exclusivo da task do handler e não permite task independente. Ordinal
é reservado na abertura no mesmo espaço de `send.text`; `output_id=output:N` e a
chave final `output:<delivery>:<phase>:N` não dependem de chunks, tentativa ou
fence. `finish()` repetido retorna o mesmo receipt; ACK perdido repete os mesmos
bytes/chave, sem chamar novamente geração ou ferramenta. Depois de iniciar
finish, o conteúdo fica congelado mesmo se o resultado for desconhecido.
A conclusão da fase e o journal E05-09 continuam com suas autoridades existentes.

Adota limites E09-01: 16 KiB UTF-8 pendentes por output (também satisfaz UTF-16),
256 chunks, 16 handles abertos e 256 KiB de texto integral agregado nos handles.
Final integral tem `max_output_bytes` (até 64 KiB); estes limites não expandem o
contrato durável. A produção contínua coalesce e faz flush quando transcorreram
50 ms ou a fila enche; `flush()` força a entrega respeitando o intervalo.
Todos os outputs compartilham espaçamento mínimo de 50 ms na execução, também
aplicado imediatamente antes de enviar no transporte. Não há timer de stream:
um produtor ocioso pode deixar seu último pequeno delta pendente até a próxima
chamada ou finish. Isso limita frequência, não promete latência de 50 ms.

Perda de ACK de chunk desliga somente o preview, preservando o texto integral
para finish. Não há retry automático do chunk nem replay de geração para reparar
gap/reset; o consumidor descarta parcial e aguarda final. Após 256 chunks também
cessam os previews. Rejeição de autoridade, cancelamento ou erro definitivo fecha
o handle; não gera final falso. Abandonar um handle sem finish falha a fase,
em vez de converter parcial em resposta. O finally do executor limpa buffers,
contexto e heartbeat, inclusive cancelamento externo; não há tarefa extra.

Os testes usam callbacks TCP locais sintéticos com schemas reais. Eles não provam
ingresso E09-02, fanout, broker, core, replay do cliente ou produção. Os testes de
journal continuam usando PostgreSQL privado temporário; nenhuma nova tabela de
streams, recibos ou efeitos foi criada. O wheel candidato E09-03 tem hash próprio;
versão `0.23.4` não substitui identificação exata por hash.
