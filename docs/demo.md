# Roteiro de demonstração

Reproduz, em ~5 minutos, os cenários que respondem ao feedback. Pré-requisito: API no ar com índice (veja `docs/runbook.md`).

## Automático

```bash
python scripts/smoke.py --base-url http://localhost:8000 --require-ready   # sem LLM
python scripts/demo.py  --base-url http://localhost:8000 --restart         # cenários, com reinício no meio
python scripts/drills.py                                                    # carga e falhas provocadas (sobe containers próprios)
python -m evals.run_evals --base-url http://localhost:8000 --timeout 90     # 17 casos
python -m evals.retrieval_metrics                                           # métricas de recuperação (só embeddings)
```

`demo.py` imprime PASS/FAIL por passo e sai com código 1 se algo falhar. A saída da execução registrada está em `docs/evidence/demo.txt`.

## Manual (para gravar ou apresentar)

Token de desenvolvimento (só `APP_ENV=development`):

```bash
TOKEN=$(curl -s -X POST localhost:8000/dev/token -H 'Content-Type: application/json' -d '{"sub":"user-a"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
chat() { curl -s -X POST localhost:8000/chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$1" | python -m json.tool; }
```

| # | O que mostrar | Pedido | O que observar |
|---|---|---|---|
| 1 | Identidade pelo token | `chat '{"message":"Quando recebo?","user_id":"cliente2000"}'` | 403 `forbidden`, nenhuma chamada ao LLM |
| 2 | Recebíveis do servidor | `chat '{"message":"Quando recebo minhas vendas de ontem?"}'` | valores e "previsto" renderizados pelo servidor, `data_mode: demo` |
| 3 | RAG com fonte | `chat '{"message":"Preciso de conta bancária para receber via Pix?"}'` | `sources` com página da Getnet; `plan_changes` se houve reparo |
| 4 | Produto inexistente | `chat '{"message":"Qual é a taxa da maquininha Getnet Quantum 9000?"}'` | `insufficient_evidence`, sem texto inventado |
| 5 | Replanejamento | `chat '{"message":"Qual é o status da minha maquininha agora?"}'` | `plan_changes: ["guidance_added"]`, orientação oficial do ADQ 1-19 |
| 6 | Dois turnos (token de `user-b`) | "Minha maquininha não conecta." e depois `{"message":"o segundo","conversation_id":"…"}` | lista de terminais; depois diagnóstico do T-2000-02 sem o Router |
| 7 | Caso humano | `chat '{"message":"Quero falar com um atendente."}'` | `status: handoff_opened`, `handoff_id`, `run_status: awaiting_operator` |
| 8 | Operador (token de `operator-1`) | `GET /operator/handoffs`, `POST …/claim {"expected_version":1}`, `POST …/resolve {...}` | 409 ao repetir; resposta humana aparece em `GET /conversations/{id}` |
| 8b | Follow-up com pronome | "O que é o modo quiosque da Get Smart?" e depois `{"message":"E como eu ativo ele?","conversation_id":"…"}` | o cabeçalho da segunda resposta traz a pergunta resolvida ("modo quiosque da Get Smart") |
| 9 | Prontidão e desligamento | `GET /ready`; `docker compose stop` | checks; log `{"event":"shutdown","status":"clients_closed"}` |

Principais na demo: `user-a` → cliente1988 (um terminal offline com ADQ 1-19 e recusa 51), `user-b` → cliente2000 (dois terminais), `user-c` → cliente sem cadastro, `operator-1` → operador do tenant.
