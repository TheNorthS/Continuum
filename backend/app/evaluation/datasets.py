"""Pinned public sources; deterministic sampled corpora with complete sampled positives."""

import hashlib
import json
import random
from dataclasses import dataclass

import httpx

from app.config import ROOT

DATA_ROOT = ROOT / ".cache/evaluation/datasets"
T2_REV = "8731a845f1bf500a4f111cf1070785c793d10e64"
T2_QRELS_REV = "1c83b8d1544e529875e3f6930f3a1fcf749a8e97"
CRUD_REV = "1aace383994e1f68efa12cf2a8e2dadfb4102ceb"
SOURCES = {
    "t2/corpus.parquet": f"https://huggingface.co/datasets/C-MTEB/T2Retrieval/resolve/{T2_REV}/data/corpus-00000-of-00001-8afe7b7a7eca49e3.parquet",
    "t2/queries.parquet": f"https://huggingface.co/datasets/C-MTEB/T2Retrieval/resolve/{T2_REV}/data/queries-00000-of-00001-930bf3b805a80dd9.parquet",
    "t2/qrels.parquet": f"https://huggingface.co/datasets/C-MTEB/T2Retrieval-qrels/resolve/{T2_QRELS_REV}/data/dev-00000-of-00001-92ed0416056ff7e1.parquet",
    "crud/split_merged.json": f"https://raw.githubusercontent.com/IAAR-Shanghai/CRUD_RAG/{CRUD_REV}/data/crud_split/split_merged.json",
}


@dataclass
class Dataset:
    name: str
    corpus: dict[str, str]
    queries: list[dict]
    metadata: dict


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def acquire(name, root=DATA_ROOT):
    path = root / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        with (
            httpx.Client(timeout=120, follow_redirects=True) as client,
            client.stream("GET", SOURCES[name]) as response,
        ):
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for block in response.iter_bytes():
                    handle.write(block)
        temporary.replace(path)
    return path


def provenance(names, root):
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    return [{"file": name, "url": SOURCES[name], "sha256": digest(root / name)} for name in names]


def subset(corpus, queries, documents, seed):
    required = {key for query in queries for key in query["relevant"]}
    if required - corpus.keys():
        raise ValueError("Some labeled source documents are missing")
    # Every sampled positive is included, regardless of the target distractor pool size.
    others = sorted(corpus.keys() - required)
    random.Random(seed).shuffle(others)
    selected = required | set(others[: max(0, documents - len(required))])
    return {key: corpus[key] for key in sorted(selected)}


def t2(questions=20, documents=500, seed=42, root=DATA_ROOT, full=False):
    import pyarrow.parquet as pq

    names = ["t2/corpus.parquet", "t2/queries.parquet", "t2/qrels.parquet"]
    paths = [acquire(name, root) for name in names]

    def rows(path):
        # Bound Arrow/Python conversion temporaries instead of materializing the
        # complete corpus as both an Arrow table and a Python list of dicts.
        for batch in pq.ParquetFile(path).iter_batches(batch_size=2048, use_threads=False):
            yield from batch.to_pylist()

    corpus_rows = pq.ParquetFile(paths[0]).metadata.num_rows
    query_count = pq.ParquetFile(paths[1]).metadata.num_rows
    # Exact duplicates are one evidence item; preserve aliases in the manifest.
    corpus, aliases, seen = {}, {}, {}
    for row in rows(paths[0]):
        original, text = str(row["id"]), row["text"].strip()
        if not text:
            continue
        digest = sha(text)
        canonical = seen.setdefault(digest, original)
        aliases[original] = canonical
        corpus[canonical] = text
    relevant = {}
    for row in rows(paths[2]):
        pid = str(row["pid"])
        if row["score"] > 0 and pid in aliases:
            qid, canonical = str(row["qid"]), aliases[pid]
            values = relevant.setdefault(qid, {})
            values[canonical] = max(values.get(canonical, 0), int(row["score"]))
    candidates = [
        {
            "id": str(q["id"]),
            "question": q["text"],
            "relevant": relevant[str(q["id"])],
            "answers": [],
            "group": str(q["id"]),
        }
        for q in rows(paths[1])
        if str(q["id"]) in relevant and q["text"].strip()
    ]
    candidates.sort(key=lambda q: q["id"])
    if not full:
        random.Random(seed).shuffle(candidates)
    chosen = candidates if full else candidates[:questions]
    if not full and len(chosen) != questions:
        raise ValueError("Requested more questions than available")
    selected = corpus if full else subset(corpus, chosen, documents, seed + 1)
    return Dataset(
        "T2Retrieval-full" if full else "T2Retrieval-subset",
        selected,
        chosen,
        {
            "source": "https://huggingface.co/datasets/C-MTEB/T2Retrieval",
            "full": full,
            "revision": T2_REV,
            "qrels_revision": T2_QRELS_REV,
            "seed": seed,
            "original_corpus_rows": corpus_rows,
            "unique_corpus_texts": len(corpus),
            "original_queries": query_count,
            "corpus_size": len(selected),
            "query_count": len(chosen),
            "complete_sampled_positives": True,
            "distractors": "complete official corpus"
            if full
            else "uniform fixed sample from full corpus, seed+1",
            "official_leaderboard_comparable": False,
            "qrel_policy": "official positives, exact-text duplicate IDs canonicalized",
            "duplicate_aliases": {
                key: value for key, value in aliases.items() if key != value and value in selected
            },
            "files": provenance(names, root),
        },
    )


