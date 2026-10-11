import hashlib
import hmac
import json
import re
import secrets
import time
import uuid

import jwt
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.common.context import actor, request_meta
from app.common.encryption import b64
from app.common.errors import AppError
from app.common.redaction import redact
from app.models.entities import (
    AuditEvent,
    AuthSession,
    Grant,
    KnowledgeBase,
    Nonce,
    Role,
    SecurityState,
    UsedRefresh,
    User,
)
from app.utils.security import hash_password, verify_password

PERMISSIONS = {
    "chat.use": "问答与本人会话",
    "knowledge.read": "读取已授权知识库",
    "knowledge.write": "编辑已授权知识库",
    "knowledge.create": "创建私有知识库",
    "knowledge.share": "管理知识库授权",
    "graph.read": "查看图谱",
    "graph.write": "重建图谱",
    "monitor.read": "查看系统监控",
    "users.manage": "管理账号",
    "roles.manage": "管理角色权限",
    "audit.read": "查询审计日志",
    "security.manage": "管理安全会话",
}
DEFAULT_USER = [
    "chat.use",
    "knowledge.read",
    "knowledge.create",
    "knowledge.write",
    "knowledge.share",
    "graph.read",
    "graph.write",
]
ACCESS = {"read": 1, "write": 2, "manage": 3}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public_user(user, permissions):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "active": user.active,
        "must_change_password": user.must_change_password,
        "permissions": permissions,
    }


