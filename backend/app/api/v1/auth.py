import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.schemas import Credentials, LoginCredentials
from app.common.errors import AppError
from app.dependencies import container, current_user

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login")
async def login(payload: LoginCredentials, request: Request, services=Depends(container)):
    return await asyncio.to_thread(
        services.security.login,
        payload.username,
        payload.password,
        payload.challenge,
        request.headers.get("X-Request-Proof", ""),
        request.method,
        request.state.proof_target,
        request.state.body_hash,
    )


@router.get("/challenge")
async def challenge(services=Depends(container)):
    return await asyncio.to_thread(services.security.challenge)


@router.post("/register", status_code=201)
async def register(payload: Credentials, request: Request, services=Depends(container)):
    if not services.settings.allow_registration:
        raise AppError("registration_disabled", "注册未开放，请联系管理员", 403)
    # Registration has the same single-use proof/challenge boundary as login.
    await asyncio.to_thread(
        services.security.verify_anonymous,
        payload.challenge,
        request.headers.get("X-Request-Proof", ""),
        request.method,
        request.state.proof_target,
        request.state.body_hash,
    )
    return await asyncio.to_thread(
        services.security.create_user, payload.username, payload.password, registration=True
    )


@router.get("/me")
async def me(user=Depends(current_user)):
    return user


@router.get("/configuration")
async def configuration(services=Depends(container)):
    return {"registration_enabled": services.settings.allow_registration, "request_proof": "rage-proof-v1"}


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=200)


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


@router.post("/refresh")
async def refresh(payload: RefreshRequest, request: Request, services=Depends(container)):
    return await asyncio.to_thread(
        services.security.refresh,
        payload.refresh_token,
        request.headers.get("X-Request-Proof", ""),
        request.method,
        request.state.proof_target,
        request.state.body_hash,
    )


@router.post("/logout", status_code=204)
async def logout(user=Depends(current_user), services=Depends(container)):
    await asyncio.to_thread(services.security.revoke, user["session_id"])


@router.post("/password", status_code=204)
async def password(payload: PasswordChange, user=Depends(current_user), services=Depends(container)):
    await asyncio.to_thread(services.security.change_password, payload.old_password, payload.new_password)


@router.get("/sessions")
async def sessions(user=Depends(current_user), services=Depends(container)):
    return await asyncio.to_thread(services.security.session_list)


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke(session_id: str, user=Depends(current_user), services=Depends(container)):
    await asyncio.to_thread(services.security.revoke, session_id)
