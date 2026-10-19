"""Which table serialization retrieves best? Run: uv run python -m evaluation.table_eval

Builds long tables that share row labels (so a row is only identifiable through its table's
caption and header), splits them with the production `split_table`, serializes each row group
three ways, embeds with the asset embedding model and measures how often a "what is column X
for row Y in table Z" question finds the right row group. Synthetic and small: it ranks
formats on this kind of lookup, it does not prove anything about your own documents.
"""

import argparse
import asyncio
import hashlib
import math

from app.config import get_settings
from app.ingestion.loaders import split_table
from app.retrieval.embeddings import Embedder, HashingEmbedder, OllamaEmbedder

LABELS = [
    "Oslo",
    "Bergen",
    "Lyon",
    "Porto",
    "Cork",
    "Graz",
    "Leeds",
    "Turin",
    "Ghent",
    "Basel",
    "Malmo",
    "Split",
    "Brno",
    "Riga",
    "Bonn",
    "Nancy",
    "Seville",
    "Utrecht",
    "Aarhus",
    "Linz",
    "Ostrava",
    "Tampere",
    "Zurich",
    "Antwerp",
    "Cardiff",
    "Dundee",
    "Essen",
    "Genoa",
    "Hull",
    "Kiel",
]
TABLES = [
    ("Table 1. Headcount and payroll by office", "Office", ["Headcount", "Payroll (EUR k)"]),
    ("Table 2. Server inventory by data centre", "Site", ["Racks", "Power draw (kW)"]),
    (
        "Table 3. Rainfall and temperature by weather station",
        "Station",
        ["Rainfall (mm)", "Temp (C)"],
    ),
    ("Table 4. Freight volume by logistics hub", "Hub", ["Containers", "Dwell time (h)"]),
    ("Table 5. Student intake by campus", "Campus", ["Enrolled", "Scholarships"]),
    ("Table 6. Retail sales by store", "Store", ["Units sold", "Returns"]),
    ("Table 7. Energy use by plant", "Plant", ["Consumption (MWh)", "Outages"]),
    ("Table 8. Patient admissions by clinic", "Clinic", ["Admissions", "Wait time (min)"]),
]
QUESTIONS_PER_TABLE = 8


def value(table: int, label: str, column: str) -> int:
    digest = hashlib.sha256(f"{table}|{label}|{column}".encode()).digest()
    return 100 + int.from_bytes(digest[:3], "big") % 9_000


def markdown(index: int) -> str:
    caption, label_header, columns = TABLES[index]
    header = f"| {label_header} | " + " | ".join(columns) + " |"
    rows = [
        f"| {label} | " + " | ".join(f"{value(index, label, c):,}" for c in columns) + " |"
        for label in LABELS
    ]
    return caption + "\n\n" + "\n".join([header, "|---|" + "---|" * len(columns), *rows])


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def serialize(chunk: str, style: str) -> str:
    """Re-render one row group (caption, header, separator, rows) in the chosen style."""
    lines = chunk.splitlines()
    caption, header, rows = lines[0], cells(lines[1]), [cells(line) for line in lines[3:]]
    if style == "markdown":
        return chunk
    if style == "csv":
        return "\n".join([caption, ",".join(header), *(",".join(r) for r in rows)])
    sentences = [
        f"{header[0]} {r[0]}: "
        + "; ".join(f"{h} is {v}" for h, v in zip(header[1:], r[1:], strict=True))
        + "."
        for r in rows
    ]
    return "\n".join([caption, *sentences])  # "natural-language rows"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


async def evaluate(embedder: Embedder, max_chars: int) -> dict[str, tuple[float, float, int]]:
    groups: list[tuple[int, list[str], str]] = []  # (table, labels in group, markdown chunk)
    for t in range(len(TABLES)):
        for chunk in split_table(markdown(t), max_chars):
            labels = [cells(line)[0] for line in chunk.splitlines()[3:]]
            groups.append((t, labels, chunk))
    questions = []
    for t, (caption, label_header, columns) in enumerate(TABLES):
        topic = caption.split(". ", 1)[1].lower()
        for k in range(QUESTIONS_PER_TABLE):
            label = LABELS[(k * 7 + t * 3) % len(LABELS)]
            column = columns[k % len(columns)]
            questions.append(
                (f"In the table of {topic}, what is the {column} for {label}?", t, label)
            )
    query_vectors = await embedder.embed_many([q for q, _, _ in questions])

    results: dict[str, tuple[float, float, int]] = {}
    for style in ("markdown", "csv", "sentences"):
        texts = [serialize(chunk, style) for _, _, chunk in groups]
        vectors = await embedder.embed_many(texts)
        hits, reciprocal = 0, 0.0
        for (_, t, label), qv in zip(questions, query_vectors, strict=True):
            ranked = sorted(range(len(groups)), key=lambda i: -cosine(qv, vectors[i]))
            rank = next(
                n for n, i in enumerate(ranked, 1) if groups[i][0] == t and label in groups[i][1]
            )
            hits += rank == 1
            reciprocal += 1 / rank
        results[style] = (hits / len(questions), reciprocal / len(questions), len(groups))
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedder", choices=["ollama", "hashing"], default="ollama")
    parser.add_argument("--max-chars", type=int, default=600, help="row-group size")
    args = parser.parse_args()
    settings = get_settings()
    embedder: Embedder = (
        OllamaEmbedder(
            settings.ollama_url, settings.asset_embedding_model, settings.asset_embedding_dimension
        )
        if args.embedder == "ollama"
        else HashingEmbedder(1024)
    )
    results = await evaluate(embedder, args.max_chars)
    n = len(TABLES) * QUESTIONS_PER_TABLE
    print(f"{n} questions, embedder={embedder.model}, row groups of <= {args.max_chars} chars")
    print(f"{'serialization':<14}{'Recall@1':>10}{'MRR':>8}{'chunks':>8}")
    for style, (recall, mrr, chunks) in results.items():
        print(f"{style:<14}{recall:>10.3f}{mrr:>8.3f}{chunks:>8}")


if __name__ == "__main__":
    asyncio.run(main())
