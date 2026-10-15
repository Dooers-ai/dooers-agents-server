# Cancelamento cooperativo do executor Maestro

E09-06 reutiliza a implementação aceita de E05-08/E09-03: uma perda de autoridade no heartbeat marca `on.context.maestro.cancellation`, cancela a task do handler e interrompe I/O awaitable cooperativo. O finally fecha sink/contexto/cliente e heartbeat. Não foi criado executor, journal ou sinal concorrente.

`tests/test_maestro_cancellation_controls.py` acrescenta provas por HTTP loopback real: recusa 403/409 fecha a conexão de geração em andamento; saída persistida e efeito sandbox concluído permanecem; A cancelado não interrompe B; fechamento do provider é falha, não pedido de cancelamento. Testes existentes cobrem deadline e cancelamento externo do ASGI.

Tasks destacadas pelo handler e ferramentas não cooperativas continuam responsabilidade do integrador. O fence do Maestro nega saídas tardias, mas não desfaz chamadas já efetivadas. O sinal local ao terminar também fecha o escopo; não constitui por si só um resultado canônico `canceled`. O pacote wheel executável permanece idêntico ao E09-03 porque esta tarefa acrescenta somente testes/documentação ao SDK servidor.
