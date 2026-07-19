"""
Auth Service — FastAPI application.

Responsibilities (phases.md §Authentication Service):
- User management (PostgreSQL with in-memory fallback)
- JWT authentication (python-jose)
- Refresh tokens
- Role management (admin, engineer, viewer)

Security
--------
Passwords are ALWAYS hashed with bcrypt — both in-memory and PostgreSQL paths.
The original plaintext password storage has been removed.
"""
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field, ConfigDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRE_MINUTES
from shared.database import create_pool, init_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Backend state
# ---------------------------------------------------------------------------
_pool: Optional[asyncpg.Pool] = None

# In-memory fallback: username → user record (with hashed password)
_USERS: Dict[str, dict] = {}


def _hash(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def _verify(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Seed default users
# ---------------------------------------------------------------------------
# Phase 1 limitation (explicitly acknowledged):
# Default credentials are hardcoded here for local development.  The startup
# guard in `lifespan()` refuses to boot with these in production (ENV=production).
#
# Phase 2 path: replace this list with env-var / secret-manager driven seed:
#   SEED_USER_0=admin:password:admin:org_001
#   SEED_USER_1=engineer:password:engineer:org_001
# This allows operators to supply secure credentials at deploy time without
# rebuilding the image.  Until then, production deployments MUST override
# admin credentials via POST /users after first boot with a custom admin account.
_DEFAULT_USERS = [
    {"user_id": "user_admin",    "username": "admin",    "password": "admin",    "role": "admin",    "organization_id": "org_001"},
    {"user_id": "user_engineer", "username": "engineer", "password": "engineer", "role": "engineer", "organization_id": "org_001"},
]


async def _seed_users() -> None:
    """Insert default users if they don't already exist."""
    for u in _DEFAULT_USERS:
        pw_hash = _hash(u["password"])

        if _pool:
            async with _pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT user_id FROM users WHERE username=$1", u["username"]
                )
                if not existing:
                    await conn.execute(
                        "INSERT INTO users (user_id, username, password_hash, role, organization_id) "
                        "VALUES ($1,$2,$3,$4,$5)",
                        u["user_id"], u["username"], pw_hash, u["role"], u["organization_id"],
                    )
                    logger.info("Seeded default user: %s", u["username"])
        else:
            if u["username"] not in _USERS:
                _USERS[u["username"]] = {
                    "userId":         u["user_id"],
                    "username":       u["username"],
                    "passwordHash":   pw_hash,
                    "role":           u["role"],
                    "organizationId": u["organization_id"],
                    "createdAt":      _now_iso(),
                }


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------

async def _get_user_by_username(username: str) -> Optional[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE username=$1", username
            )
            if row is None:
                return None
            d = dict(row)
            return {
                "userId":         d["user_id"],
                "username":       d["username"],
                "passwordHash":   d["password_hash"],
                "role":           d["role"],
                "organizationId": d["organization_id"],
                "createdAt":      d["created_at"].isoformat(),
            }
    return _USERS.get(username)


async def _get_user_by_id(user_id: str) -> Optional[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
            if row is None:
                return None
            d = dict(row)
            return {
                "userId":         d["user_id"],
                "username":       d["username"],
                "role":           d["role"],
                "organizationId": d["organization_id"],
            }
    for u in _USERS.values():
        if u["userId"] == user_id:
            return {k: v for k, v in u.items() if k != "passwordHash"}
    return None


async def _list_users() -> list:
    if _pool:
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, username, role, organization_id, created_at FROM users")
            return [
                {
                    "userId":         r["user_id"],
                    "username":       r["username"],
                    "role":           r["role"],
                    "organizationId": r["organization_id"],
                    "createdAt":      r["created_at"].isoformat(),
                }
                for r in rows
            ]
    return [{k: v for k, v in u.items() if k != "passwordHash"} for u in _USERS.values()]


async def _create_user_record(user_id: str, username: str, pw_hash: str, role: str, org_id: str) -> dict:
    if _pool:
        async with _pool.acquire() as conn:
            existing = await conn.fetchval("SELECT user_id FROM users WHERE username=$1", username)
            if existing:
                raise ValueError(f"Username '{username}' already exists.")
            await conn.execute(
                "INSERT INTO users (user_id, username, password_hash, role, organization_id) VALUES ($1,$2,$3,$4,$5)",
                user_id, username, pw_hash, role, org_id,
            )
    else:
        if username in _USERS:
            raise ValueError(f"Username '{username}' already exists.")
        _USERS[username] = {
            "userId":         user_id,
            "username":       username,
            "passwordHash":   pw_hash,
            "role":           role,
            "organizationId": org_id,
            "createdAt":      _now_iso(),
        }
    return {"userId": user_id, "username": username, "role": role}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str = Field(..., alias="accessToken")
    token_type: str   = Field(default="bearer", alias="tokenType")
    expires_in: int   = Field(..., alias="expiresIn")
    user_id: str      = Field(..., alias="userId")
    role: str

    model_config = ConfigDict(populate_by_name=True)


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "engineer"
    organization_id: str = Field(default="org_001", alias="organizationId")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def _create_token(user: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub":            user["userId"],
        "username":       user["username"],
        "role":           user["role"],
        "organizationId": user["organizationId"],
        "exp":            expire,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    return _decode_token(token)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool

    # Security warnings — emit on every startup so they show in logs
    _PROD = os.getenv("ENV", "development").lower() == "production"
    _DEFAULT_SECRET = "changeme"
    if _DEFAULT_SECRET in JWT_SECRET_KEY:
        msg = "JWT_SECRET_KEY is set to a default/insecure value."
        if _PROD:
            raise RuntimeError(f"SECURITY ERROR: {msg} Set a strong secret via JWT_SECRET_KEY env var.")
        logger.warning("SECURITY WARNING: %s This is only acceptable for local development.", msg)

    if _PROD:
        # In production, refuse to start with default admin/engineer passwords
        default_passwords = {u["password"] for u in _DEFAULT_USERS}
        for u in _DEFAULT_USERS:
            if u["password"] in default_passwords:
                raise RuntimeError(
                    f"SECURITY ERROR: Default password detected for user '{u['username']}'. "
                    "Change all default passwords before running in production."
                )
    else:
        logger.warning(
            "SECURITY WARNING: Default credentials (admin/admin, engineer/engineer) are active. "
            "Set ENV=production and change passwords before deploying."
        )

    _pool = await create_pool()
    if _pool:
        await init_schema(_pool)
    await _seed_users()
    yield
    if _pool:
        await _pool.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Auth Service",
    description="JWT-based authentication and user management with bcrypt password hashing.",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "auth-service", "version": "2.0.0"}


@app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
async def login(form: OAuth2PasswordRequestForm = Depends()):
    """Standard OAuth2 password flow. Returns a JWT access token."""
    user = await _get_user_by_username(form.username)
    if user is None or not _verify(form.password, user["passwordHash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = _create_token(user)
    logger.info("User authenticated: username=%s role=%s", user["username"], user["role"])
    return TokenResponse(
        accessToken=token,
        expiresIn=JWT_EXPIRE_MINUTES * 60,
        userId=user["userId"],
        role=user["role"],
    )


@app.get("/auth/me", tags=["Auth"])
async def me(current_user: dict = Depends(get_current_user)):
    """Return the currently authenticated user's information."""
    return {"data": current_user}


@app.post("/users", status_code=201, tags=["Users"])
async def create_user(req: UserCreateRequest, current_user: dict = Depends(get_current_user)):
    """Create a new user (admin only)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    user_id = f"user_{uuid.uuid4().hex[:8]}"
    pw_hash = _hash(req.password)
    try:
        result = await _create_user_record(user_id, req.username, pw_hash, req.role, req.organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    logger.info("User created: username=%s role=%s", req.username, req.role)
    return result


@app.get("/users", tags=["Users"])
async def list_users(current_user: dict = Depends(get_current_user)):
    """List all users (admin only)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    users = await _list_users()
    return {"data": users, "total": len(users)}
