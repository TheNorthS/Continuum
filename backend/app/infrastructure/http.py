import asyncio

import httpx

from app.common.errors import AppError


async def post_json(client: httpx.AsyncClient, url: str, **kwargs):
    for attempt in range(3):
        try:
            response = await client.post(url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.3 * 2**attempt)
                    continue
            response.raise_for_status()
            return response.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            if attempt == 2:
                raise AppError("upstream_unavailable", "外部服务连接失败，请稍后重试", 503) from exc
            await asyncio.sleep(0.3 * 2**attempt)
        except (httpx.HTTPStatusError, ValueError) as exc:
            # Do not expose provider response bodies, URLs or credentials.
            raise AppError("upstream_error", "外部服务返回错误，请检查服务配置", 502) from exc
