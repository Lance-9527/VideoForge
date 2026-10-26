"""Central VideoForge OTP accounts and the desktop's opt-in HTTP proxy.

The desktop must use ``create_account_router``. Only an owner's separately
deployed server uses ``create_account_app`` and stores account/delivery secrets.
No provider is enabled implicitly; failed delivery never returns a usable code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from email.message import EmailMessage
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx


class AccountError(Exception):
    """A public failure containing a fixed error code and safe user message."""
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _failure(error: AccountError) -> HTTPException:
    return HTTPException(error.status, detail={"code": error.code, "message": error.message})


def normalize_identifier(value: str, country_code: str = "") -> tuple[str, str]:
    """Normalize a verified contact; a phone without '+' needs an explicit region."""
    text = str(value or "").strip()
    if "@" in text:
        if len(text) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
            raise AccountError(422, "invalid_identifier", "请输入有效邮箱或带国家区号的手机号")
        return text.casefold(), "email"
    phone = re.sub(r"[\s()-]", "", text)
    if not phone.startswith("+") and country_code:
        phone = "+" + country_code.lstrip("+") + phone
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        raise AccountError(422, "invalid_identifier", "手机号需要国家区号，例如 +86")
    return phone, "sms"


def _nickname(value: str) -> str:
    result = str(value or "").strip()
    if len(result) > 40 or any(ord(char) < 32 for char in result):
        raise AccountError(422, "invalid_nickname", "昵称最多40个字符，不能包含控制字符")
    return result


def _safe_url(value: str) -> str:
    """Allow TLS endpoints and loopback development; never accept credentials in URLs."""
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in ("http", "https") or not parsed.hostname):
        raise ValueError("账户服务URL必须是不带凭据/查询参数的HTTP(S)地址")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("非本机账户服务必须使用HTTPS")
    return url


class Delivery(Protocol):
    """Server-owned delivery implementation; plaintext codes stay inside this call."""
    def available(self, channel: str) -> bool: ...
    def send(self, channel: str, identifier: str, code: str, ttl: int) -> None: ...


class EnvironmentDelivery:
    """SMTP and an explicitly configured HTTP SMS provider, without bundled credentials."""
    def __init__(self, env: dict[str, str] | None = None):
        self.env = dict(os.environ if env is None else env)

    def available(self, channel: str) -> bool:
        if channel == "email":
            return bool(self.env.get("VF_ACCOUNT_SMTP_HOST") and self.env.get("VF_ACCOUNT_SMTP_FROM")
                        and self.env.get("VF_ACCOUNT_SMTP_TLS", "starttls") in ("ssl", "starttls"))
        if channel == "sms":
            return bool(self.env.get("VF_ACCOUNT_SMS_URL") and self.env.get("VF_ACCOUNT_SMS_TOKEN")
                        and self.env.get("VF_ACCOUNT_SMS_TEMPLATE") and self.env.get("VF_ACCOUNT_SMS_SUCCESS_FIELD")
                        and "VF_ACCOUNT_SMS_SUCCESS_VALUE" in self.env)
        return False

    def send(self, channel: str, identifier: str, code: str, ttl: int) -> None:
        if not self.available(channel):
            raise AccountError(503, "delivery_unavailable", "该验证码通道尚未开通")
        try:
            if channel == "email":
                self._email(identifier, code, ttl)
            else:
                self._sms(identifier, code, ttl)
        except AccountError:
            raise
        except Exception as error:
            # Provider errors may contain request bodies, tokens or personal data.
            raise AccountError(502, "delivery_failed", "验证码发送失败，请稍后重试") from error

    def _email(self, identifier: str, code: str, ttl: int) -> None:
        env = self.env
        mail = EmailMessage()
        mail["From"] = env["VF_ACCOUNT_SMTP_FROM"]
        mail["To"] = identifier
        mail["Subject"] = "VideoForge 登录验证码"
        mail.set_content(f"你的VideoForge验证码是：{code}\n有效期{ttl // 60}分钟。请勿向他人透露。\n如非本人操作，请忽略。")
        tls = env.get("VF_ACCOUNT_SMTP_TLS", "starttls")
        host, port = env["VF_ACCOUNT_SMTP_HOST"], int(env.get("VF_ACCOUNT_SMTP_PORT", "465" if tls == "ssl" else "587"))
        if tls == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=12, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, port, timeout=12)
        with client:
            if tls == "starttls":
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if env.get("VF_ACCOUNT_SMTP_USERNAME"):
                client.login(env["VF_ACCOUNT_SMTP_USERNAME"], env.get("VF_ACCOUNT_SMTP_PASSWORD", ""))
            if client.send_message(mail):
                raise AccountError(502, "delivery_failed", "验证码发送失败，请稍后重试")

    def _sms(self, identifier: str, code: str, ttl: int) -> None:
        env = self.env
        url = _safe_url(env["VF_ACCOUNT_SMS_URL"])
        # A provider-specific gateway can map these fields to its signed vendor API.
        payload = {"to": identifier, "code": code, "templateId": env["VF_ACCOUNT_SMS_TEMPLATE"],
                   "expiresInSeconds": ttl}
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
            response = client.post(url, json=payload, headers={"Authorization": "Bearer " + env["VF_ACCOUNT_SMS_TOKEN"]})
        response.raise_for_status()
        value: Any = response.json()
        for segment in env["VF_ACCOUNT_SMS_SUCCESS_FIELD"].split("."):
            if not isinstance(value, dict) or segment not in value:
                raise AccountError(502, "delivery_failed", "短信服务未确认发送成功")
            value = value[segment]
        expected = json.loads(env["VF_ACCOUNT_SMS_SUCCESS_VALUE"])
        if type(value) is not type(expected) or value != expected:
            raise AccountError(502, "delivery_failed", "短信服务未确认发送成功")


@dataclass(frozen=True)
class AccountConfig:
    database: Path
    secret: str = field(repr=False)
    admin_contacts: frozenset[str] = field(default_factory=frozenset, repr=False)
    phone_country_code: str = ""
    otp_ttl: int = 600
    resend_seconds: int = 60
    max_attempts: int = 5
    session_ttl: int = 30 * 24 * 3600

    @classmethod
    def from_environment(cls) -> "AccountConfig":
        """Read only server-owned settings; this is never called by the desktop proxy."""
        secret = os.environ.get("VF_ACCOUNT_SECRET", "")
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("集中账户服务需要至少32字节的VF_ACCOUNT_SECRET")
        country = os.environ.get("VF_ACCOUNT_PHONE_COUNTRY_CODE", "").lstrip("+")
        admins = frozenset(normalize_identifier(v.strip(), country)[0]
                           for v in os.environ.get("VF_ACCOUNT_ADMIN_CONTACTS", "").split(",") if v.strip())
        database = Path(os.environ.get("VF_ACCOUNT_DATABASE", "./account-data/accounts.sqlite3")).resolve()
        return cls(database, secret, admins, country)


class AccountService:
    """SQLite-backed OTP exchange; all transitions are serialized transactions."""
    def __init__(self, config: AccountConfig, delivery: Delivery, clock: Callable[[], float] = time.time):
        if len(config.secret.encode("utf-8")) < 32:
            raise ValueError("账户服务secret至少32字节")
        self.config, self.delivery, self.clock = config, delivery, clock
        config.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_users (
                    id TEXT PRIMARY KEY, identifier TEXT UNIQUE NOT NULL, channel TEXT NOT NULL,
                    nickname TEXT NOT NULL, created_at INTEGER NOT NULL, last_login_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS account_challenges (
                    id TEXT PRIMARY KEY, identifier TEXT NOT NULL, channel TEXT NOT NULL,
                    nickname TEXT NOT NULL, code_digest TEXT NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, ip_digest TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS account_challenge_contact ON account_challenges(identifier,created_at);
                CREATE TABLE IF NOT EXISTS account_sessions (
                    token_digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES account_users(id),
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS account_rates (
                    kind TEXT NOT NULL, ip_digest TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS account_rate_ip ON account_rates(kind,ip_digest,created_at);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.config.database), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _hash(self, value: str) -> str:
        return hmac.new(self.config.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def _user(self, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in ("id", "nickname", "identifier", "channel", "created_at", "last_login_at")} | {
            "role": "admin" if row["identifier"] in self.config.admin_contacts else "user"}

    def config_public(self) -> dict[str, Any]:
        return {"configured": True, "channels": [name for name in ("email", "sms") if self.delivery.available(name)],
                "nickname_required_for_registration": True, "phone_country_code": self.config.phone_country_code,
                "otp_digits": 6, "expires_in": self.config.otp_ttl, "resend_after": self.config.resend_seconds}

    def send_code(self, identifier: str, nickname: str, ip: str) -> dict[str, Any]:
        identifier, channel = normalize_identifier(identifier, self.config.phone_country_code)
        nickname = _nickname(nickname)
        if not self.delivery.available(channel):
            raise AccountError(503, "delivery_unavailable", "该验证码通道尚未开通")
        now, ip_hash = int(self.clock()), self._hash("ip:" + ip)
        challenge, code = secrets.token_urlsafe(24), f"{secrets.randbelow(1000000):06d}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            recent = db.execute("SELECT created_at FROM account_challenges WHERE identifier=? ORDER BY created_at DESC LIMIT 1", (identifier,)).fetchone()
            if recent and now - recent[0] < self.config.resend_seconds:
                raise AccountError(429, "resend_too_soon", "请稍后再获取验证码")
            contacts = db.execute("SELECT COUNT(*) FROM account_challenges WHERE identifier=? AND created_at>?", (identifier, now-3600)).fetchone()[0]
            ips = db.execute("SELECT COUNT(*) FROM account_rates WHERE kind='send' AND ip_digest=? AND created_at>?", (ip_hash, now-3600)).fetchone()[0]
            daily = db.execute("SELECT COUNT(*) FROM account_rates WHERE kind='send' AND ip_digest=? AND created_at>?", (ip_hash, now-86400)).fetchone()[0]
            if contacts >= 5 or ips >= 20 or daily >= 100:
                raise AccountError(429, "send_rate_limited", "验证码请求过多，请稍后再试")
            db.execute("UPDATE account_challenges SET state='superseded' WHERE identifier=? AND state IN ('sending','sent')", (identifier,))
            db.execute("INSERT INTO account_challenges(id,identifier,channel,nickname,code_digest,created_at,expires_at,state,ip_digest) VALUES(?,?,?,?,?,?,?,?,?)",
                       (challenge,identifier,channel,nickname,self._hash(challenge+":"+identifier+":"+code),now,now+self.config.otp_ttl,"sending",ip_hash))
            db.execute("INSERT INTO account_rates VALUES('send',?,?)", (ip_hash,now))
        try:
            self.delivery.send(channel,identifier,code,self.config.otp_ttl)
        except Exception as error:
            with self._connect() as db:
                db.execute("UPDATE account_challenges SET state='failed' WHERE id=?", (challenge,))
            if isinstance(error, AccountError):
                raise
            raise AccountError(502,"delivery_failed","验证码发送失败，请稍后重试") from error
        with self._connect() as db:
            db.execute("UPDATE account_challenges SET state='sent' WHERE id=? AND state='sending'", (challenge,))
        masked = (identifier[:2] + "***" + identifier[identifier.index("@"):]) if channel == "email" else identifier[:3]+"****"+identifier[-4:]
        return {"ok": True, "challenge_id": challenge, "expires_in": self.config.otp_ttl,
                "resend_after": self.config.resend_seconds, "channel": channel, "masked_identifier": masked}

    def verify(self, challenge_id: str, code: str, ip: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]{6}", code):
            raise AccountError(422,"invalid_code","请输入6位验证码")
        now, ip_hash = int(self.clock()), self._hash("ip:"+ip)
        error: AccountError | None = None
        result: dict[str, Any] = {}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM account_rates WHERE kind='verify' AND ip_digest=? AND created_at>?", (ip_hash,now-3600)).fetchone()[0]
            if count >= 30:
                raise AccountError(429,"verify_rate_limited","验证尝试过多，请稍后再试")
            db.execute("INSERT INTO account_rates VALUES('verify',?,?)", (ip_hash,now))
            row = db.execute("SELECT * FROM account_challenges WHERE id=?", (challenge_id,)).fetchone()
            if (not row or row["state"] != "sent" or row["expires_at"] <= now
                    or row["attempts"] >= self.config.max_attempts):
                error = AccountError(400,"invalid_or_expired_code","验证码错误或已过期，请重新获取")
            else:
                db.execute("UPDATE account_challenges SET attempts=attempts+1 WHERE id=?", (challenge_id,))
                expected = self._hash(challenge_id+":"+row["identifier"]+":"+code)
                if not hmac.compare_digest(expected,row["code_digest"]):
                    error = AccountError(400,"invalid_or_expired_code","验证码错误或已过期，请重新获取")
                else:
                    user = db.execute("SELECT * FROM account_users WHERE identifier=?", (row["identifier"],)).fetchone()
                    if not user and not row["nickname"]:
                        error = AccountError(422,"nickname_required","首次注册需要填写昵称后重新获取验证码")
                        db.execute("UPDATE account_challenges SET state='used' WHERE id=?", (challenge_id,))
                    else:
                        db.execute("UPDATE account_challenges SET state='used' WHERE id=?", (challenge_id,))
                        if not user:
                            user_id = secrets.token_urlsafe(18)
                            db.execute("INSERT INTO account_users VALUES(?,?,?,?,?,?)", (user_id,row["identifier"],row["channel"],row["nickname"],now,now))
                        else:
                            user_id = user["id"]
                            db.execute("UPDATE account_users SET last_login_at=? WHERE id=?", (now,user_id))
                        user = db.execute("SELECT * FROM account_users WHERE id=?", (user_id,)).fetchone()
                        token = secrets.token_urlsafe(32)
                        db.execute("INSERT INTO account_sessions VALUES(?,?,?,?)", (self._hash("session:"+token),user_id,now,now+self.config.session_ttl))
                        result = {"ok": True,"access_token": token,"token_type": "bearer",
                                  "expires_in": self.config.session_ttl,"user": self._user(user)}
            # Commit wrong-attempt counters too. Raising inside this context would roll them back.
        if error:
            raise error
        return result

    def me(self, token: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT u.* FROM account_users u JOIN account_sessions s ON s.user_id=u.id WHERE s.token_digest=? AND s.expires_at>?", (self._hash("session:"+token),int(self.clock()))).fetchone()
        if not row:
            raise AccountError(401,"unauthenticated","请先登录VideoForge账户")
        return self._user(row)

    def logout(self, token: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM account_sessions WHERE token_digest=?", (self._hash("session:"+token),))

    def users(self, token: str, offset: int, limit: int) -> dict[str, Any]:
        if self.me(token)["role"] != "admin":
            raise AccountError(403,"admin_required","需要管理员账户")
        offset, limit = max(0,min(1_000_000,offset)), max(1,min(100,limit))
        with self._connect() as db:
            rows = db.execute("SELECT * FROM account_users ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (limit,offset)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM account_users").fetchone()[0]
        return {"ok": True,"users": [self._user(row) for row in rows],"total": total,"offset": offset,"limit": limit}


class SendCodeRequest(BaseModel):
    identifier: str = Field(min_length=3,max_length=254)
    nickname: str = Field(default="",max_length=40)


class VerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=16,max_length=100)
    code: str = Field(pattern=r"^[0-9]{6}$")


def _bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise AccountError(401,"unauthenticated","请先登录VideoForge账户")
    return authorization[7:].strip()


def create_server_router(service: AccountService) -> APIRouter:
    """Expose the central server's account routes, without an unauthenticated user list."""
    router = APIRouter(prefix="/api/account",tags=["VideoForge accounts"])

    @router.get("/config")
    def config():
        return service.config_public()

    @router.post("/send-code")
    def send_code(body: SendCodeRequest,request: Request):
        try:
            return service.send_code(body.identifier,body.nickname,request.client.host if request.client else "unknown")
        except AccountError as error:
            raise _failure(error) from None

    @router.post("/verify")
    def verify(body: VerifyRequest,request: Request):
        try:
            return service.verify(body.challenge_id,body.code,request.client.host if request.client else "unknown")
        except AccountError as error:
            raise _failure(error) from None

    @router.get("/me")
    def me(request: Request):
        try:
            return {"ok": True,"user": service.me(_bearer(request))}
        except AccountError as error:
            raise _failure(error) from None

    @router.post("/logout")
    def logout(request: Request):
        try:
            service.logout(_bearer(request))
            return {"ok": True}
        except AccountError as error:
            raise _failure(error) from None

    @router.get("/admin/users")
    def users(request: Request,offset: int = 0,limit: int = 50):
        try:
            return service.users(_bearer(request),offset,limit)
        except AccountError as error:
            raise _failure(error) from None

    return router


