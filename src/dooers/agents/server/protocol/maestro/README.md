# Contratos Maestro opt-in

Este submódulo prepara o contrato de execução. Não monta endpoint, executa handler,
cria store/journal, verifica assinatura JWT ou anuncia capabilities. Imports e
defaults legados permanecem intactos.

Instalar o extra `dooers-agents-server[maestro-contracts]` para construir
`MaestroContracts`. Imports legados e o port de journal não precisam desse extra.

```python
from dooers.agents.server.protocol.maestro import MaestroContracts, stable_identity

contracts = MaestroContracts()
# O adapter obtém o corpo por JSON, com tamanho limitado, antes dessa chamada.
envelope = contracts.validate("Delivery.DispatchEnvelope", body)
identity = stable_identity(contracts, envelope["delivery_id"], envelope["phase_id"], 0)
```

`validate` devolve cópia independente e só expõe códigos técnicos em erros;
`is_valid` preserva strings decimais e semântica do produtor Zod. `schema_names`
identifica os 142 schemas. `fixture_results()` verifica os 598 casos congelados,
sem tráfego de rede. O contrato usa extensões de JSON Schema obrigatórias
(semântica relacional, UTF-16 e URL WHATWG); um validador JSON Schema genérico não
é substituto. Os 24 casos adicionais do consumidor estão nos testes e o resultado
comum TS/Python está em `consumer-manifest.json`.

`bundle.zip` é um recurso de dados: contém os arquivos canônicos sem alteração,
com inventário e SHA validados antes de carregar o validador. Nenhuma entrada é
extraída ou adicionada ao import path. O ZIP impede bytecode de instalação de
alterar o inventário original. `source-manifest.json` e os pins no loader vêm da
baseline revisada, não são calculados a partir de um candidato recém-recebido.
Uma mudança de contrato exige export/review/pin coordenados com o produtor.

`Delivery.*Binding`, `*Transition`, `*Decision`, `*Replay` e `DurableResult` são
assertions para testes com snapshots confiáveis. Não aceitar `current`,
`authorization_current`, endpoints/configuração ou claims decodificados do
request como prova de autenticação. A assinatura, IAM atual, membership, lease,
fence e relógio do DB serão impostos pelos adapters das próximas tarefas.

Credenciais são distintas: dispatch JWT Maestro tem audience da URL completa do
runtime; token agente core tem audience `dooers-service-maestro`; capability
Maestro curta vincula execução/fase/fence/deadline/operações e acompanha o token
de instância em cada callback. Dispatch nunca é bearer de callback. Core token
sozinho não concede execução. Não reutilizar tokens humanos, legados ou OTEL.

`stable_identity` e `callback_idempotency_key` usam somente delivery/fase/ordinal
semântico; retry não acrescenta attempt, fence, runtime ou relógio. O SDK precisa
fixar ordinais antes de operações concorrentes e recuperar resultados no retry.

`journal.py` contém somente os tipos e Protocols privados da ADR-0005 Maestro
(`docs/decisoes/0005-journal-efeitos.md`). Autoridade única: journal durável privado
da instância compartilhado entre réplicas; primeira implementação PostgreSQL na
E05-09. Maestro `side_effect_receipts` é projeção sanitizada, nunca permissão de
executar. Ausência de projeção ou perda do journal não autoriza repetição.

Implementador E05-09 deve validar/derivar hash e namespace no adapter, garantir
intenção+outbox atômicos, claim/CAS/fence privado antes de provider, receipt
privado, reconciliação e unknown/manual_review sem nova chamada cega. Dataclasses
não autenticam entrada; `repr=False` não substitui ACL/criptografia. Sincronização
CAS por revision com tombstone usa port interno; transporte autenticado ainda
requer contrato revisado, sem improvisar endpoint/scopes da v1.

Argumentos/resultados/provider receipts ficam no journal. Projeção só contém
identidade, status, revision, receipt_id opaco e fingerprint HMAC persistido com
versão de chave. Rotação não muda fingerprint/effect_key de uma intenção antiga.
Planos 09 e 12 reutilizam esse journal, inclusive streaming, retries e approvals.

Perfil local: deadline 300 s incluindo conclusão, HTTP 340 s, lease 60 s,
heartbeat 15 s, tokens propostos 60 s (contrato máximo 300 s), concorrência 2 e
até cinco tentativas por fase. Parâmetros não são medições de provedor. Forms,
efeitos, callbacks, storage privado e execução integrada permanecem pendentes.

Verificação local no checkout SDK:

```sh
uv sync --frozen --python 3.13.9 --extra dev --extra maestro-contracts
.venv/bin/pytest -q tests/test_maestro_contracts.py
.venv/bin/ruff check src/dooers/agents/server/protocol/maestro tests/test_maestro_contracts.py
uv build
```

O wheel precisa ser instalado em ambiente isolado e validar o corpus sem imports
do checkout. `tests/maestro_package_smoke.py` suporta essa prova; não usa provider,
DB, navegador ou credencial. A v1 e o versionamento do pacote legado não mudam
nesta tarefa; não há publicação de registry.