class SecurityService:
    def __init__(self, settings, db, cipher):
        self.settings, self.db, self.cipher = settings, db, cipher

    def lock(self, s, name="policy"):
        s.execute(
            update(SecurityState).where(SecurityState.id == name).values(version=SecurityState.version + 1)
        )
        return s.get(SecurityState, name, populate_existing=True)

    def bootstrap(self, admin_id):
        with self.db.session.begin() as s:
            self.lock(s)
            for name, permissions in [("admin", list(PERMISSIONS)), ("user", DEFAULT_USER)]:
                if not s.get(Role, name):
                    s.add(Role(id=name, permissions=permissions, builtin=True))
            legacy = s.get(KnowledgeBase, "legacy")
            if legacy and not legacy.owner_id:
                legacy.owner_id = admin_id
                # Preserve the earlier shared-read knowledge base, but new bases are private by default.
                s.merge(Grant(base_id="legacy", subject_type="role", subject_id="user", access="read"))

    def permissions(self, s, role_id):
        role = s.get(Role, role_id)
        return role.permissions if role else []

    def require(self, permission):
        current = actor.get()
        if current is None:
            return
        if permission not in current.get("permissions", []):
            raise AppError("forbidden", "当前角色无此操作权限", 403)

    def visible_bases(self, s, access="read"):
        current = actor.get()
        if current is None:
            return list(s.scalars(select(KnowledgeBase.id)))
        live_user = s.get(User, current.get("id", ""), populate_existing=True) if current else None
        if not live_user or not live_user.active:
            return []
        permissions = self.permissions(s, live_user.role)
        if "knowledge.read" not in permissions:
            return []
        if live_user.role == "admin":
            return list(s.scalars(select(KnowledgeBase.id)))
        ids = set(s.scalars(select(KnowledgeBase.id).where(KnowledgeBase.owner_id == current["id"])))
        for grant in s.scalars(
            select(Grant).where(
                (Grant.subject_type == "user") & (Grant.subject_id == current["id"])
                | (Grant.subject_type == "role") & (Grant.subject_id == live_user.role)
            )
        ):
            if ACCESS[grant.access] >= ACCESS[access]:
                ids.add(grant.base_id)
        return sorted(ids)

    def require_base(self, s, base_id, access="read"):
        if base_id not in self.visible_bases(s, access):
            raise AppError("not_found", "知识库不存在或无访问权限", 404)
        base = s.get(KnowledgeBase, base_id)
        if not base:
            raise AppError("not_found", "知识库不存在", 404)
        return base

    def audit(self, action, resource="", outcome="ok", details=None, *, session=None, actor_id=None):
        def append(s):
            head = self.lock(s, "audit")
            current = actor.get() or {}
            meta = request_meta.get() or {}
            value = {
                "id": head.version,
                "at": time.time(),
                "actor_id": actor_id or current.get("id"),
                "action": action[:100],
                "resource": redact(resource[:200]),
                "outcome": outcome,
                "request_id": meta.get("request_id", "operator"),
                "details": redact({**(details or {}), "client": meta.get("client", "local")}),
                "previous": head.value,
            }
            signature = self.cipher.sign_audit(
                json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
            s.add(AuditEvent(**value, signature=signature))
            head.value = signature

        if session is not None:
            append(session)
        else:
            with self.db.session.begin() as s:
                append(s)

    def audit_list(self, after=0, limit=100):
        self.require("audit.read")
        with self.db.session() as s:
            return [
                {c.key: getattr(row, c.key) for c in AuditEvent.__table__.columns}
                for row in s.scalars(
                    select(AuditEvent).where(AuditEvent.id > after).order_by(AuditEvent.id).limit(limit)
                )
            ]

    def verify_audit(self):
        self.require("audit.read")
        with self.db.session.begin() as s:
            s.execute(
                update(SecurityState).where(SecurityState.id == "audit").values(value=SecurityState.value)
            )
            previous, count = "", 0
            for row in s.scalars(select(AuditEvent).order_by(AuditEvent.id)):
                value = {
                    c.key: getattr(row, c.key) for c in AuditEvent.__table__.columns if c.key != "signature"
                }
                count += 1
                expected = self.cipher.sign_audit(
                    json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                )
                if (
                    row.id != count
                    or row.previous != previous
                    or not hmac.compare_digest(expected, row.signature)
                ):
                    return {"valid": False, "first_invalid": row.id}
                previous = row.signature
            head = s.get(SecurityState, "audit")
            return {
                "valid": head.version == count and head.value == previous,
                "events": count,
                "head": previous,
            }

    def consume_nonce(self, name, expires):
        try:
            with self.db.session.begin() as s:
                s.execute(
                    delete(Nonce).where(Nonce.expires_at < time.time() - self.settings.proof_window_seconds)
                )
                s.add(Nonce(id=name, expires_at=expires))
        except IntegrityError as exc:
            raise AppError("replay_detected", "请求已使用，请重新发起操作", 409) from exc

    def challenge(self):
        value = secrets.token_urlsafe(32)
        with self.db.session.begin() as s:
            s.execute(
                delete(Nonce).where(Nonce.expires_at < time.time() - self.settings.proof_window_seconds)
            )
            s.add(Nonce(id="challenge:" + digest(value), expires_at=time.time() + 90))
        return {"challenge": value, "expires_in": 90}

    def proof(self, encoded, method, target, body_hash, access_token="", jwk=None, thumbprint=None):
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get("alg") != "ES256" or header.get("typ") != "rage-proof+jwt" or header.get("crit"):
                raise ValueError("invalid header")
            key = jwk or header["jwk"]
            if key.get("kty") != "EC" or key.get("crv") != "P-256" or "d" in key:
                raise ValueError("invalid public key")
            public = {k: key[k] for k in ("crv", "kty", "x", "y")}
            fingerprint = b64(
                hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).digest()
            )
            if thumbprint and not hmac.compare_digest(thumbprint, fingerprint):
                raise ValueError("key binding")
            payload = jwt.decode(
                encoded,
                jwt.PyJWK.from_dict(public).key,
                algorithms=["ES256"],
                options={"require": ["jti", "iat", "htm", "htu", "bht", "ath"], "verify_aud": False},
            )
            if (
                type(payload["iat"]) not in (int, float)
                or abs(time.time() - payload["iat"]) > self.settings.proof_window_seconds
            ):
                raise ValueError("expired proof")
            if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", payload["jti"]):
                raise ValueError("invalid nonce")
            if (
                payload["htm"] != method
                or payload["htu"] != target
                or payload["bht"] != body_hash
                or payload["ath"] != digest(access_token)
            ):
                raise ValueError("request binding")
            return public, fingerprint, payload
        except Exception as exc:
            raise AppError("invalid_proof", "请求签名无效、已过期或与请求内容不一致", 401) from exc

    def login(self, username, password, challenge, proof, method, target, body_hash):
        public, fingerprint = self.verify_anonymous(challenge, proof, method, target, body_hash)
        with self.db.session() as s:
            user = s.scalar(select(User).where(User.username == username))
            stored = user.password_hash if user else hash_password("fixed-dummy-password")
            valid = verify_password(password, stored)
            if not user or not valid or not user.active:
                self.audit(
                    "auth.login",
                    outcome="denied",
                    details={"account": self.cipher.blind(username, "account")},
                )
                raise AppError("invalid_credentials", "用户名或密码错误，或账号已停用", 401)
            uid = user.id
        refresh = secrets.token_urlsafe(48)
        with self.db.session.begin() as s:
            self.lock(s)
            user = s.get(User, uid)
            if not user.active or user.password_hash != stored:
                raise AppError("session_revoked", "账号已停用", 401)
            record = AuthSession(
                user_id=uid,
                public_jwk=public,
                thumbprint=fingerprint,
                refresh_hash=digest(refresh),
                expires_at=time.time() + self.settings.session_hours * 3600,
            )
            s.add(record)
            s.flush()
            self.audit("auth.login", record.id, session=s, actor_id=uid)
            return self.tokens(s, user, record, refresh)

    def verify_anonymous(self, challenge, proof, method, target, body_hash):
        public, fingerprint, payload = self.proof(proof, method, target, body_hash)
        with self.db.session.begin() as s:
            result = s.execute(
                delete(Nonce).where(
                    Nonce.id == "challenge:" + digest(challenge), Nonce.expires_at > time.time()
                )
            )
            if not result.rowcount:
                raise AppError("invalid_challenge", "登录挑战已过期或已使用", 401)
        self.consume_nonce(
            "login:" + digest(fingerprint + payload["jti"]), time.time() + self.settings.proof_window_seconds
        )
        return public, fingerprint

    def tokens(self, s, user, record, refresh):
        now = int(time.time())
        access = jwt.encode(
            {
                "sub": user.id,
                "sid": record.id,
                "ver": user.token_version,
                "cnf": {"jkt": record.thumbprint},
                "iat": now,
                "exp": min(now + self.settings.token_minutes * 60, int(record.expires_at)),
                "jti": uuid.uuid4().hex,
                "iss": "rag-e",
                "aud": "rag-e-ui",
            },
            self.settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": min(self.settings.token_minutes * 60, int(record.expires_at - now)),
            "user": public_user(user, self.permissions(s, user.role)),
        }

    def authenticate(self, access, proof, method, target, body_hash):
        try:
            claims = jwt.decode(
                access,
                self.settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                issuer="rag-e",
                audience="rag-e-ui",
                options={"require": ["sub", "sid", "ver", "cnf", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise AppError("unauthorized", "登录已过期，请刷新或重新登录", 401) from exc
        if (
            not isinstance(claims.get("cnf"), dict)
            or not isinstance(claims.get("sid"), str)
            or not isinstance(claims.get("sub"), str)
        ):
            raise AppError("unauthorized", "令牌结构无效", 401)
        with self.db.session() as s:
            user, record = s.get(User, claims["sub"]), s.get(AuthSession, claims["sid"])
            if (
                not user
                or not user.active
                or user.token_version != claims["ver"]
                or not record
                or record.user_id != user.id
                or record.revoked
                or record.expires_at <= time.time()
            ):
                raise AppError("session_revoked", "登录会话已撤销或账号不可用", 401)
            if claims["cnf"].get("jkt") != record.thumbprint:
                raise AppError("invalid_proof", "会话密钥不匹配", 401)
            _, _, payload = self.proof(
                proof, method, target, body_hash, access, record.public_jwk, record.thumbprint
            )
            self.consume_nonce(
                record.id + ":" + payload["jti"], time.time() + self.settings.proof_window_seconds
            )
            return {**public_user(user, self.permissions(s, user.role)), "session_id": record.id}

    def refresh(self, refresh, proof, method, target, body_hash):
        value = digest(refresh)
        replay = False
        with self.db.session.begin() as s:
            self.lock(s)
            used = s.get(UsedRefresh, value)
            record = (
                s.get(AuthSession, used.session_id)
                if used
                else s.scalar(select(AuthSession).where(AuthSession.refresh_hash == value))
            )
            if not record:
                raise AppError("unauthorized", "刷新凭据无效", 401)
            _, _, payload = self.proof(
                proof, method, target, body_hash, refresh, record.public_jwk, record.thumbprint
            )
            # Insert the proof nonce in this transaction; UNIQUE protects all workers.
            if s.get(Nonce, record.id + ":" + payload["jti"]):
                raise AppError("replay_detected", "请求已使用", 409)
            s.add(
                Nonce(
                    id=record.id + ":" + payload["jti"],
                    expires_at=time.time() + self.settings.proof_window_seconds,
                )
            )
            user = s.get(User, record.user_id)
            if used:
                record.revoked = True
                self.audit("auth.refresh_reuse", record.id, "denied", session=s, actor_id=record.user_id)
                replay = True
            elif record.revoked or record.expires_at <= time.time() or not user.active:
                raise AppError("session_revoked", "会话已失效", 401)
            else:
                s.add(UsedRefresh(digest=value, session_id=record.id))
                next_refresh = secrets.token_urlsafe(48)
                record.refresh_hash = digest(next_refresh)
                result = self.tokens(s, user, record, next_refresh)
        if replay:
            raise AppError("refresh_replay", "刷新凭据重复使用，此会话已撤销", 401)
        return result

    def revoke(self, session_id=None, user_id=None):
        current = actor.get()
        with self.db.session.begin() as s:
            target = s.get(AuthSession, session_id) if session_id else None
            uid = user_id or (target.user_id if target else current["id"])
            if uid != current["id"]:
                self.require("security.manage")
            clause = AuthSession.id == session_id if session_id else AuthSession.user_id == uid
            s.execute(update(AuthSession).where(clause).values(revoked=True))
            self.audit("auth.revoke", session_id or uid, session=s)

    def change_password(self, old, new):
        current = actor.get()
        with self.db.session.begin() as s:
            self.lock(s)
            user = s.get(User, current["id"])
            if not verify_password(old, user.password_hash):
                raise AppError("invalid_credentials", "原密码不正确", 400)
            user.password_hash = hash_password(new)
            user.token_version += 1
            user.must_change_password = False
            s.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True))
            self.audit("auth.password_change", user.id, session=s)

    def users(self):
        self.require("users.manage")
        with self.db.session() as s:
            return [
                public_user(u, self.permissions(s, u.role))
                for u in s.scalars(select(User).order_by(User.username))
            ]

    def create_user(self, username, password, role="user", *, registration=False):
        if not registration:
            self.require("users.manage")
        if role == "admin" and actor.get() is not None and actor.get().get("role") != "admin":
            raise AppError("forbidden", "只有系统管理员可创建管理员", 403)
        hashed = hash_password(password)
        try:
            with self.db.session.begin() as s:
                self.lock(s)
                if not s.get(Role, role):
                    raise AppError("invalid_role", "角色不存在", 422)
                if actor.get() and not set(self.permissions(s, role)).issubset(actor.get()["permissions"]):
                    raise AppError("forbidden", "不能授予自己没有的权限", 403)
                user = User(
                    username=username, password_hash=hashed, role=role, must_change_password=not registration
                )
                s.add(user)
                s.flush()
                self.audit("user.create", user.id, session=s)
                return public_user(user, self.permissions(s, role))
        except IntegrityError as exc:
            raise AppError("username_exists", "用户名已存在", 409) from exc

    def update_user(self, uid, *, role=None, active=None, password=None):
        self.require("users.manage")
        with self.db.session.begin() as s:
            self.lock(s)
            user = s.get(User, uid)
            if not user:
                raise AppError("not_found", "用户不存在", 404)
            if actor.get() and not set(self.permissions(s, user.role)).issubset(actor.get()["permissions"]):
                raise AppError("forbidden", "不能修改权限高于自己的账号", 403)
            if (user.role == "admin" or role == "admin") and (actor.get() or {}).get(
                "role", "admin"
            ) != "admin":
                raise AppError("forbidden", "只有系统管理员可修改管理员", 403)
            if user.role == "admin" and user.active and (active is False or role and role != "admin"):
                count = s.scalar(
                    select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True))
                )
                if count <= 1:
                    raise AppError("last_admin", "不能停用或降级最后一位管理员", 409)
            if role is not None:
                if not s.get(Role, role):
                    raise AppError("invalid_role", "角色不存在", 422)
                if actor.get() and not set(self.permissions(s, role)).issubset(actor.get()["permissions"]):
                    raise AppError("forbidden", "不能授予自己没有的权限", 403)
                user.role = role
            if active is not None:
                user.active = active
            if password:
                user.password_hash = hash_password(password)
                user.must_change_password = True
            user.token_version += 1
            s.execute(update(AuthSession).where(AuthSession.user_id == uid).values(revoked=True))
            self.audit(
                "user.update",
                uid,
                details={"role": role, "active": active, "password_reset": bool(password)},
                session=s,
            )
            return public_user(user, self.permissions(s, user.role))

    def roles(self):
        self.require("roles.manage")
        with self.db.session() as s:
            return {
                "catalog": PERMISSIONS,
                "roles": [
                    {"id": r.id, "permissions": r.permissions, "builtin": r.builtin}
                    for r in s.scalars(select(Role))
                ],
            }

    def save_role(self, role_id, permissions):
        self.require("roles.manage")
        if role_id == "admin":
            raise AppError("builtin_role", "admin 角色不可修改", 409)
        if role_id == "user" and actor.get() is not None and actor.get().get("role") != "admin":
            raise AppError("forbidden", "只有系统管理员可修改 user 角色", 403)
        if set(permissions) - set(PERMISSIONS):
            raise AppError("invalid_permission", "包含未知权限", 422)
        if actor.get() and not set(permissions).issubset(actor.get()["permissions"]):
            raise AppError("forbidden", "不能授予自己没有的权限", 403)
        with self.db.session.begin() as s:
            self.lock(s)
            role = s.get(Role, role_id)
            if role is not None:
                if actor.get() and not set(role.permissions).issubset(actor.get()["permissions"]):
                    raise AppError("forbidden", "不能修改权限高于自己的角色", 403)
                role.permissions = sorted(set(permissions))
            else:
                s.add(Role(id=role_id, permissions=sorted(set(permissions)), builtin=role_id == "user"))
            self.audit("role.save", role_id, details={"permissions": permissions}, session=s)

    def delete_role(self, role_id):
        self.require("roles.manage")
        with self.db.session.begin() as s:
            self.lock(s)
            role = s.get(Role, role_id)
            if role and actor.get() and not set(role.permissions).issubset(actor.get()["permissions"]):
                raise AppError("forbidden", "不能删除权限高于自己的角色", 403)
            if (
                not role
                or role.builtin
                or s.scalar(select(func.count()).select_from(User).where(User.role == role_id))
            ):
                raise AppError("role_in_use", "角色是内置角色、不存在或仍有用户使用", 409)
            s.execute(delete(Grant).where(Grant.subject_type == "role", Grant.subject_id == role_id))
            s.delete(role)
            self.audit("role.delete", role_id, session=s)

    def bases(self):
        self.require("knowledge.read")
        with self.db.session() as s:
            read, write, manage = (set(self.visible_bases(s, level)) for level in ("read", "write", "manage"))
            return [
                {
                    "id": b.id,
                    "name": b.name,
                    "owner_id": b.owner_id,
                    "access": "manage" if b.id in manage else "write" if b.id in write else "read",
                }
                for b in s.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(read)))
            ]

    def create_base(self, name):
        self.require("knowledge.create")
        with self.db.session.begin() as s:
            base = KnowledgeBase(name=name, owner_id=actor.get()["id"])
            s.add(base)
            s.flush()
            self.audit("base.create", base.id, session=s)
            return {"id": base.id, "name": base.name, "owner_id": base.owner_id, "access": "manage"}

    def grants(self, base_id):
        self.require("knowledge.share")
        with self.db.session() as s:
            self.require_base(s, base_id, "manage")
            return [
                {"subject_type": g.subject_type, "subject_id": g.subject_id, "access": g.access}
                for g in s.scalars(select(Grant).where(Grant.base_id == base_id))
            ]

    def grant(self, base_id, subject_type, subject_id, access):
        self.require("knowledge.share")
        with self.db.session.begin() as s:
            self.lock(s)
            self.require_base(s, base_id, "manage")
            if not s.get(User if subject_type == "user" else Role, subject_id):
                raise AppError("not_found", "授权对象不存在", 404)
            if access:
                s.merge(
                    Grant(base_id=base_id, subject_type=subject_type, subject_id=subject_id, access=access)
                )
            else:
                s.execute(
                    delete(Grant).where(
                        Grant.base_id == base_id,
                        Grant.subject_type == subject_type,
                        Grant.subject_id == subject_id,
                    )
                )
            self.audit(
                "base.grant" if access else "base.revoke",
                base_id,
                details={"subject_type": subject_type, "subject_id": subject_id, "access": access},
                session=s,
            )

    def session_list(self):
        current = actor.get()
        with self.db.session() as s:
            query = (
                select(AuthSession)
                .where(AuthSession.user_id == current["id"])
                .order_by(AuthSession.created_at.desc())
            )
            return [
                {
                    "id": r.id,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "revoked": r.revoked,
                    "current": r.id == current["session_id"],
                }
                for r in s.scalars(query)
            ]
