# Evidência da revisão v2

Cada crítica do feedback (7,9/10) aponta para **implementação, teste e evidência executada**. Nada aqui é só descrito: o que não foi entregue está marcado como tal.

Código congelado para a evidência final: commit `6ae47f2`, com a árvore limpa durante as rodadas (manifesto em [`release-manifest.json`](release-manifest.json)).

## Feedback → implementação → teste → evidência

| # | Crítica do feedback | Implementação | Testes | Evidência executada |
|---|---|---|---|---|
| 1 | `user_id` não é identidade autenticada (Security 6,0) | JWT RS256 → `Principal`; acesso resolvido no servidor; `user_id` só seletor; escopos por ferramenta (`app/auth.py`, `app/policies.py`, ADR 0001) | `tests/test_auth.py` (29) | `demo.txt` seção 1 (401, 403 antes do LLM, identidade no texto ignorada); smoke no CI e em `smoke.txt`; eval `x15` |
| 2 | Não existe memória entre requests (Memory 1,5) | Conversa, mensagens e runs em SQLite com lease, versão e `Idempotency-Key`; "o segundo" resolvido pelo servidor; follow-up com pronome reescrito pelo Router como pergunta autocontida e verificado contra ela (`app/state.py`, `app/conversations.py`, `app/prompts.py`, ADR 0002) | `tests/test_conversations.py` (22), `tests/test_followup.py` (2) | `demo.txt` seções 3–4: dois turnos **atravessando reinício do container**, repetição idempotente, 409 com outro corpo; eval `x12`; eval `x17`: pronome resolvido nas 3 rodadas, mas o caso falha por rotulagem (veja a revisão semântica) |
| 3 | Não existe HITL real (Human Oversight 2,5) | Caso aberto na mesma transação do turno; fila, `claim`/`resolve` versionados com prazo; worker de retomada com lease; auditoria; pedido explícito de atendente; `status=handoff_opened` quando há caso real, `handoff_recommended` só para orientação (`app/handoffs.py`, ADR 0003) | `tests/test_handoffs.py` (14), `tests/test_replanning.py` | `demo.txt` seção 6 (caso → fila → assumir → 409 → decidir → resposta humana na conversa); eval `x16` = `handoff_opened` nas 3 rodadas |
| 4 | Orquestração plan-once (Agentic 7,2) | Observação depois de cada etapa e uma mudança de plano por pedido: `knowledge_repair`, `guidance_added`; reserva de orçamento; parada sem progresso (`app/replanning.py`, ADR 0004) | `tests/test_replanning.py` (14) | `plan_changes` por caso nos relatórios finais (reparos com e sem progresso); `demo.txt` seção 5 |
| 5 | x13: evidência existe ≠ evidência sustenta | Claims com trecho literal, proveniência determinística e verificador semântico separado; status pelo servidor (`app/verification.py`, ADR 0004) | `tests/test_verification.py` (11), `tests/test_regressions.py` | `x13` = `insufficient_evidence` em todas as rodadas finais |
| 6 | Avaliação semântica incompleta | Avaliação repetida (3 rodadas, `evals/summarize.py`) e revisão semântica caso a caso, separada da pontuação objetiva; os defeitos que ela achou viraram regressões (D2, D4, D5 em `tests/test_review_defects.py`). **Parcial:** é uma revisão registrada pelo agente que implementou, não um avaliador independente | `tests/test_review_defects.py` (9) | [`semantic-review.md`](semantic-review.md), [`eval-summary.json`](eval-summary.json) |
| 7 | Retrieval pouco medido (Recall@K, MRR, ablações) | Medição, sem mudar a recuperação: 27 consultas rotuladas, Recall@1/3/5, Hit@K, MRR, nDCG@5 e ablação do `top_k` (`evals/retrieval_metrics.py`, `evals/retrieval_cases.json`) | `tests/test_retrieval_metrics.py` (5) | [`retrieval-metrics.json`](retrieval-metrics.json) no commit limpo `57c05d3`: Recall@5 0,91, MRR 0,87, Hit@5 1,00; inglês pior que português (0,78 × 0,98); achados e rotulagem em [`retrieval-labeling.md`](retrieval-labeling.md) |

