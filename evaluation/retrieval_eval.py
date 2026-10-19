"""Retrieval-quality evaluation over the synthetic corpus.

Run: uv run python -m evaluation.retrieval_eval [--embedder hashing|ollama]

Queries are retrieved through the real authorization filter, so quality is measured over
what each user is actually allowed to see. Chunks are collapsed to documents (best rank
wins). The offline hashing embedder is only a lexical baseline; use --embedder ollama to
measure the production model.
"""

import argparse
import asyncio
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import asyncpg

from app.config import get_settings
from app.retrieval.embeddings import Embedder, HashingEmbedder, OllamaEmbedder
from evaluation.corpus import EVAL_DIMENSION, USERS, build_stack
from evaluation.db import isolated_pool

DATASET_PATH = Path(__file__).parent / "dataset.jsonl"
REPORT_PATH = Path(__file__).parent / "reports" / "retrieval_report.json"
KS = (1, 3, 5)


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / k


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for position, document_id in enumerate(ranked, start=1):
        if document_id in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, doc in enumerate(ranked[:k]) if doc in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def dedupe_to_documents(chunk_document_ids: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for document_id in chunk_document_ids:
        seen.setdefault(document_id)
    return list(seen)


@dataclass
class QualityReport:
    embedder: str
    queries: int
    metrics: dict[str, float]
    misses: list[dict[str, object]]


async def evaluate(
    pool: asyncpg.Pool,
    embedder: Embedder,
    label: str,
    relative_cutoff: float = 0.0,
) -> QualityReport:
    service, corpus = await build_stack(pool, embedder, relative_cutoff)
    key_of = {document.document_id: document.key for document in corpus}
    lines = DATASET_PATH.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line]
    totals: dict[str, float] = {
        f"{name}@{k}": 0.0 for name in ("recall", "precision", "ndcg") for k in KS
    }
    totals["mrr"] = 0.0
    totals["context_precision"] = 0.0  # relevant share of what would reach the LLM
    totals["avg_context_docs"] = 0.0
    misses: list[dict[str, object]] = []
    for row in rows:
        chunks = await service.retrieve(row["query"], USERS[row["user"]])
        ranked = dedupe_to_documents([key_of[chunk.document_id] for chunk in chunks])
        relevant = set(row["relevant"])
        for k in KS:
            totals[f"recall@{k}"] += recall_at_k(ranked, relevant, k)
            totals[f"precision@{k}"] += precision_at_k(ranked, relevant, k)
            totals[f"ndcg@{k}"] += ndcg_at_k(ranked, relevant, k)
        totals["mrr"] += reciprocal_rank(ranked, relevant)
        totals["avg_context_docs"] += len(ranked)
        if ranked:
            totals["context_precision"] += len(set(ranked) & relevant) / len(ranked)
        if reciprocal_rank(ranked, relevant) < 1.0:
            misses.append({"query": row["query"], "expected": sorted(relevant), "got": ranked[:3]})
    return QualityReport(
        embedder=label,
        queries=len(rows),
        metrics={name: round(total / len(rows), 4) for name, total in totals.items()},
        misses=misses,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedder", choices=["hashing", "ollama"], default="hashing")
    parser.add_argument(
        "--relative-cutoff", type=float, help="default: RETRIEVAL_RELATIVE_CUTOFF"
    )
    parser.add_argument("--no-cutoff", action="store_true", help="evaluate raw top-K")
    args = parser.parse_args()
    settings = get_settings()
    if args.no_cutoff:
        relative_cutoff = 0.0
    else:
        relative_cutoff = (
            settings.retrieval_relative_cutoff
            if args.relative_cutoff is None
            else args.relative_cutoff
        )
    if args.embedder == "ollama":
        embedder: Embedder = OllamaEmbedder(
            settings.ollama_url, settings.embedding_model, settings.embedding_dimension
        )
        label = settings.embedding_model
    else:
        embedder, label = HashingEmbedder(EVAL_DIMENSION), f"hashing-{EVAL_DIMENSION}"

    async def run() -> QualityReport:
        async with isolated_pool() as pool:
            return await evaluate(pool, embedder, label, relative_cutoff)

    report = asyncio.run(run())
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    print(
        f"embedder: {report.embedder}  queries: {report.queries}  "
        f"relative_cutoff: {relative_cutoff}"
    )
    for name, value in report.metrics.items():
        print(f"  {name:<14}{value:.3f}")
    for miss in report.misses:
        print("  miss:", miss)


if __name__ == "__main__":
    main()
