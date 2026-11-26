# AGENTS.md

Serviço FastAPI com 3 agentes (Router, Knowledge, Customer Support). Comportamento em `spec.md`; visão geral em `README.md`.

## Verificação (única, antes de dizer "pronto")

```bash
python -m ruff check . && python -m ruff format --check . && python -m mypy app && python -m pytest
```
Testes padrão não usam rede nem chaves. Os que usam serviços reais têm `@pytest.mark.live` (`pytest -m live`).

## Mapa

| Caminho | O que é |
|---|---|
| `app/main.py` | `create_app`, `/chat`, `/health`, mapeamento de erros HTTP, `build_deps` |
| `app/orchestrator.py` | fluxos, isolamento de falhas por ramo, composição, `merge_status` |
| `app/replanning.py` | política de replanejamento limitado (reparo de lacunas, orientação acrescentada) |
| `app/privacy.py`, `app/observability.py` | checagem de consultas públicas; logs com allowlist |
| `app/agents/{router,knowledge,support}.py` | os 3 agentes; prompts em `app/prompts.py` |
| `app/llm.py` | `LLMClient`, `OpenAIClient`, `ToolSpec`, `run_agent` (loop de tool calling) |
| `app/tools/` | ferramentas: `customer.py`, `knowledge.py`, `search.py` (Tavily) |
| `app/rag.py`, `app/ingest.py` | chunking, índice Chroma, ingestão (`python -m app.ingest`) |
| `app/auth.py`, `app/policies.py` | JWT → `Principal`, emissor de desenvolvimento; autorização por cliente, escopo, dono e operador |
| `app/repositories.py` | `CustomerRepository` + fixtures JSON (`data/fixtures/`) |
| `app/schemas.py`, `app/context.py` | contratos Pydantic; `RequestContext`, `Budget`, exceções |
| `tests/fakes.py` | `ScriptedLLM`, `FakeEmbedder`, `FakeSearch`, `FrozenClock`, `make_deps` |
| `evals/` | casos e runner de avaliação de IA (exige API e chaves); `retrieval_metrics.py` mede a recuperação do índice |
| `scripts/smoke.py` | smoke contra um container no ar (usado no CI sobre a imagem construída) |

## Regras

- **Segurança**: nunca ler, imprimir, logar ou commitar segredos (`.env`, chaves). Só `.env.example` é versionado.
- **Identidade**: vem do token (`Principal`); o `user_id` do payload é só um seletor autorizado por `policies.authorize_chat` e vive só em `RequestContext`. Ferramentas **não** recebem identidade como argumento (`args_model` com `extra="forbid"`). Não criar estado global por usuário.
- **Evidências**: o LLM seleciona ids; URLs e títulos vêm dos metadados do servidor. Valores financeiros saem do template (`render_receivables`), nunca da geração.
- **Dependências externas** (LLM, embeddings, busca, clientes) são `Protocol`s com implementação real e falsa; testes usam as falsas.
- Type hints em `app/`; Pydantic nas fronteiras; erros com tipo próprio (sem `except Exception` genérico); prompts só em `app/prompts.py`; config só via `Settings`.
- Docstrings curtas só em API pública e decisões não óbvias (o porquê); sem comentários que narram o código.
- Mudar comportamento → atualizar `spec.md`, o teste de aceite correspondente e, se afetar rota/resposta, `evals/cases.json`.
- Git: trabalhar em branch; não commitar na principal; não commitar sem pedido do usuário.
