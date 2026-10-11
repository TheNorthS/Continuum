from contextlib import nullcontext

from app.infrastructure.http import post_json


class WebSearch:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def search(self, query):
        context = self.tracing.span("web_search") if getattr(self, "tracing", None) else nullcontext()
        async with context:
            return await self._search(query)

    async def _search(self, query):
        if not self.settings.web_search_enabled:
            return []
        data = await post_json(
            self.client,
            "https://api.tavily.com/search",
            json={
                "api_key": self.settings.tavily_api_key.get_secret_value(),
                "query": query,
                "max_results": 3,
                "include_answer": False,
            },
        )
        return [
            {
                "id": f"web-{index}",
                "text": str(item.get("content", ""))[:5000],
                "score": item.get("score", 0),
                "metadata": {
                    "source": item.get("url", ""),
                    "title": item.get("title", "网络来源"),
                    "category": "web",
                },
            }
            for index, item in enumerate(data.get("results", []))
            if item.get("content")
        ]
