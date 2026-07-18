"""
shared/database.py

Centralised PostgreSQL connection pool factory for the Engineering Intelligence Platform.

Usage in each service:
    from shared.database import create_pool, init_schema

    _pool = None

    @asynccontextmanager
    async def lifespan(app):
        global _pool
        _pool = await create_pool()
        if _pool:
            await init_schema(_pool)
        yield
        if _pool:
            await _pool.close()

Graceful degradation:
    If PostgreSQL is unavailable, create_pool() returns None and the calling
    service falls back to its in-memory store — identical to the original
    MVP behaviour, with a WARNING log.
"""
import logging
import os
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

POSTGRES_DSN: str = os.getenv(
    "POSTGRES_DSN",
    "postgresql://eip_user:eip_password@localhost:5432/eip_db",
)

# ---------------------------------------------------------------------------
# DDL — all tables are created with IF NOT EXISTS for idempotency
# ---------------------------------------------------------------------------

_DDL_REPOSITORIES = """
CREATE TABLE IF NOT EXISTS repositories (
    repository_id   TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    language        TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    visibility      TEXT    NOT NULL DEFAULT 'private',
    default_branch  TEXT    NOT NULL DEFAULT 'main',
    description     TEXT,
    status          TEXT    NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_synced_at  TIMESTAMPTZ
);
"""

_DDL_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    repository_id   TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    document_type   TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    word_count      INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DDL_DOCUMENT_CHUNKS = """
CREATE TABLE IF NOT EXISTS document_chunks (
    id           SERIAL  PRIMARY KEY,
    document_id  TEXT    NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    content      TEXT    NOT NULL,
    UNIQUE (document_id, chunk_index)
);
"""

_DDL_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'engineer',
    organization_id TEXT NOT NULL DEFAULT 'org_001',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_ALL_DDL = [
    _DDL_REPOSITORIES,
    _DDL_DOCUMENTS,
    _DDL_DOCUMENT_CHUNKS,
    _DDL_USERS,
]


# ---------------------------------------------------------------------------
# Pool factory
# ---------------------------------------------------------------------------

async def create_pool(min_size: int = 2, max_size: int = 10) -> Optional[asyncpg.Pool]:
    """
    Attempt to create an asyncpg connection pool.

    Returns:
        asyncpg.Pool  — on success
        None          — if PostgreSQL is unavailable (graceful degradation)
    """
    try:
        pool = await asyncpg.create_pool(
            POSTGRES_DSN,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
        )
        # Quick connectivity check
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        host_info = POSTGRES_DSN.split("@")[-1] if "@" in POSTGRES_DSN else POSTGRES_DSN
        logger.info("PostgreSQL connected: %s", host_info)
        return pool
    except Exception as exc:
        logger.warning(
            "PostgreSQL unavailable (%s). Service will use in-memory storage.", exc
        )
        return None


async def init_schema(pool: asyncpg.Pool) -> None:
    """
    Run all DDL statements to create tables if they do not already exist.
    Safe to call on every startup (idempotent).
    """
    async with pool.acquire() as conn:
        for ddl in _ALL_DDL:
            await conn.execute(ddl)
    logger.info("PostgreSQL schema initialised (all tables verified).")