def crud(questions=20, documents=200, seed=42, root=DATA_ROOT):
    name = "crud/split_merged.json"
    data = json.loads(acquire(name, root).read_text(encoding="utf-8"))
    return crud_from_data(data, questions, documents, seed, provenance([name], root))


def crud_from_data(data, questions, documents, seed, files=None):
    tasks = ["questanswer_1doc", "questanswer_2docs", "questanswer_3docs"]
    corpus, pools = {}, {}
    for task in tasks:
        pool = []
        for row in data[task]:
            references = {}
            for key in ("news1", "news2", "news3"):
                text = row.get(key, "").strip()
                if text:
                    doc_id = sha(text)
                    corpus[doc_id] = text
                    references[doc_id] = 1
            if not references or not row["questions"].strip() or not row["answers"].strip():
                raise ValueError("Invalid CRUD question/source/reference")
            pool.append(
                {
                    "id": task + ":" + str(row["ID"]),
                    "question": row["questions"],
                    "answers": [row["answers"]],
                    "relevant": references,
                    "group": str(row["ID"]),
                    "task": task,
                }
            )
        pool.sort(key=lambda q: q["id"])
        random.Random(seed + tasks.index(task)).shuffle(pool)
        pools[task] = pool
    # Round-robin tasks, independent event IDs: do not select three correlated variants of one event.
    chosen, groups = [], set()
    while len(chosen) < questions:
        progress = False
        for task in tasks:
            while pools[task] and pools[task][-1]["group"] in groups:
                pools[task].pop()
            if pools[task] and len(chosen) < questions:
                query = pools[task].pop()
                chosen.append(query)
                groups.add(query["group"])
                progress = True
        if not progress:
            raise ValueError("Not enough independent CRUD events")
    selected = subset(corpus, chosen, documents, seed + 1)
    return Dataset(
        "CRUD-RAG-QA-associated-news-subset",
        selected,
        chosen,
        {
            "source": "https://github.com/IAAR-Shanghai/CRUD_RAG",
            "revision": CRUD_REV,
            "seed": seed,
            "tasks": {task: sum(q["task"] == task for q in chosen) for task in tasks},
            "source_pool_texts": len(corpus),
            "corpus_size": len(selected),
            "query_count": len(chosen),
            "complete_sampled_positives": True,
            "official_leaderboard_comparable": False,
            "qrel_policy": "news1/news2/news3 association inferred as expected sources, not exhaustive human qrels",
            "distractors": "other QA associated news, fixed random sample; not the full 80,000-news corpus",
            "indexed_fields": ["news1", "news2", "news3"],
            "excluded_fields": ["questions", "answers", "thoughts", "event"],
            "files": files or [],
        },
    )
