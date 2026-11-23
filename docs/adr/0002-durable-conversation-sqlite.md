# ADR 0002 — Conversa e execução duráveis em SQLite

**Status:** aceita (T04) · **Responde a:** feedback gap 2 ("não existe memória entre requests")

## Contexto
A v1 era stateless: "o segundo" numa resposta a um pedido de esclarecimento não tinha a que se referir, e um retry do cliente executava tudo de novo.

## Decisão
- SQLite (WAL, `BEGIN IMMEDIATE` nas escritas) com `conversations`, `messages` e `runs` (`app/state.py`).
- Turno em três passos: grava a intenção (run com lease), executa **sem** transação aberta, grava o resultado conferindo a versão da conversa. Turno concorrente → 409 `conversation_busy`; versão velha → 409.
- `Idempotency-Key` por tenant + sub com hash do pedido: mesmo corpo devolve o run gravado; corpo diferente → 409; run falho ou abandonado → reexecuta.
- Escolha em dois turnos resolvida pelo **servidor** contra a lista que ele apresentou (`parse_choice`); lista mudou ou escolha inválida → pergunta de novo sem LLM; escolha válida retoma o plano salvo sem o Router.
- Histórico vai ao Router como dado (últimos 4 turnos); retenção 72 h configurável.

## Alternativas descartadas
- Postgres: correto para várias instâncias, mas exige servidor e migrações; uma instância basta ao escopo. O acesso está atrás do `StateStore` (Protocol), então a troca não toca o orquestrador.
- Memória no LLM (passar a conversa inteira): não resolve idempotência nem concorrência, e põe a resolução de "o segundo" num componente não determinístico.

## Consequências
- Uma instância por volume. Escalar horizontalmente exige trocar o store.
- A retenção é técnica, não um prazo regulatório validado.

**Evidência:** `tests/test_conversations.py` (22 testes); `scripts/demo.py --restart` seções 3–4 (dois turnos atravessando reinício do container; repetição idempotente em 0,05 s).
