# ADR 0003 — Caso humano interno com retomada na conversa

**Status:** aceita (T05, gatilho explícito na T07) · **Responde a:** feedback gap 3 ("não existe HITL real")

## Contexto
A v1 só emitia `handoff_recommended`: nenhum caso era aberto, nenhum operador decidia, nada voltava ao cliente.

## Decisão
- O turno que escala abre um caso (`handoffs`) **na mesma transação** que grava a resposta. Gatilhos: diagnóstico com problema e nenhuma orientação oficial (mesmo após reparo), ou pedido explícito do cliente (fluxo `handoff` do Router). Exige o escopo `handoffs:create`; sem ele, só recomenda.
- Snapshot para o operador sem valores nem ids de recebíveis.
- Operador do mesmo tenant: fila, `claim` e `resolve` com versão esperada e prazo (24 h). Conflito → 409, vencido → 409 `handoff_expired`, outro tenant → 404.
- Decisão + evento de retomada numa transação; um worker do próprio processo consome o evento com lease, revalida o acesso do cliente e grava a resposta na conversa uma única vez. Acesso revogado → caso cancelado.
- A resposta humana aparece como tal e não altera dados da conta. Auditoria de criação, assunção, decisão e retomada.

## Alternativas descartadas
- Integrar uma plataforma de atendimento externa: sem acesso a uma no escopo; o contrato (abrir, decidir, retomar) é o mesmo e fica atrás das rotas de operador.
- Worker separado com fila (outbox + broker): necessário com várias instâncias; aqui o lease no SQLite dá a mesma garantia de entrega única.

## Consequências
- Não há tela de operador: o ciclo é exercido pela API e por `scripts/demo.py`.
- O worker roda no processo da API; parar a API pausa as retomadas (o evento continua pendente e é entregue na volta).

**Evidência:** `tests/test_handoffs.py` (14 testes, incluindo reinício entre decisão e retomada e queda do worker); `tests/test_replanning.py` (pedido explícito); `scripts/demo.py` seção 6.
