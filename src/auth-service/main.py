"""
Auth Service — FastAPI application.

Responsibilities (phases.md §Authentication Service):
- User management (in-memory for MVP)
- JWT authentication (python-jose)
- Refresh tokens
- Role management (admin, engineer, viewer)

For production, replace the in-memory user store with a database and
use bcrypt for password hashing.
"""
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, Field, ConfigDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRE_MINUTES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

# ---------------------------------------------------------------------------
# In-memory user store (MVP)
# ---------------------------------------------------------------------------
_USERS: Dict[str, dict] = {
    "admin": {
        "userId": "user_admin",
        "username": "admin",
        "password": "admin",        # plaintext for MVP — use bcrypt in production
        "role": "admin",
        "organizationId": "org_001",
        "createdAt": datetime.utcnow().isoformat() + "Z",
    },
    "engineer": {
        "userId": "user_engineer",
        "username": "engineer",
        "password": "engineer",
        "role": "engineer",
        "organizationId": "org_001",
        "createdAt": datetime.utcnow().isoformat() + "Z",
    },
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str = Field(..., alias="accessToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expires_in: int = Field(..., alias="expiresIn")
    user_id: str = Field(..., alias="userId")
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
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user["userId"],
        "username": user["username"],
        "role": user["role"],
        "organizationId": user["organizationId"],
        "exp": expire,
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
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="Auth Service",
    description="JWT-based authentication and user management.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "auth-service"}


@app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
async def login(form: OAuth2PasswordRequestForm = Depends()):
    """Standard OAuth2 password flow. Returns a JWT access token."""
    user = _USERS.get(form.username)
    if user is None or user["password"] != form.password:
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
    if req.username in _USERS:
        raise HTTPException(status_code=409, detail=f"Username '{req.username}' already exists.")
    user_id = f"user_{uuid.uuid4().hex[:8]}"
    _USERS[req.username] = {
        "userId": user_id,
        "username": req.username,
        "password": req.password,
        "role": req.role,
        "organizationId": req.organization_id,
        "createdAt": datetime.utcnow().isoformat() + "Z",
    }
    logger.info("User created: username=%s role=%s", req.username, req.role)
    return {"userId": user_id, "username": req.username, "role": req.role}


@app.get("/users", tags=["Users"])
async def list_users(current_user: dict = Depends(get_current_user)):
    """List all users (admin only)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    safe = [{k: v for k, v in u.items() if k != "password"} for u in _USERS.values()]
    return {"data": safe, "total": len(safe)}
