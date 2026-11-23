# ADR 0001 — Identidade por JWT, acesso resolvido no servidor

**Status:** aceita (T02) · **Responde a:** feedback gap 1 ("`user_id` não é identidade autenticada")

## Contexto
Na v1 o chamador escolhia o `user_id`; o LLM nunca recebia identidade, mas qualquer cliente HTTP podia pedir dados de outro estabelecimento.

## Decisão
- `POST /chat` exige `Authorization: Bearer <JWT RS256>`; emissor, audiência, validade, `sub` e `tenant_id` são verificados (`app/auth.py`).
- O mapa principal → estabelecimentos é do servidor (`AccessRepository`); o `user_id` do payload virou **seletor**, conferido contra esse mapa antes do Router (`app/policies.py`). Seletor alheio → 403; recurso de outro dono → 404; escopo ausente → 403 `insufficient_scope`.
- Ferramentas declaram `required_scope`; o LLM só vê as ferramentas que o principal pode usar.
- Desenvolvimento: emissor local com chave efêmera e `POST /dev/token` (só reduz escopos). Produção (`APP_ENV=production`, padrão) exige `AUTH_ISSUER` + `AUTH_JWKS_URL` https e recusa o emissor local.

## Alternativas descartadas
- Sistema próprio de usuários e senhas: fora do escopo e pior que delegar a um provedor de identidade.
- API key por estabelecimento: não carrega escopos nem papel de operador.

## Consequências
- Demo e evals usam o emissor local pelo mesmo caminho criptográfico da produção.
- Sem revogação de token antes do `exp` (TTL curto, 15 min no emissor local); a revogação de *acesso* é imediata porque o mapa é consultado a cada pedido.

**Evidência:** `tests/test_auth.py` (29 testes); `scripts/demo.py` seção 1; `scripts/smoke.py` (401/403 sem LLM).
