import asyncio

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.context import actor
from app.common.errors import AppError

bearer = HTTPBearer(auto_error=False)


def container(request: Request):
    return request.app.state.container


async def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    services=Depends(container),
):
    if credentials is None:
        raise AppError("unauthorized", "请先登录", 401)
    if getattr(request.state, "security_user", None):
        return request.state.security_user
    user = await asyncio.to_thread(
        services.security.authenticate,
        credentials.credentials,
        request.headers.get("X-Request-Proof", ""),
        request.method,
        request.state.proof_target,
        request.state.body_hash,
    )
    request.state.security_user = user
    actor.set(user)
    if user["must_change_password"] and request.url.path not in {
        "/api/v1/auth/me",
        "/api/v1/auth/password",
        "/api/v1/auth/logout",
    }:
        raise AppError("password_change_required", "请先修改初始密码", 403)
    return user


def require_permission(permission):
    async def guard(user=Depends(current_user)):
        if permission not in user["permissions"]:
            raise AppError("forbidden", "当前角色无此操作权限", 403)
        return user

    return guard


def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise AppError("forbidden", "此操作需要管理员权限", 403)
    return user
