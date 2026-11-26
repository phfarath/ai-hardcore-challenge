# Métricas de recuperação (T08)

Responde à crítica 7 do feedback ("Recall@K, MRR, ablações"). **Só medição:** o índice, o chunking e a busca são os mesmos da v1 (25 páginas, 41 trechos, `text-embedding-3-small`, `top_k = 5`, filtro de mercado `BR`).

- Consultas: [`evals/retrieval_cases.json`](../../evals/retrieval_cases.json) (27 consultas: 10 em inglês, 17 em português; 7 vêm dos casos do eval de IA).
- Script: [`evals/retrieval_metrics.py`](../../evals/retrieval_metrics.py) roda contra o Chroma local, sem LLM de chat (uma chamada de embedding por consulta).
- Teste offline do cálculo: [`tests/test_retrieval_metrics.py`](../../tests/test_retrieval_metrics.py) (rankings sintéticos com valores feitos à mão).
- Relatório: [`retrieval-metrics.json`](retrieval-metrics.json), commit limpo `57c05d3`.

## Quem rotulou e como

Rotulado por Claude, o agente que implementou a revisão, em 25/11/2026, lendo o texto dos 41 trechos do índice. **Relevante = página cujo texto responde diretamente à consulta.** A relevância é por página (URL): vários trechos da mesma página contam uma vez, na posição do primeiro.

- As 7 consultas que vêm do eval (`from_eval_case`) partem das fontes esperadas daquele caso, reduzidas às páginas que de fato respondem. Exemplo: no e04, as páginas de Pix explicam como vender com Pix, mas a pergunta "preciso de conta bancária?" é respondida pela Get Conta, pela conta digital e pela FAQ (Superdigital).
- As outras 20 são paráfrases ou perguntas novas, uma ou duas por página do índice.
- O rótulo não foi revisado por uma segunda pessoa. Isso é um limite da medição.

## Resultado (top_k de produção = 5)

| Métrica | Todas (27) | Português (17) | Inglês (10) |
|---|---|---|---|
| Recall@1 | 0,63 | 0,81 | 0,32 |
| Recall@3 | 0,85 | 0,96 | 0,67 |
| Recall@5 | 0,91 | 0,98 | 0,78 |
| Hit@5 (alguma página relevante) | 1,00 | 1,00 | 1,00 |
| MRR | 0,87 | 0,93 | 0,78 |
| nDCG@5 | 0,84 | 0,92 | 0,69 |

**Ablação do `top_k`** (o que o agente receberia em uma chamada):

| top_k | Recall | Hit | Páginas distintas por chamada |
|---|---|---|---|
| 3 | 0,85 | 0,96 | 2,6 |
| **5 (produção)** | **0,91** | **1,00** | 3,8 |
| 8 | 0,94 | 1,00 | 5,6 |
| 10 | 0,96 | 1,00 | 6,5 |

Subir de 5 para 8 ganha 3 pontos de recall ao custo de cerca de 60% mais texto por chamada. Com 3, uma consulta fica sem nenhuma página relevante.

## Consultas fracas e hipótese da causa

Nenhuma consulta ficou sem página relevante no top 5. As que ficaram abaixo de 1 em Recall@5 ou em MRR:

| Consulta | Recall@5 | RR | Hipótese |
|---|---|---|---|
| r04 "Do I need a bank account to receive my sales via Pix?" (e04) | 0,33 | 0,25 | A palavra "Pix" puxa as 3 páginas de Pix (que não respondem) para o topo; a resposta está na Get Conta e na FAQ. Pergunta em inglês contra páginas em português. |
| r12 "How does receivables advance work?" (e06) | 0,33 | 1,00 | A FAQ (`/duvidas/`, 14 dos 41 trechos) ocupa 4 das 5 posições; as duas páginas de antecipação automática ficam de fora. |
| r01 "Difference between Get Clássica and Get Smart?" (e01) | 0,67 | 0,50 | A FAQ fica em 1º; a página de aluguel (comparativo) não entra. |
| r27 "transaction decline error" (e08) | 0,50 | 0,50 | O índice não tem página sobre recusa por código (51); o melhor que existe é a de status das vendas. É falta de cobertura, não de ranking. |
| r05 "receber sem conta em banco?" | 0,67 | 1,00 | A FAQ (Superdigital) fica fora do top 5. |
| r08, r20, r22 | 1,00 | 0,33–0,50 | Página irmã do mesmo tema na frente (outra página de Pix, o artigo sobre Link de Pagamento, a página "venda desfeita"). |

**Achados (não corrigidos, por escopo):**

1. **Inglês recupera pior** (Recall@5 0,78 contra 0,98). O corpus é todo em português. Próximo passo: traduzir a consulta para português antes do embedding, ou indexar um resumo bilíngue por trecho, e medir de novo com este mesmo script.
2. **A FAQ domina o top_k.** `/duvidas/` tem 14 trechos e aparece em quase todas as listas, empurrando páginas específicas para fora. Próximo passo: limitar a 2 trechos por página no resultado (diversidade por URL) e medir.
3. **Cobertura:** a recusa por código (e08) não tem página no índice. Nenhum ajuste de ranking resolve; é preciso ingerir a página oficial de códigos de recusa.

O recall alto também reflete o tamanho do índice: com 25 páginas, quase toda consulta acha alguma página certa. As métricas medem se a página certa está entre as indexadas que voltam, não se o índice cobre o produto.

## Como reproduzir

```bash
python -m evals.retrieval_metrics --out docs/evidence/retrieval-metrics.json
```

Exige `OPENAI_API_KEY` no `.env` (só embeddings) e o índice em `CHROMA_PATH`.