def create_account_app() -> FastAPI:
    """Uvicorn factory for the owner-deployed central service; never start it in a client."""
    service = AccountService(AccountConfig.from_environment(),EnvironmentDelivery())
    app = FastAPI(title="VideoForge Accounts",docs_url=None,redoc_url=None,openapi_url=None)
    app.include_router(create_server_router(service))

    @app.middleware("http")
    async def no_cache(request: Request,call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app


def create_account_router() -> APIRouter:
    """Desktop proxy configured only by VF_ACCOUNT_SERVER_URL; no local account fallback."""
    raw = os.environ.get("VF_ACCOUNT_SERVER_URL", "").strip()
    base = _safe_url(raw) if raw else ""
    router = APIRouter(prefix="/api/account",tags=["VideoForge account proxy"])

    async def relay(path: str,request: Request,body: dict[str,Any] | None = None):
        if not base:
            raise _failure(AccountError(503,"account_service_unconfigured","VideoForge账户服务尚未开通，请联系软件作者配置"))
        headers = {"Accept": "application/json"}
        if request.headers.get("authorization"):
            headers["Authorization"] = request.headers["authorization"]
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False) as client:
                response = await client.request(request.method,base+path,headers=headers,json=body,
                                                params=dict(request.query_params) if request.method == "GET" else None)
            if len(response.content) > 2_000_000:
                raise ValueError("Oversized account response")
            data = response.json()
        except Exception:
            raise _failure(AccountError(502,"account_service_unreachable","暂时无法连接VideoForge账户服务，请稍后重试")) from None
        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data,dict) else None
            if not isinstance(detail,dict) or not isinstance(detail.get("message"),str):
                detail = {"code":"account_service_error","message":"账户服务暂时无法完成请求"}
            raise HTTPException(response.status_code,detail=detail)
        if not 200 <= response.status_code < 300 or not isinstance(data,dict):
            raise _failure(AccountError(502,"account_service_error","账户服务返回格式异常"))
        return JSONResponse(data,headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @router.get("/config")
    async def config(request: Request):
        if not base:
            return {"configured": False,"channels": [],"message": "账户服务尚未开通；部署集中后台并配置账户服务地址后即可注册登录"}
        return await relay("/config",request)

    @router.post("/send-code")
    async def send_code(body: SendCodeRequest,request: Request):
        return await relay("/send-code",request,body.model_dump())

    @router.post("/verify")
    async def verify(body: VerifyRequest,request: Request):
        return await relay("/verify",request,body.model_dump())

    @router.get("/me")
    async def me(request: Request):
        return await relay("/me",request)

    @router.post("/logout")
    async def logout(request: Request):
        return await relay("/logout",request,{})

    @router.get("/admin/users")
    async def users(request: Request):
        return await relay("/admin/users",request)

    return router
