# SDK: conexões remotas no sandbox

Perfil opt-in para `sandbox-499421`, região `southamerica-east1`. A integração local e o modo legado continuam disponíveis. Não há deploy automático.

## Configuração

Importar de `dooers.agents.server.maestro`:

- `MaestroRemoteTransport`
- `cloud_run_transport`
- `remote_core_credentials`
- `MaestroConfig` e `MaestroCredentialRequest`

Criar o transporte com `cloud_run_transport(origins, service_name="maestro-agent-<nome>")`. `origins` é uma tupla de origens HTTPS exatas: Core, Maestro e o próprio runtime. Somente os endereços canônicos `<serviço>-68544666733.southamerica-east1.run.app` são aceitos.

O processo deve executar no serviço informado, com `GOOGLE_CLOUD_PROJECT=sandbox-499421` e conta de serviço `<service_name>@sandbox-499421.iam.gserviceaccount.com`. O metadata server deve confirmar projeto e identidade. Credenciais pessoais, arquivos ADC e emulador são recusados. Não há fallback de conta ou de projeto.

Usar `remote_core_credentials(core_origin, transport, identity, credential_provider)` como resolvedor de `MaestroConfig`. `identity` fixa organização, workspace, instância, runtime e os escopos máximos. `credential_provider` devolve a credencial técnica privada dessa instância. O resolvedor troca a credencial no Core a cada operação; não reutiliza o token de dispatch. O Core permanece a autoridade de permissão.

Em `MaestroConfig`, fornecer as URLs HTTPS, `remote_transport=transport` e `allow_local_http=False`. Não combinar os perfis local e remoto. As chaves públicas de dispatch, limites, fences, leases e capacidades continuam obrigatórios.

## Transporte

- Certificado TLS e hostname são verificados.
- IAM usa `X-Serverless-Authorization`; identidade de aplicação continua em `Authorization`.
- O token IAM usa a origem exata do destino como audience e possui validade verificada.
- Sem redirects, proxy de ambiente, retry implícito ou ampliação de escopo.
- Prazo de metadata e limites de leitura permanecem ativos dentro do prazo da operação.
- Contexto, contexto de interação, callbacks, resultado terminal e cliente de arquivos utilizam o transporte configurado.

O SDK pode enviar IAM para Core/API públicos ou privados. Esse header não substitui autorização da aplicação. Para serviços privados, configurar `run.invoker` no deploy.

## Journal e readiness

`JournalServiceKey` e `JournalRecoveryAdapter` aceitam `remote_transport`. O journal mantém sua assinatura HMAC separada, vinculada a audience, direção, operação e instância. Uma resposta HTTPS sem assinatura válida é recusada. Os adapters de readiness usam o perfil do adapter de execução.

Isso não habilita automaticamente ferramentas, formulários, cobrança ou armazenamento remoto no Maestro. Os clientes financeiros sintéticos de `usage-sandbox` permanecem locais. Eles não são um adaptador de cobrança remota.

## Testes

`tests/test_maestro_remote.py` usa sockets TCP/TLS reais em loopback, com hostnames do sandbox, SNI e verificação de certificado. Só o destino do socket e a CA de teste são substituídos. As respostas de Core/Maestro e os tokens IAM são fixtures; não são serviços implantados.

Casos: troca de credenciais, histórico/contexto, heartbeat, resposta, conclusão, journal assinado, certificado não confiável, hostname incorreto, redirect, mudança de instância/escopo, audience/validade do token e identidade do metadata server. O journal também recusa uma resposta com assinatura alterada.

Build privado: `.cache/sandbox-remote/dooers_agents_server-0.24.3-py3-none-any.whl`. Não foi publicado no registry nem instalado no runtime local ativo. Usar o digest verificado do wheel no manifesto do deploy; a versão por si só não distingue esse candidato do pacote anterior.

O serviço consumidor ainda deve construir `MaestroConfig` com esse perfil, configurar seus secrets e banco, e usar esse wheel. O bootstrap remoto do Maestro e a infraestrutura não fazem parte desta prova de conexão.

## Registro desta validação

- SDK: 268 testes de regressão passaram; 16 testes exigem PostgreSQL dedicado e foram ignorados.
- Readiness/streaming: 75 passaram; 1 teste dependente da fixture de banco foi ignorado.
- Perfil remoto: 22 testes passaram, incluindo conexões TLS reais controladas.
- Wheel instalado: 2 testes TLS passaram em processo novo, com dependências reutilizadas do ambiente de teste; o código do SDK foi carregado do wheel instalado. O modo legado continua como padrão.
- SHA-256 do wheel: `0fa3c2ee251a08cfe52ddd1610c23855ad32198b7662120bd7ddc30fe4b18a06`.
