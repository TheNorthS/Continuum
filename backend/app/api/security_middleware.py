import hashlib
import ipaddress
import logging

from starlette.responses import JSONResponse

from app.common.context import actor, request_meta


class SecurityMiddleware:
    """Capture exact request bytes for signatures, enforce transport, and audit without body/header logs."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        import asyncio
        import uuid

        services = scope["app"].state.container
        path = scope.get("path", "")
        state = scope.setdefault("state", {})
        request_id = state.setdefault("request_id", uuid.uuid4().hex)
        client = (scope.get("client") or ("unknown", 0))[0]
        meta_token = request_meta.set(
            {"request_id": request_id, "client": services.cipher.blind(client, "ip")[:24]}
        )
        actor_token = actor.set({})
        status = 500
        public_health = path.endswith(("/health/live", "/health/ready"))

        async def wrapped_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        async def reject(code, message, status_code):
            await JSONResponse(
                {"error": {"code": code, "message": message, "request_id": request_id}},
                status_code=status_code,
            )(scope, receive, wrapped_send)

        try:
            headers = dict(scope.get("headers", []))
            if (
                self.settings.environment == "production"
                and not public_health
                and scope.get("scheme") != "https"
            ):
                try:
                    trusted = any(
                        ipaddress.ip_address(client) in ipaddress.ip_network(network)
                        for network in self.settings.trusted_proxy_hosts
                    )
                except ValueError:
                    trusted = False
                if not trusted or headers.get(b"x-forwarded-proto") != b"https":
                    return await reject("https_required", "生产环境要求 HTTPS", 400)
            limit = self.settings.max_upload_bytes + 65536
            chunks, size = [], 0
            while True:
                part = await receive()
                if part["type"] == "http.disconnect":
                    return
                body = part.get("body", b"")
                size += len(body)
                if size > limit:
                    return await reject("upload_too_large", "请求体超出限制", 413)
                chunks.append(body)
                if not part.get("more_body", False):
                    break
            body = b"".join(chunks)
            state["body_hash"] = hashlib.sha256(body).hexdigest()
            state["proof_target"] = scope.get("raw_path", path.encode()).decode("ascii") + (
                "?" + scope["query_string"].decode("ascii") if scope.get("query_string") else ""
            )
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, wrapped_send)
        finally:
            try:
                if path.startswith("/api/") and not public_health and scope["method"] != "OPTIONS":
                    route = getattr(scope.get("route"), "path", "unmatched")
                    resource = "/".join(str(v) for v in scope.get("path_params", {}).values())
                    await asyncio.to_thread(
                        services.security.audit,
                        "http." + scope["method"],
                        resource,
                        "ok" if status < 400 else "denied",
                        {"route": route, "status": status},
                        actor_id=state.get("security_user", {}).get("id"),
                    )
            except Exception:
                logging.getLogger(__name__).critical("audit_write_failed request_id=%s", request_id)
            finally:
                actor.reset(actor_token)
                request_meta.reset(meta_token)
