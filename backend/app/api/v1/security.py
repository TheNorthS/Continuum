import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.dependencies import container, current_user, require_permission

router = APIRouter(tags=["账号、角色、资源授权与审计"], dependencies=[Depends(current_user)])


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=128)
    role: str = Field(default="user", pattern=r"^[a-zA-Z0-9_-]{1,16}$")


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,16}$")
    active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=128)


class RoleCreate(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,16}$")
    permissions: list[str] = Field(max_length=30)


class BaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class GrantRequest(BaseModel):
    subject_type: Literal["user", "role"]
    subject_id: str = Field(min_length=1, max_length=64)
    access: Literal["read", "write", "manage"] | None


@router.get("/admin/users", dependencies=[Depends(require_permission("users.manage"))])
async def users(services=Depends(container)):
    return await asyncio.to_thread(services.security.users)


@router.post("/admin/users", status_code=201, dependencies=[Depends(require_permission("users.manage"))])
async def create_user(payload: UserCreate, services=Depends(container)):
    return await asyncio.to_thread(services.security.create_user, **payload.model_dump())


@router.patch("/admin/users/{user_id}", dependencies=[Depends(require_permission("users.manage"))])
async def update_user(user_id: str, payload: UserUpdate, services=Depends(container)):
    return await asyncio.to_thread(services.security.update_user, user_id, **payload.model_dump())


@router.post(
    "/admin/users/{user_id}/revoke",
    status_code=204,
    dependencies=[Depends(require_permission("security.manage"))],
)
async def revoke_user(user_id: str, services=Depends(container)):
    await asyncio.to_thread(services.security.revoke, user_id=user_id)


@router.get("/admin/roles", dependencies=[Depends(require_permission("roles.manage"))])
async def roles(services=Depends(container)):
    return await asyncio.to_thread(services.security.roles)


@router.post("/admin/roles", status_code=204, dependencies=[Depends(require_permission("roles.manage"))])
async def save_role(payload: RoleCreate, services=Depends(container)):
    await asyncio.to_thread(services.security.save_role, payload.id, payload.permissions)


@router.delete(
    "/admin/roles/{role_id}", status_code=204, dependencies=[Depends(require_permission("roles.manage"))]
)
async def delete_role(role_id: str, services=Depends(container)):
    await asyncio.to_thread(services.security.delete_role, role_id)


@router.get("/knowledge/bases")
async def bases(services=Depends(container)):
    return await asyncio.to_thread(services.security.bases)


@router.post("/knowledge/bases", status_code=201)
async def create_base(payload: BaseCreate, services=Depends(container)):
    return await asyncio.to_thread(services.security.create_base, payload.name)


@router.get("/knowledge/bases/{base_id}/grants")
async def grants(base_id: str, services=Depends(container)):
    return await asyncio.to_thread(services.security.grants, base_id)


@router.put("/knowledge/bases/{base_id}/grants", status_code=204)
async def grant(base_id: str, payload: GrantRequest, services=Depends(container)):
    await asyncio.to_thread(services.security.grant, base_id, **payload.model_dump())


@router.get("/admin/audit", dependencies=[Depends(require_permission("audit.read"))])
async def audit(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    services=Depends(container),
):
    return await asyncio.to_thread(services.security.audit_list, after, limit)


@router.get("/admin/audit/verify", dependencies=[Depends(require_permission("audit.read"))])
async def verify(services=Depends(container)):
    return await asyncio.to_thread(services.security.verify_audit)
