import math

import asyncpg
import pytest

from app.retrieval.embeddings import HashingEmbedder
from evaluation.corpus import EVAL_DIMENSION
from evaluation.retrieval_eval import (
    dedupe_to_documents,
    evaluate,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_rank_metrics_on_a_known_ranking() -> None:
    ranked = ["x", "a", "y", "b"]
    relevant = {"a", "b"}

    assert recall_at_k(ranked, relevant, 2) == 0.5
    assert recall_at_k(ranked, relevant, 4) == 1.0
    assert precision_at_k(ranked, relevant, 2) == 0.5
    assert reciprocal_rank(ranked, relevant) == 0.5
    assert reciprocal_rank(["x"], relevant) == 0.0
    ideal = 1 + 1 / math.log2(3)
    assert ndcg_at_k(ranked, relevant, 4) == pytest.approx(
        (1 / math.log2(3) + 1 / math.log2(5)) / ideal
    )
    assert ndcg_at_k(["a", "b"], relevant, 2) == pytest.approx(1.0)


def test_chunks_collapse_to_documents_keeping_best_rank() -> None:
    assert dedupe_to_documents(["d1", "d1", "d2", "d1", "d3"]) == ["d1", "d2", "d3"]


@pytest.mark.asyncio
async def test_hashing_baseline_clears_a_minimum_quality_bar(pg_pool: asyncpg.Pool) -> None:
    report = await evaluate(pg_pool, HashingEmbedder(EVAL_DIMENSION), "hashing-test")

    assert report.queries >= 15
    assert report.metrics["recall@5"] >= 0.6
    assert report.metrics["mrr"] >= 0.4
