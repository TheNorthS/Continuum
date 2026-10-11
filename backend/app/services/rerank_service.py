"""Pairwise model reranking, with bounded work and no document replacement by providers."""

import asyncio
import hashlib
import json
import logging
import math
from contextlib import nullcontext
from pathlib import Path

from app.common.errors import AppError
from app.infrastructure.http import post_json


class Reranker:
    def __init__(self, settings, client, cache, tracing=None):
        self.settings, self.client, self.cache, self.tracing = settings, client, cache, tracing
        self._model = None
        self._gate = asyncio.Semaphore(settings.rerank_max_concurrency)
        self._local_gate = asyncio.Semaphore(1)
        self._jobs = set()
        self._last_error = None

    @property
    def enabled(self):
        return self.settings.rerank_provider != "none"

    @property
    def model_name(self):
        value = self.settings.rerank_model
        return Path(value).name if Path(value).is_absolute() else value

    def status(self):
        return {
            "enabled": self.enabled,
            "provider": self.settings.rerank_provider,
            "model": self.model_name if self.enabled else None,
            "state": "disabled"
            if not self.enabled
            else "error"
            if self._last_error
            else "ready"
            if self._model is not None
            else "configured"
            if self.settings.rerank_provider == "api"
            else "not_loaded",
            "candidates": self.settings.rerank_candidates,
            "max_length": self.settings.rerank_max_length,
            "min_score": self.settings.rerank_min_score,
            "failure_mode": self.settings.rerank_failure_mode,
            "last_error": self._last_error,
        }

    def _predict_local(self, query, documents):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            from torch.nn import Sigmoid

            self._model = CrossEncoder(
                self.settings.rerank_model,
                device=self.settings.rerank_device,
                max_length=self.settings.rerank_max_length,
                activation_fn=Sigmoid(),
                trust_remote_code=False,
                local_files_only=self.settings.rerank_local_files_only,
                revision=self.settings.rerank_revision or None,
                model_kwargs={"use_safetensors": True},
            )
        values = self._model.predict(
            [(query, text) for text in documents],
            batch_size=self.settings.rerank_batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return values.tolist()

    async def _local(self, query, documents):
        await self._local_gate.acquire()
        # Cancelling a Python await cannot stop native model inference. Retain the permit
        # until the actual worker completes, so timeouts cannot spawn parallel model loads.
        job = asyncio.create_task(asyncio.to_thread(self._predict_local, query, documents))
        self._jobs.add(job)

        def complete(task):
            self._jobs.discard(task)
            self._local_gate.release()
            if not task.cancelled():
                task.exception()  # Consume a detached worker's error without logging input.

        job.add_done_callback(complete)
        return await asyncio.shield(job)

    @staticmethod
    def validate_scores(values, count):
        if (
            not isinstance(values, list)
            or len(values) != count
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                for value in values
            )
        ):
            raise AppError("rerank_format", "精排响应分数无效或数量不完整", 502)
        return values

    async def _api(self, query, documents):
        body = {
            "model": self.settings.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        }
        # Model Studio qwen3-rerank uses a flat payload without return_documents.
        if self.settings.rerank_api_format == "standard":
            body["return_documents"] = False
        data = await post_json(
            self.client,
            self.settings.rerank_api_url,
            headers={"Authorization": "Bearer " + self.settings.rerank_api_key.get_secret_value()}
            if self.settings.rerank_api_key.get_secret_value()
            else {},
            json=body,
            timeout=self.settings.rerank_timeout,
        )
        try:
            rows = data["results"]
            if not isinstance(rows, list) or len(rows) != len(documents):
                raise ValueError("incomplete response")
            scores = [None] * len(documents)
            for row in rows:
                index = row["index"]
                if type(index) is not int or not 0 <= index < len(documents) or scores[index] is not None:
                    raise ValueError("invalid index")
                scores[index] = row["relevance_score"]
            return self.validate_scores(scores, len(documents))
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("rerank_format", "精排响应索引无效或结果不完整", 502) from exc

    async def rank(self, query, sources):
        if not self.enabled or not sources:
            return sources
        if len(sources) > max(self.settings.rerank_candidates, 12):
            raise AppError("rerank_limit", "精排候选数量超过配置上限", 413)
        text = query[: self.settings.rerank_query_max_chars]
        documents = [s["text"][: self.settings.rerank_document_max_chars] for s in sources]
        signature = json.dumps(
            [
                "v1",
                self.settings.rerank_provider,
                self.settings.rerank_model,
                self.settings.rerank_revision,
                self.settings.rerank_api_url,
                self.settings.rerank_api_format,
                self.settings.rerank_max_length,
                text,
                [(s["id"], doc) for s, doc in zip(sources, documents, strict=True)],
            ],
            ensure_ascii=False,
        )
        key = "rag-e:secure:rerank:" + hashlib.sha256(signature.encode()).hexdigest()

        async def load():
            async with self._gate:
                values = (
                    await self._api(text, documents)
                    if self.settings.rerank_provider == "api"
                    else await self._local(text, documents)
                )
                return self.validate_scores(values, len(sources))

        context = (
            self.tracing.span(
                "rerank",
                model=self.model_name,
                metadata={"provider": self.settings.rerank_provider, "candidates": len(sources)},
            )
            if self.tracing
            else nullcontext()
        )
        try:
            async with context:
                async with asyncio.timeout(self.settings.rerank_timeout):
                    scores = self.validate_scores(await self.cache.get_or_load(key, load), len(sources))
            self._last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = "rerank_timeout" if isinstance(exc, TimeoutError) else "rerank_unavailable"
            logging.getLogger(__name__).warning(self._last_error)
            if self.settings.rerank_failure_mode == "rrf":
                return [
                    {**s, "metadata": {**s.get("metadata", {}), "rerank_status": "fallback"}} for s in sources
                ]
            raise AppError(self._last_error, "模型精排未完成，请检查模型配置或稍后重试", 503) from exc
        threshold = self.settings.rerank_min_score
        return [
            {
                **sources[index],
                "rerank_score": float(scores[index]),
                "metadata": {
                    **sources[index].get("metadata", {}),
                    "rerank_status": "ranked",
                    "rerank_model": self.model_name,
                },
            }
            for index in sorted(range(len(sources)), key=lambda i: (-scores[i], i))
            if threshold is None or scores[index] >= threshold
        ]

    async def close(self):
        # Detached native inference must finish before releasing model resources.
        if self._jobs:
            await asyncio.gather(*(asyncio.shield(job) for job in list(self._jobs)), return_exceptions=True)
        self._model = None
