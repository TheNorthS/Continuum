import asyncio
import hashlib
import math
from contextlib import nullcontext
from urllib.parse import urlsplit, urlunsplit

from app.common.context import knowledge_scope
from app.common.errors import AppError
from app.infrastructure.http import post_json


def source_url(value):
    """Accept linkable public-web URLs without fetching or executing their contents."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and not (parsed.scheme == "https" and port == 443 or parsed.scheme == "http" and port == 80):
        host += f":{port}"
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


class WebSearch:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.gate = asyncio.Semaphore(settings.web_search_max_concurrency)

    @property
    def providers(self):
        return (
            ("tavily", "exa")
            if self.settings.web_search_provider == "hybrid"
            else (self.settings.web_search_provider,)
        )

    def status(self):
        return {
            "enabled": self.settings.web_search_enabled,
            "provider": self.settings.web_search_provider,
            "configured": {
                name: bool(getattr(self.settings, name + "_api_key").get_secret_value().strip())
                for name in self.providers
            },
            "max_results": self.settings.web_search_max_results,
            "timeout": self.settings.web_search_timeout,
            "failure_mode": self.settings.web_search_failure_mode,
            "scope_policy": "unscoped_only",
        }

    async def search(self, query):
        # Defense in depth: callers cannot bypass a selected knowledge-base scope.
        if not self.settings.web_search_enabled or knowledge_scope.get() is not None:
            return []
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise AppError("invalid_web_query", "联网查询须为 1–4000 字符的非空文本", 422)
        context = self.tracing.span("web_search") if getattr(self, "tracing", None) else nullcontext()
        async with context:
            tasks = [
                asyncio.create_task(self._provider_search(name, query.strip())) for name in self.providers
            ]
            try:
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            successes, failed = [], []
            for name, value in zip(self.providers, outcomes, strict=True):
                if isinstance(value, asyncio.CancelledError):
                    raise value
                if isinstance(value, BaseException):
                    if not isinstance(value, AppError):
                        raise value
                    failed.append(name)
                else:
                    successes.append((name, value))
            if failed and (
                self.settings.web_search_failure_mode == "error" or not any(rows for _, rows in successes)
            ):
                raise AppError("web_search_unavailable", "联网检索服务暂不可用，请检查配置或稍后重试", 503)
            return self._merge(successes, failed)

    async def _provider_search(self, provider, query):
        tracing = getattr(self, "tracing", None)
        context = (
            tracing.span("web_search." + provider, metadata={"provider": provider})
            if tracing
            else nullcontext()
        )
        async with context:
            try:
                # Total provider budget includes semaphore queueing and HTTP retries.
                async with asyncio.timeout(self.settings.web_search_timeout):
                    async with self.gate:
                        key = getattr(self.settings, provider + "_api_key").get_secret_value()
                        if provider == "tavily":
                            data = await post_json(
                                self.client,
                                "https://api.tavily.com/search",
                                headers={"Authorization": "Bearer " + key},
                                json={
                                    "query": query,
                                    "max_results": self.settings.web_search_max_results,
                                    "search_depth": self.settings.tavily_search_depth,
                                    "include_answer": False,
                                    "include_raw_content": False,
                                },
                                timeout=self.settings.web_search_timeout,
                            )
                        else:
                            data = await post_json(
                                self.client,
                                "https://api.exa.ai/search",
                                headers={"x-api-key": key},
                                json={
                                    "query": query,
                                    "type": "auto",
                                    "numResults": self.settings.web_search_max_results,
                                    "contents": {"text": True},
                                },
                                timeout=self.settings.web_search_timeout,
                            )
                        return self._normalize(data, provider)
            except (AppError, TimeoutError) as exc:
                raise AppError(
                    "web_search_unavailable", "联网检索服务暂不可用，请检查配置或稍后重试", 503
                ) from exc

    def _normalize(self, data, provider):
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise AppError("web_search_format", "联网检索响应格式不符合要求", 502)
        sources, seen = [], set()
        for item in data["results"]:
            if not isinstance(item, dict):
                raise AppError("web_search_format", "联网检索响应格式不符合要求", 502)
            url = source_url(item.get("url"))
            content = item.get("content" if provider == "tavily" else "text")
            if provider == "exa" and (not isinstance(content, str) or not content.strip()):
                highlights = item.get("highlights")
                if isinstance(highlights, list) and all(isinstance(h, str) for h in highlights):
                    content = "\n".join(highlights)
            if not url or url in seen or not isinstance(content, str) or not content.strip():
                continue
            score = item.get("score")
            if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
                raise AppError("web_search_format", "联网检索响应格式不符合要求", 502)
            seen.add(url)
            rank = len(sources) + 1
            title = item.get("title")
            sources.append(
                {
                    "id": "web-" + hashlib.sha256(url.encode()).hexdigest(),
                    "text": content.strip()[: self.settings.web_search_content_max_chars],
                    "score": score if score is not None else 1 / (60 + rank),
                    "metadata": {
                        "source": url,
                        "title": title[:500] if isinstance(title, str) and title.strip() else "网络来源",
                        "category": "web",
                        "web_provider": provider,
                        "web_rank": rank,
                        "score_type": "provider" if score is not None else "rank",
                        "provider_score": score,
                    },
                }
            )
            if len(sources) == self.settings.web_search_max_results:
                break
        return sources

    def _merge(self, channels, failed):
        merged, scores = {}, {}
        multi = sum(bool(rows) for _, rows in channels) > 1
        for provider, rows in channels:
            for rank, row in enumerate(rows, 1):
                cid = row["id"]
                if cid not in merged:
                    merged[cid] = {
                        **row,
                        "metadata": {**row["metadata"], "web_providers": [], "web_ranks": {}},
                    }
                metadata = merged[cid]["metadata"]
                metadata["web_providers"].append(provider)
                metadata["web_ranks"][provider] = {"rank": rank, "score": row["metadata"]["provider_score"]}
                if multi:
                    scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
        if multi:
            results = [
                {**merged[cid], "score": score}
                for cid, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
            ]
            for row in results:
                row["metadata"]["score_type"] = "rrf"
        else:
            results = list(merged.values())
        for row in results:
            row["metadata"]["web_status"] = "partial" if failed else "complete"
            if failed:
                row["metadata"]["web_failed_providers"] = list(failed)
        return results[: self.settings.web_search_max_results]
