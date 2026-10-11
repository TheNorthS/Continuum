"""All CRUD QA questions with an explicitly selected official corpus-file scope."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.evaluation.datasets import CRUD_REV, DATA_ROOT, Dataset, acquire, provenance, sha


def full_crud(root=DATA_ROOT, *, include_auxiliary=False):
    name = "crud/split_merged.json"
    data = json.loads(acquire(name, root).read_text(encoding="utf-8"))
    folder = root / "crud/full-corpus"
    folder.mkdir(parents=True, exist_ok=True)
    files = [f"documents_dup_part_{part}_part_{section}" for part in range(1, 16) for section in range(1, 4)]
    if include_auxiliary:
        files += [f"documents_hallu.txt_part_{part}" for part in range(1, 4)]

    def download(filename):
        path = folder / filename
        url = (
            f"https://raw.githubusercontent.com/IAAR-Shanghai/CRUD_RAG/{CRUD_REV}/data/80000_docs/{filename}"
        )
        if not path.exists():
            audited = root / "crud/corpus-audit" / filename
            if audited.exists():
                content = audited.read_bytes()
            else:
                with httpx.Client(timeout=120, follow_redirects=True) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    content = response.content
            temporary = path.with_suffix(".part")
            temporary.write_bytes(content)
            temporary.replace(path)
        return {
            "file": filename,
            "url": url,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    with ThreadPoolExecutor(max_workers=3) as pool:
        evidence = list(pool.map(download, files))
    # This application's explicit parser uses one physical line as an input
    # record; the upstream SimpleDirectoryReader instead loads whole files.
    # Preserve literal escaped newlines and never inject generated QA answers.
    corpus, line_count, duplicate_count = {}, 0, 0
    for filename in files:
        for line in (folder / filename).read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            line_count += 1
            key = sha(text)
            if key in corpus:
                duplicate_count += 1
            corpus[key] = text
    queries = []
    task_counts = {}
    for task in ("questanswer_1doc", "questanswer_2docs", "questanswer_3docs"):
        task_counts[task] = len(data[task])
        for row in data[task]:
            # No exhaustive qrels are provided for the 80k full corpus. Do not
            # pretend the news snippets' hashes identify full corpus records.
            queries.append(
                {
                    "id": task + ":" + str(row["ID"]),
                    "question": row["questions"],
                    "answers": [row["answers"]],
                    "relevant": {},
                    "group": str(row["ID"]),
                    "task": task,
                    "reference_sources": [row[key] for key in ("news1", "news2", "news3") if row.get(key)],
                }
            )
    if len({q["id"] for q in queries}) != len(queries):
        raise ValueError("Duplicate full CRUD question IDs")
    return Dataset(
        "CRUD-RAG-QA-official-directory" if include_auxiliary else "CRUD-RAG-QA-full-corpus",
        corpus,
        queries,
        {
            "source": "https://github.com/IAAR-Shanghai/CRUD_RAG",
            "revision": CRUD_REV,
            "full": True,
            "qa_full": True,
            "corpus_scope": "official_directory_48_files" if include_auxiliary else "documents_dup_45_files",
            "scope": "all three QA tasks in the official paper split; excludes summary/continuation/hallucination-edit tasks",
            "tasks": task_counts,
            "query_count": len(queries),
            "corpus_size": len(corpus),
            "physical_news_records": line_count,
            "exact_duplicate_records": duplicate_count,
            "corpus_partition_count": len(files),
            "corpus_parse": "nonempty physical lines; exact-text deduplication",
            "qrel_policy": "unavailable for full news corpus; do not publish recall/MRR/nDCG for this dataset",
            "distractors": "all 48 files in official 80000_docs directory"
            if include_auxiliary
            else "45 documents_dup partitions only; excludes 3 documents_hallu auxiliary files; filename alone does not establish fabricated content",
            "official_leaderboard_comparable": False,
            "files": provenance([name], root) + evidence,
        },
    )
