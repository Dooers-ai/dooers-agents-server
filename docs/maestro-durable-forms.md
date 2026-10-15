# Formulários Maestro com continuidade durável — candidato local E11-05

A ativação é explícita. `AgentSend.form` e o callback legado permanecem intactos; este adapter continua sem anunciar capabilities de produto. O operador configura o mesmo `PostgresEffectJournal` já usado para efeitos/uso e um `MaestroCheckpointStore`, com backend/generation e schemas privados registrados igualmente no Maestro.

```python
checkpoints = MaestroCheckpointStore(
    journal,
    backend_id="local_journal",
    schemas={"demo.step": (1, validate_step)},
)
adapter = create_durable_text_delivery_adapter(
    config, handler, private_resolver,
    journal=journal, checkpoints=checkpoints, retry_policy="journal_only",
)
```

A extensão SQL imutável E11-02 é provisionada OFFLINE por migrator, no banco privado do journal existente. O executor recebe conexão DML, nunca migrator/admin. Não há DDL ou RAM fallback no startup. A geração e o estado de integridade devem ser conferidos fora do backup antes de liberar admissão. O reader Maestro usa SELECT restrito.

```python
async def handler(on, send, memory, analytics, settings):
    if on.context.maestro.resume:
        state = on.context.maestro.checkpoint
        assert state["step"] == "await_name"
        name = on.form_data["name"]
        # on.form_event_id: evento canônico original, nunca interaction_id.
        # on.context.maestro.origin_event_id: resposta humana aceita.
        yield send.text(f"Recebido: {name}")
        return

    yield send.form(
        "Nome para continuar",
        [{"name": "name", "label": "Nome", "type": "text", "required": True,
          "share_response": False, "max_length": 80}],
        suspension=MaestroFormSuspension(
            MaestroCheckpoint("demo.step", 1, {"step": "await_name"}),
            expires_at, tuple(eligible_human_participant_ids),
        ),
    )
    # Não continuará aqui: o generator é fechado após o ACK de suspend.
```

`elements` desta superfície opt-in contém fields canônicos v1; não converte controles de callback legado. Labels/size customizados legados são recusados. Faltando store/schema/capability, a criação é recusada antes de publicar uma interface. Checkpoint é JSON fechado, validado sem transformação, limitado a16KiB; não aceita generator, stack, socket, objeto arbitrário ou número float. Sua canonicalização corresponde ao `canonicalJournal` Maestro (UTF-16 e índices de propriedades JS). O journal de efeitos conserva seu formato anterior.

O checkpoint referencia delivery+phase+ordinal e a execution de origem. Replay no mesmo attempt conserva referência/bytes; outra execution só pode escrever nova origem sob a autoridade/fence vigente. `output_id=output:<ordinal>` e effect keys continuam nos namespaces já existentes, sem segunda máquina de retry. Uma falha/ACK desconhecido encerra a invocação; não autoriza ferramentas, próximo comando ou complete cego. O heartbeat é encerrado antes de suspend; generator/finally perde autoridade SDK antes de fechar. Código externo arbitrário do criador continua cooperativo: não inicie ferramentas fora dos ports mediados.

Uma nova invocação em outro processo lê `GET /deliveries/:id/interaction-context`, candidato privado, usando bearer de instância e capability da execution atual. O serviço reautoriza `delivery.context` no Core, consulta binding/fase/cutoff/runtime/fence/ACL/lease e devolve DTO privado versionado. Não altera `Public.ContextResponse` ou `DispatchEnvelope.resume`; não aceita admission enviado pelo caller, humano, CORS, cookie, query ou URL de credencial.

O SDK lê o checkpoint no MESMO backend/generation e verifica schema/version/hash/origem/retention/integrity, depois volta a consultar o contexto privado antes de fornecer handler. Gates de lease/capability/credential são repetidos depois de validar/copiar o DTO. `on.form_data` contém a resposta privada completa; `shared_data` público continua somente a parte explicitamente compartilhada. Não registre `on`, checkpoint, response_data ou credenciais. IDs de arquivos são vínculos privados, nunca URLs ou autorização de download genérico; a API de field binding/transporte de arquivos pertence à E11-06.

Reentrada sempre cria outro handler; não restaura generator nem repete automaticamente ferramentas anteriores. O handler deve ramificar usando resume/checkpoint. `on.form_cancelled=False`; cancelar/expirar não agenda callback Maestro legado. Sem checkpoints configurados, o adapter durável recusa resume.

Recovery mantém a política existente: com output canônico já persistido, crash exige `manual_review` e conserva evento/recibo, sem retry automático. Antes do output, recovery assinado + Core atual pode autorizar retry da mesma fase, com backoff existente, nova execution/fence e reutilização dos efeitos/recibos confirmados. Retransmissão não cria nova operação financeira. `usage.py`, fatos e relay não foram alterados.

Ensaios locais separados comprovam a jornada forms e regressão financeira E10-08 com o wheel instalado; isso não é aprovação de política comercial, provider pago, publicação em registry, nuvem ou produção. Forms seguem off/sem anúncio até E11-09 e gates13/14.
