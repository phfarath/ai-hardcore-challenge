"""Métricas de recuperação do índice atual (T08): Recall@1/3/5, Hit@K, MRR e nDCG@5 por consulta e
no agregado, mais uma ablação do `top_k` de produção. Só mede: não muda a recuperação.

Roda direto contra o Chroma local, sem LLM de chat (uma chamada de embedding por consulta):

    python -m evals.retrieval_metrics [--out docs/evidence/retrieval-metrics.json]

A relevância é por página (URL): vários trechos da mesma página relevante contam uma vez só,
na posição do primeiro. O ranking é o mesmo que `retrieve_getnet_knowledge` recebe (mesmo índice,
mesmo filtro de mercado).
"""

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evals.run_evals import _git_commit

CASES_FILE = Path(__file__).with_name("retrieval_cases.json")
CUTOFFS = (1, 3, 5)
ABLATION_TOP_K = (3, 5, 8, 10)
PRODUCTION_TOP_K = 5  # app/tools/knowledge.py:TOP_K


class RetrievalCase(BaseModel):
    id: str
    query: str
    language: str
    relevant_urls: list[str] = Field(min_length=1)
    from_eval_case: str | None = None


def load_cases(path: Path = CASES_FILE) -> list[RetrievalCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [RetrievalCase.model_validate(c) for c in raw["cases"]]


def _gains(ranked: list[str], relevant: set[str], k: int) -> list[int]:
    seen: set[str] = set()
    gains = []
    for url in ranked[:k]:
        gains.append(int(url in relevant and url not in seen))
        seen.add(url)
    return gains


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return sum(_gains(ranked, relevant, k)) / len(relevant)


def hit_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return float(any(_gains(ranked, relevant, k)))


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for position, url in enumerate(ranked, start=1):
        if url in relevant:
            return 1 / position
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(_gains(ranked, relevant, k)))
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / idcg


def score_query(ranked: list[str], relevant: set[str]) -> dict[str, float]:
    scores = {f"recall@{k}": recall_at_k(ranked, relevant, k) for k in CUTOFFS}
    scores |= {f"hit@{k}": hit_at_k(ranked, relevant, k) for k in CUTOFFS}
    scores["mrr"] = reciprocal_rank(ranked[: max(CUTOFFS)], relevant)
    scores["ndcg@5"] = ndcg_at_k(ranked, relevant, 5)
    return scores


def aggregate(per_query: list[dict[str, float]]) -> dict[str, float]:
    keys = per_query[0].keys()
    return {k: round(sum(q[k] for q in per_query) / len(per_query), 4) for k in keys}


def ablation(rankings: list[list[str]], cases: list[RetrievalCase]) -> list[dict[str, Any]]:
    """O que o agente veria com outro `top_k`: recall e páginas distintas entregues por chamada."""
    rows = []
    for top_k in ABLATION_TOP_K:
        recalls = [
            recall_at_k(r, set(c.relevant_urls), top_k)
            for r, c in zip(rankings, cases, strict=True)
        ]
        hits = [
            hit_at_k(r, set(c.relevant_urls), top_k) for r, c in zip(rankings, cases, strict=True)
        ]
        pages = [len(set(r[:top_k])) for r in rankings]
        rows.append(
            {
                "top_k": top_k,
                "production": top_k == PRODUCTION_TOP_K,
                "recall": round(sum(recalls) / len(recalls), 4),
                "hit": round(sum(hits) / len(hits), 4),
                "distinct_pages_per_call": round(sum(pages) / len(pages), 2),
            }
        )
    return rows


async def _rank_all(cases: list[RetrievalCase], market: str) -> tuple[list[list[str]], str | None]:
    from app.config import Settings
    from app.rag import KnowledgeIndex, OpenAIEmbedder

    settings = Settings()
    embedder = OpenAIEmbedder(
        settings.openai_api_key.get_secret_value(), settings.openai_embedding_model
    )
    index = KnowledgeIndex(settings.chroma_path, embedder)
    try:
        rankings = []
        for case in cases:
            hits = await index.search(case.query, top_k=max(ABLATION_TOP_K), market=market)
            rankings.append([h.chunk.source_url for h in hits])
        return rankings, index.version
    finally:
        await index.aclose()


def build_report(
    cases: list[RetrievalCase], rankings: list[list[str]], index_version: str | None
) -> dict[str, Any]:
    per_query = [score_query(r, set(c.relevant_urls)) for r, c in zip(rankings, cases, strict=True)]
    return {
        "commit": _git_commit(),
        "generated_at": datetime.now(UTC).isoformat(),
        "index_version": index_version,
        "production_top_k": PRODUCTION_TOP_K,
        "queries": len(cases),
        "aggregate": aggregate(per_query),
        "aggregate_by_language": {
            lang: aggregate(
                [q for q, c in zip(per_query, cases, strict=True) if c.language == lang]
            )
            for lang in sorted({c.language for c in cases})
        },
        "ablation_top_k": ablation(rankings, cases),
        "per_query": [
            {
                "id": c.id,
                "query": c.query,
                "language": c.language,
                "from_eval_case": c.from_eval_case,
                "relevant_urls": c.relevant_urls,
                "ranked_urls": r[:PRODUCTION_TOP_K],
                **{k: round(v, 4) for k, v in s.items()},
            }
            for c, r, s in zip(cases, rankings, per_query, strict=True)
        ],
        "misses_at_production_top_k": [
            c.id
            for c, s in zip(cases, per_query, strict=True)
            if s[f"hit@{PRODUCTION_TOP_K}"] == 0.0
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("evals/reports/retrieval-metrics.json"))
    parser.add_argument("--market", default="BR")
    args = parser.parse_args()
    cases = load_cases()
    rankings, version = asyncio.run(_rank_all(cases, args.market))
    report = build_report(cases, rankings, version)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['commit']}: {json.dumps(report['aggregate'])}")
    for row in report["ablation_top_k"]:
        print(row)
    print("misses @top_k:", report["misses_at_production_top_k"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
