"""Cálculo das métricas de recuperação (T08) sobre rankings sintéticos com valores feitos à mão."""

import math

import pytest

A, B, C, X, Y = "a", "b", "c", "x", "y"


def _m():
    from evals import retrieval_metrics

    return retrieval_metrics


def test_recall_counts_each_relevant_url_once() -> None:
    m = _m()
    ranked = [X, A, A, Y, B]  # dois trechos da mesma página relevante
    assert m.recall_at_k(ranked, {A, B}, 1) == 0.0
    assert m.recall_at_k(ranked, {A, B}, 3) == 0.5
    assert m.recall_at_k(ranked, {A, B}, 5) == 1.0


def test_hit_and_reciprocal_rank() -> None:
    m = _m()
    assert m.hit_at_k([X, Y, A], {A}, 2) == 0.0
    assert m.hit_at_k([X, Y, A], {A}, 3) == 1.0
    assert m.reciprocal_rank([X, Y, A, B], {A, B}) == pytest.approx(1 / 3)
    assert m.reciprocal_rank([X, Y], {A}) == 0.0


def test_ndcg_binary_gain_ignores_repeated_url() -> None:
    m = _m()
    ranked = [X, A, A, B, Y]
    # ganhos nas posições 2 e 4 (o segundo "a" não conta de novo)
    dcg = 1 / math.log2(3) + 1 / math.log2(5)
    idcg = 1 / math.log2(2) + 1 / math.log2(3)
    assert m.ndcg_at_k(ranked, {A, B}, 5) == pytest.approx(dcg / idcg)
    assert m.ndcg_at_k([A, B], {A, B}, 5) == pytest.approx(1.0)
    assert m.ndcg_at_k([X, Y], {A}, 5) == 0.0


def test_score_query_and_aggregate() -> None:
    m = _m()
    first = m.score_query([A, X, X, X, X], {A})
    second = m.score_query([X, X, X, X, X], {A})
    assert first["recall@1"] == 1.0 and first["mrr"] == 1.0
    assert second["recall@5"] == 0.0 and second["ndcg@5"] == 0.0
    agg = m.aggregate([first, second])
    assert agg["recall@1"] == 0.5
    assert agg["mrr"] == 0.5


def test_cases_file_is_labeled() -> None:
    m = _m()
    cases = m.load_cases()
    assert 20 <= len(cases) <= 30
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.relevant_urls for c in cases)
    assert all(u.startswith("https://site.getnet.com.br/") for c in cases for u in c.relevant_urls)