Também da revisão (sem crítica numerada, mas no plano): fatos financeiros só do servidor e diagnóstico guiado pela intenção (T03, `tests/test_regressions.py`); avaliador sem falsos positivos (T01, `tests/test_eval_cases.py`); fronteira de privacidade, prontidão, desligamento e saturação (T09, `tests/test_operations.py`, `drills.json`, ADR 0005).

## Execuções registradas

| Execução | Resultado | Arquivo |
|---|---|---|
| Suíte offline (host e dentro da imagem `test`) | **328 passed**; ruff, format e mypy limpos | job `verify` e `image` do CI |
| Smoke na API construída (sem LLM) | OK: prontidão com checks, 401, 403 antes do Router | [`smoke.txt`](smoke.txt) |
| Demo ponta a ponta com reinício do container no meio | **21/21 PASS** (commit `6ae47f2`) | [`demo.txt`](demo.txt) |
| Avaliação real, 3 rodadas no mesmo commit limpo `6ae47f2` | **15/17, 13/17, 13/17**, 0 erros; rota, ferramentas, fontes esperadas, valores e afirmações proibidas 100% nas 3 | [`eval-summary.json`](eval-summary.json), `evals/reports/run-20261125T170355Z.json` / `170713Z` / `171028Z` |
| Métricas de recuperação, 27 consultas | Recall@5 0,91, MRR 0,87, nDCG@5 0,84, Hit@5 1,00 (commit limpo `57c05d3`) | [`retrieval-metrics.json`](retrieval-metrics.json), [`retrieval-labeling.md`](retrieval-labeling.md) |
| Revisão semântica | sem afirmação sem fonte; D4 e D5 corrigidos, D2 parcial, D1 e D3 abertos; x17 mal rotulado | [`semantic-review.md`](semantic-review.md) |
| Saturação (limite 2, 6 simultâneos) | 2 atendidos (p50 ≈ 10,7 s), 4 recusados com 503 `overloaded` em ≤ 0,07 s com `Retry-After: 2` | [`drills.json`](drills.json) |
| Chave do provedor inválida | 503 `llm_unavailable` em 0,53 s, sem retry; log `HTTP 401 invalid_api_key` | [`drills.json`](drills.json) |
| Índice ausente | `/ready` 503 (`index: missing`); pergunta de RAG → 503 `index_unavailable` | [`drills.json`](drills.json) |
| SIGTERM com pedido em curso | o pedido termina (200); log `Shutting down` → `shutdown/clients_closed` | [`drills.json`](drills.json) |

A carga medida é pequena (6 pedidos simultâneos numa instância): prova o comportamento sob saturação, não a capacidade. Os drills rodaram em `6b7df82`; o código que eles exercitam (`app/main.py`, `app/llm.py`, `app/state.py`, `app/deps.py`, `app/rag.py`, `app/tools/`, `Dockerfile`, `docker-compose.yml`) não mudou até `6ae47f2`.

Comparação com o placar da v1 (14/15): os relatórios por caso da v1 não foram guardados e a API v1 não tem `/dev/token`, então o scorer v2 não foi rodado contra ela. O scorer v2 é mais estrito por construção: os defeitos que a v1 aprovava foram registrados como regressões `xfail(strict=True)` antes das correções (`tests/test_regressions.py`, `tests/test_eval_cases.py`).

## Como reproduzir

Veja [`docs/demo.md`](../demo.md) e [`docs/runbook.md`](../runbook.md). Em resumo: `docker compose build`, ingestão, `docker compose up -d`, e então `scripts/smoke.py`, `scripts/demo.py --restart`, `scripts/drills.py` e `python -m evals.run_evals`.
