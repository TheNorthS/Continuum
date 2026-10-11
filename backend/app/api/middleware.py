import json
import logging
import time
import uuid
from collections import OrderedDict

from fastapi import HTTPException
from starlette.responses import JSONResponse


class OperationalMiddleware:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.buckets = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = scope.setdefault("state", {}).setdefault("request_id", uuid.uuid4().hex)
        path = scope.get("path", "")
        status = 500

        async def send_headers(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        async def reject(code, message, status_code):
            response = JSONResponse(
                {"error": {"code": code, "message": message, "request_id": request_id}},
                status_code=status_code,
            )
            await response(scope, receive, send_headers)

        headers = dict(scope.get("headers", []))
        limit = self.settings.max_upload_bytes + 65536
        try:
            if int(headers.get(b"content-length", b"0")) > limit:
                return await reject("upload_too_large", "请求体超出大小限制", 413)
        except ValueError:
            return await reject("invalid_request", "Content-Length 无效", 400)
        if not path.endswith(("/health/live", "/health/ready")) and scope["method"] != "OPTIONS":
            client = (scope.get("client") or ("unknown", 0))[0]
            key = (client, "login" if path.endswith("/auth/login") else "api")
            maximum = (
                min(10, self.settings.rate_limit_per_minute)
                if key[1] == "login"
                else self.settings.rate_limit_per_minute
            )
            window, count = self.buckets.get(key, (started, 0))
            if started - window > 60:
                window, count = started, 0
            self.buckets[key] = (window, count + 1)
            self.buckets.move_to_end(key)
            while len(self.buckets) > 4096:
                self.buckets.popitem(last=False)
            if count >= maximum:
                return await reject("rate_limited", "请求过于频繁，请稍后重试", 429)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "请求体超出大小限制")
            return message

        try:
            services = getattr(scope.get("app").state, "container", None)
            if services:
                async with services.tracing.span(
                    "request", trace_id=request_id, metadata={"method": scope["method"]}
                ) as observation:
                    await self.app(scope, bounded_receive, send_headers)
                    observation.update(
                        metadata={"route": getattr(scope.get("route"), "path", "unmatched"), "status": status}
                    )
            else:
                await self.app(scope, bounded_receive, send_headers)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            container = getattr(scope.get("app").state, "container", None)
            if container:
                if (
                    not path.endswith(("/health/live", "/health/ready", "/metrics"))
                    and "/admin/observability" not in path
                ):
                    container.monitoring.record("http", time.monotonic() - started, status < 500)
                container.requests.labels(scope["method"], route, str(status)).inc()
                container.latency.labels(route).observe(time.monotonic() - started)
            logging.getLogger("rag_e.access").info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": scope["method"],
                        "route": route,
                        "status": status,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            )
