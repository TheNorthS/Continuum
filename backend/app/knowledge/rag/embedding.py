import asyncio
import hashlib
import math
import re
from contextlib import nullcontext

from app.common.errors import AppError
from app.infrastructure.http import post_json


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    result = []
    for word in words:
        if re.search(r"[\u4e00-\u9fff]", word):
            result.extend(word)
            result.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return result


def normalize(vector):
    norm = math.sqrt(sum(v * v for v in vector)) or 1
    return [v / norm for v in vector]


class Embedder:
    def __init__(self, settings, client):
        self.settings = settings
        self.client = client
        self._model = None
        self._lock = asyncio.Lock()
        self.tracing = None

    def _local_encode(self, texts):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.settings.embedding_model, device=self.settings.embedding_device
            )
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        context = (
            self.tracing.span(
                "embedding",
                kind="embedding",
                model=self.settings.embedding_model,
                metadata={"batch_size": len(texts)},
            )
            if self.tracing
            else nullcontext()
        )
        async with context:
            return await self._embed(texts)

    def _hash_encode(self, texts):
        vectors = []
        for text in texts:
            vector = [0.0] * self.settings.embedding_dimension
            for token in tokenize(text):
                hashed = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(hashed[:4], "big") % len(vector)
                vector[index] += 1 if hashed[4] % 2 else -1
            vectors.append(normalize(vector))
        return vectors

    async def _embed(self, texts):
        if not texts:
            return []
        result = []
        for start in range(0, len(texts), 32):
            batch = texts[start : start + 32]
            if self.settings.embedding_provider == "hash":
                vectors = await asyncio.to_thread(self._hash_encode, batch)
            elif self.settings.embedding_provider == "huggingface":
                async with self._lock:
                    vectors = await asyncio.to_thread(self._local_encode, batch)
            else:
                data = await post_json(
                    self.client,
                    self.settings.embedding_base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": "Bearer " + self.settings.embedding_api_key.get_secret_value()},
                    json={"model": self.settings.embedding_model, "input": batch},
                )
                try:
                    vectors = [
                        item["embedding"] for item in sorted(data["data"], key=lambda item: item["index"])
                    ]
                except (KeyError, TypeError) as exc:
                    raise AppError("embedding_format", "Embedding 响应格式错误", 502) from exc
            if len(vectors) != len(batch) or any(
                len(v) != self.settings.embedding_dimension
                or not all(isinstance(x, (float, int)) and math.isfinite(x) for x in v)
                for v in vectors
            ):
                raise AppError("embedding_dimension", "Embedding 数量、维度或数值与配置不一致", 502)
            result.extend(normalize(v) for v in vectors)
        return result
