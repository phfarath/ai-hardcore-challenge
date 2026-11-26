# ADR 0004 — Verificação de afirmações e replanejamento limitado

**Status:** aceita (T06, T07) · **Responde a:** feedback gaps 4 ("orquestração plan-once") e 5 ("evidência existe ≠ evidência sustenta")

## Contexto
Na v1 uma evidência qualquer bastava para `answered` (x13: produto inexistente "respondido"), e o plano do Router era executado uma vez, sem observar o resultado.

## Decisão
**Verificação (T06, `app/verification.py`)**
- O Knowledge devolve `claims`: afirmação, id da evidência e cópia literal do trecho.
- Proveniência determinística: o trecho tem de existir literalmente numa evidência pública desta requisição.
- Uma chamada separada, sem ferramentas, julga suporte por afirmação e se o núcleo da pergunta foi respondido. Status pelo servidor: nada sustentado → `insufficient_evidence` (texto descartado); núcleo faltando → `partial` com só as afirmações aprovadas; verificador indisponível → o rascunho nunca é liberado.

**Replanejamento (T07, `app/replanning.py`)**
- Depois de cada etapa o código observa o resultado e pode mudar o plano **uma vez por pedido**: `knowledge_repair` (nova tentativa mirando as lacunas do verificador) ou `guidance_added` (código de erro no diagnóstico → busca de orientação oficial).
- Só começa se sobrarem 3 chamadas de LLM e 15 s; sem evidência nova, mantém o comprovado (`no_progress`). Retry de provedor não conta. Nunca amplia fontes nem ferramentas; o contador viaja com o plano salvo.
- A decisão é **código**, não um quarto agente "crítico": o verificador já é o crítico, e a política fica testável e barata.

## Alternativas descartadas
- Loop livre de reflexão pelo LLM: custo e latência sem limite, difícil de testar.
- Grafo genérico (LangGraph): o fluxo tem poucas transições; a máquina explícita é menor e foi uma restrição do projeto.

## Consequências
- O verificador é exigente: perguntas comparativas viram `partial` quando as fontes não comparam tudo (e01, x11). É o lado seguro do erro, mas reduz a taxa de `answered`; a variação entre runs está medida em `docs/evidence/`.
- Um reparo custa até 3 chamadas extras; os limites por pedido subiram para 10 chamadas de LLM, 5 ferramentas e 2 buscas web.

**Evidência:** `tests/test_verification.py`, `tests/test_replanning.py` (14 testes); evals com `plan_changes` por caso; `scripts/demo.py` seção 5.
