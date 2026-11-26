# ADR 0005 — Fronteira de privacidade e operação de uma instância

**Status:** aceita (T09) · **Responde a:** critérios de Observability, Reliability & Cost e Production Engineering

## Decisão
- **O que sai do processo é conferido**: consultas para busca web e para embeddings passam por `app/privacy.py` (valores privados vistos no pedido e padrões de dado pessoal/financeiro). Recusa vira erro da ferramenta; o log guarda só a categoria.
- **Logs com allowlist** em vez de denylist: campo novo não sai até ser declarado seguro.
- **Prazo manda**: cada chamada de LLM e busca usa no máximo o tempo restante; retry só para erro transitório e se couber no prazo; cota esgotada não repete.
- **Liveness ≠ readiness**: `/health` não depende de provedores; `/ready` confere índice, estado e ciclo de vida sem gastar cota.
- **Saturação explícita**: limite de `/chat` simultâneos por processo com 503 imediato e `Retry-After`, em vez de fila sem prazo.
- **Imagem testada = imagem publicada**: o CI roda a suíte dentro do alvo `test`, faz smoke e SIGTERM no alvo `api` com a tag do commit.

## Alternativas descartadas
- Tracing distribuído (OpenTelemetry) agora: uma instância e logs com `request_id` cobrem o diagnóstico; fica como passo de produção.
- Fila com espera para picos: esconde a saturação e estoura o deadline do cliente.

## Consequências
- A checagem de privacidade é por padrões e valores conhecidos; formatos novos de dado pessoal exigem atualizar os padrões.
- Os limites (8 simultâneos, timeouts) são pontos de partida, não medidos sob carga real.

**Evidência:** `tests/test_operations.py` (21 testes, incluindo canários num fluxo completo); `docs/evidence/drills.json` (saturação, provedor fora, índice ausente, SIGTERM com pedido em curso); job `image` do CI e `scripts/smoke.py`.
