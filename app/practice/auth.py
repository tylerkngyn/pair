"""Password accounts with opaque, revocable cookies and same-origin CSRF checks."""
import hashlib
import hmac
import os
import secrets
import time
from threading import BoundedSemaphore

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from app.practice.database import Login, RateLimit, User

COOKIE = "pair_session"
MAX_AGE = 7 * 24 * 60 * 60
PASSWORD_SLOTS = BoundedSemaphore(2)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        value = value.lower().strip()
        if len(value) < 3 or not value.isascii() or not value.replace("_", "").isalnum():
            raise ValueError("Use 3–32 letters, numbers, or underscores")
        return value


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    with PASSWORD_SLOTS:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def check_password(password, encoded):
    _, salt, _ = encoded.split("$")
    return hmac.compare_digest(hash_password(password, salt), encoded)


# Equalize the expensive password step for nonexistent users.
DUMMY_HASH = hash_password("dummy-password-never-used")


def same_origin(request: Request):
    origin = request.headers.get("origin")
    expected = os.environ.get("PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
    if origin and origin != expected:
        raise HTTPException(403, "Request origin is not allowed")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Request origin is not allowed")


def current_login(request: Request):
    token = request.cookies.get(COOKIE, "")
    with request.app.state.database.session() as db:
        login = db.get(Login, token_hash(token))
        if login is None or login.expires_at <= time.time():
            raise HTTPException(401, "Sign in to continue")
        user = db.get(User, login.user_id)
        if not user:
            raise HTTPException(401, "Sign in to continue")
        return user, login


def authenticated_write(request: Request, identity=Depends(current_login)):
    same_origin(request)
    if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), identity[1].csrf):
        raise HTTPException(403, "Refresh the page and try again")
    return identity


def throttle(request, username):
    # Do not trust caller-supplied X-Forwarded-For. Uvicorn's proxy configuration
    # determines request.client. Limits also apply per username across addresses.
    now = time.time()
    address = request.client.host if request.client else "unknown"
    with request.app.state.database.session.begin() as db:
        db.execute(delete(RateLimit).where(RateLimit.reset_at < now))
        for label, limit in (("ip:" + address, 40), ("user:" + username, 12)):
            key = token_hash(label)
            insert = sqlite_insert if db.bind.dialect.name == "sqlite" else postgres_insert
            statement = insert(RateLimit).values(key=key, count=1, reset_at=now + 900)
            statement = statement.on_conflict_do_update(index_elements=[RateLimit.key], set_={"count": RateLimit.count + 1}).returning(RateLimit.count)
            if db.execute(statement).scalar_one() > limit:
                raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")


def issue_login(db, request, response, user):
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_hex(24)
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(delete(Login).where(Login.token_hash == token_hash(old)))
    db.execute(delete(Login).where(Login.expires_at < time.time()))
    db.add(Login(token_hash=token_hash(raw), user_id=user.id, csrf=csrf, expires_at=time.time() + MAX_AGE))
    secure = os.environ.get("COOKIE_SECURE", "").lower() == "true" or request.url.scheme == "https"
    response.set_cookie(COOKIE, raw, max_age=MAX_AGE, httponly=True, secure=secure, samesite="strict", path="/")
    return {"username": user.username, "csrf": csrf}


def register(credentials: Credentials, request: Request, response: Response):
    same_origin(request)
    throttle(request, credentials.username)
    with request.app.state.database.session.begin() as db:
        user = User(id=secrets.token_hex(16), username=credentials.username, password_hash=hash_password(credentials.password))
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            raise HTTPException(409, "Username is unavailable") from None
        return issue_login(db, request, response, user)


def login(credentials: Credentials, request: Request, response: Response):
    same_origin(request)
    throttle(request, credentials.username)
    with request.app.state.database.session.begin() as db:
        user = db.scalar(select(User).where(User.username == credentials.username))
        valid = check_password(credentials.password, user.password_hash if user else DUMMY_HASH)
        if not user or not valid:
            raise HTTPException(401, "Incorrect username or password")
        return issue_login(db, request, response, user)


def logout(request: Request, response: Response, identity=Depends(authenticated_write)):
    with request.app.state.database.session.begin() as db:
        db.execute(delete(Login).where(Login.token_hash == identity[1].token_hash))
    response.delete_cookie(COOKIE, path="/", samesite="strict")
    return {"ok": True}


def me(identity=Depends(current_login)):
    return {"username": identity[0].username, "csrf": identity[1].csrf}
