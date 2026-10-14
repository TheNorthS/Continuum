import math
import threading
from collections import Counter, defaultdict

from app.common.errors import AppError
from app.knowledge.rag.embedding import tokenize


class LocalStore:
    """Small-corpus development backend. Durable vectors come from the SQL repository."""

    def __init__(self, repository):
        self.repository = repository
        self._revision = None
        self._snapshot = None
        self._lock = threading.Lock()

    def initialize(self):
        pass

    def upsert(self, chunks):
        pass  # SQL commit is the authoritative local index write.

    def delete(self, document_id):
        pass  # SQL cascade owns local index deletion.

    def healthy(self):
        self.repository.db.healthy()

    def close(self):
        pass

    def search(self, query, vector, top_k):
        revision = self.repository.revision()
        with self._lock:
            if revision != self._revision:
                # Don't publish data loaded across a concurrent content/scope change
                # under an earlier revision. Fail rather than cache a mismatched snapshot.
                for _ in range(3):
                    chunks = self.repository.ready_chunks()
                    current = self.repository.revision()
                    if current == revision:
                        break
                    revision = current
                else:
                    raise AppError("retrieval_changed", "资料正在更新，请稍后重试", 503)
                frequencies = [Counter(tokenize(c["text"])) for c in chunks]
                document_frequency = Counter(token for freq in frequencies for token in freq)
                lengths = [sum(freq.values()) for freq in frequencies]
                average = sum(lengths) / len(lengths) if lengths else 1
                self._snapshot = (chunks, frequencies, document_frequency, lengths, average or 1)
                self._revision = revision
            chunks, frequencies, document_frequency, lengths, average = self._snapshot
        if not chunks:
            return []
        query_tokens = set(tokenize(query))
        dense, sparse = [], []
        for i, (chunk, freq) in enumerate(zip(chunks, frequencies, strict=True)):
            if len(chunk["embedding"]) != len(vector):
                raise AppError("index_dimension", "索引维度已变更，请重建索引", 503)
            cosine = sum(a * b for a, b in zip(vector, chunk["embedding"], strict=True))
            score = 0
            for token in query_tokens:
                tf = freq[token]
                idf = math.log(
                    1 + (len(chunks) - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
                )
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[i] / average))
            if cosine > 0.05:
                dense.append((i, cosine))
            if score > 0:
                sparse.append((i, score))
        fused = defaultdict(float)
        for ranking in (dense, sparse):
            for rank, (index, _) in enumerate(
                sorted(ranking, key=lambda x: x[1], reverse=True)[: top_k * 4], 1
            ):
                fused[index] += 1 / (60 + rank)
        return [
            {"id": chunks[i]["id"], "text": chunks[i]["text"], "metadata": chunks[i]["meta"], "score": score}
            for i, score in sorted(fused.items(), key=lambda item: item[1], reverse=True)[:top_k]
        ]
