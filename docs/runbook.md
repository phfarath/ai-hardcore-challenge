# Runbook — Getnet Multi-Agent Support

Operação de **uma instância** (API + SQLite + índice Chroma em volumes Docker).

## Subir do zero

```bash
cp .env.example .env            # preencha OPENAI_API_KEY, OPENAI_CHAT_MODEL e (opcional) TAVILY_API_KEY
docker compose build
docker compose run --rm api python -m app.ingest    # cria o índice (≈25 páginas, 41 chunks)
docker compose up -d                                 # API em http://localhost:${API_PORT:-8000}
python scripts/smoke.py --base-url http://localhost:8000 --require-ready
```

`APP_ENV` do compose é `development` (emissor local de tokens, `POST /dev/token`). Em produção: `APP_ENV=production`, `AUTH_ISSUER` e `AUTH_JWKS_URL` (https) do provedor de identidade; a API recusa iniciar sem eles.

## Sinais

| Sinal | Saudável | Ação se não |
|---|---|---|
| `GET /health` | 200 | processo morto ou travado: ver `docker compose logs api` |
| `GET /ready` | 200 com `index=ready`, `state=ok`, `lifecycle=running` | ver tabela de falhas |
| evento `request_completed` | `llm_calls` ≤ 10, `duration_ms` < 40 000 | picos de chamadas indicam reparos frequentes (`replan`) |
| evento `request_failed` | raro | agrupar por `error_code` |
| evento `overloaded` | ausente | subir `MAX_CONCURRENT_CHATS` só se houver CPU/cota; senão, mais instâncias (exige trocar o store, ADR 0002) |
| evento `public_query_refused` | raro | o modelo tentou mandar dado do cliente para fora; revisar o prompt se frequente |
| evento `log` com `dropped_fields` | ausente | um chamador passou campo fora da allowlist: corrigir o código |

Logs são JSON, uma linha por evento, sem mensagem, resposta nem identidade do cliente.

## Falhas comuns

| Sintoma | Causa provável | Ação |
|---|---|---|
| `/ready` 503 `index: missing` | índice não ingerido ou volume novo | `docker compose run --rm api python -m app.ingest` |
| `/ready` 503 `index: incompatible` | `OPENAI_EMBEDDING_MODEL` diferente do usado na ingestão | voltar o modelo ou reingerir |
| `/ready` 503 `state: unavailable` | volume `state_data` sem permissão ou cheio | conferir o volume; o arquivo é `/data/state/state.db` |
| 503 `llm_unavailable` rápido, log `HTTP 401 invalid_api_key` | chave inválida ou revogada | trocar `OPENAI_API_KEY` e reiniciar |
| 503 `llm_unavailable`, log `insufficient_quota` | cota esgotada (não há retry) | renovar a cota |
| 504 `deadline_exceeded` | provedor lento | conferir latência do provedor; `DEADLINE_SECONDS` |
| 503 `overloaded` com `Retry-After: 2` | mais `/chat` simultâneos que o limite | ver `overloaded` acima |
| caso humano parado em `resolved` | worker de retomada parado (API fora) | subir a API: eventos pendentes são entregues na volta |
| API não sobe: `Permission denied: 'chroma_data'` | `.env` local com `CHROMA_PATH` relativo usado num container sem o compose | definir `CHROMA_PATH=/data/chroma` e `STATE_PATH=/data/state/state.db` |

Todas as linhas acima, exceto `incompatible` e `insufficient_quota`, foram provocadas e registradas em `docs/evidence/drills.json` (`python scripts/drills.py`).

## Dados e retenção

- Conversas, runs e casos humanos: `/data/state/state.db` (volume `state_data`). Retenção `CONVERSATION_RETENTION_HOURS` (72 h); casos vencem em `HANDOFF_TTL_HOURS` (24 h). Os valores são técnicos, não um prazo regulatório validado.
- Backup: com a API parada, copiar o arquivo do volume (`docker run --rm -v entrevista-getnet_state_data:/s -v "$PWD":/b alpine cp /s/state.db /b/`).
- Índice: reconstruível a qualquer momento pela ingestão; não precisa de backup.

## Desligar

`docker compose stop` (SIGTERM): a API sai do `/ready`, para de aceitar conexões, espera os pedidos em curso por até 20 s, para o worker de retomada e fecha os clientes dos provedores (evento `shutdown`). Os volumes permanecem.

## Trocar chaves

Editar `.env` e `docker compose up -d` (recria o container). Nunca colocar chaves em `docker-compose.yml`, logs ou commits.
