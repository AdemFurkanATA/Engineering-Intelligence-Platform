"""
Repository Service — business logic layer.

Responsible for:
- Repository registration (FR-101)
- Repository metadata management (FR-120)
- URL validation (FR-103)
- Duplicate rejection (FR-101)
- Publishing domain events to Kafka (EVENT_CATALOG.md §2)

Storage strategy
----------------
When a PostgreSQL pool is available (_pool is not None), all operations read
from and write to the `repositories` table.  If PostgreSQL is unavailable at
startup the service falls back to the in-memory `_store` / `_url_index`
dictionaries — identical to the original MVP behaviour — and logs a WARNING.

The public async API is identical in both modes so callers (main.py, tests)
never need to know which backend is active.
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import asyncpg
from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend state
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None          # set by main.py at startup
_store: Dict[str, dict] = {}                  # in-memory fallback: repo_id → record
_url_index: Dict[str, str] = {}              # in-memory fallback: url → repo_id


def set_pool(pool: Optional[asyncpg.Pool]) -> None:
    """Called by main.py after pool creation."""
    global _pool
    _pool = pool


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CreateRepositoryRequest(BaseModel):
    organization_id: str = Field(..., alias="organizationId")
    name: str
    url: str
    language: str
    created_by: str = Field(..., alias="createdBy")
    visibility: str = "private"
    default_branch: str = Field(default="main", alias="defaultBranch")
    description: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class UpdateRepositoryRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    default_branch: Optional[str] = Field(default=None, alias="defaultBranch")
    visibility: Optional[str] = None
    updated_by: str = Field(..., alias="updatedBy")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"^(https?://|git@)[a-zA-Z0-9._\-/:%@]+(\.git)?$"
)


def _validate_url(url: str) -> bool:
    return bool(_URL_RE.match(url))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: asyncpg.Record) -> dict:
    """Convert an asyncpg Row to a camelCase dict matching the in-memory format."""
    d = dict(row)
    return {
        "repositoryId":  d["repository_id"],
        "organizationId": d["organization_id"],
        "name":          d["name"],
        "url":           d["url"],
        "language":      d["language"],
        "createdBy":     d["created_by"],
        "visibility":    d["visibility"],
        "defaultBranch": d["default_branch"],
        "description":   d.get("description"),
        "status":        d["status"],
        "createdAt":     d["created_at"].isoformat() if d.get("created_at") else None,
        "updatedAt":     d["updated_at"].isoformat() if d.get("updated_at") else None,
        "lastSyncedAt":  d["last_synced_at"].isoformat() if d.get("last_synced_at") else None,
    }


def _make_record(repo_id: str, req: CreateRepositoryRequest) -> dict:
    """Build an in-memory record from a create request (fallback path)."""
    now = _now_iso()
    return {
        "repositoryId":  repo_id,
        "organizationId": req.organization_id,
        "name":          req.name,
        "url":           req.url,
        "language":      req.language,
        "createdBy":     req.created_by,
        "visibility":    req.visibility,
        "defaultBranch": req.default_branch,
        "description":   req.description,
        "status":        "active",
        "createdAt":     now,
        "updatedAt":     now,
        "lastSyncedAt":  None,
    }


# ---------------------------------------------------------------------------
# Service functions — PostgreSQL path
# ---------------------------------------------------------------------------

async def _pg_create(conn: asyncpg.Connection, repo_id: str, req: CreateRepositoryRequest) -> dict:
    row = await conn.fetchrow(
        """
        INSERT INTO repositories
            (repository_id, organization_id, name, url, language,
             created_by, visibility, default_branch, description, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'active')
        RETURNING *
        """,
        repo_id, req.organization_id, req.name, req.url, req.language,
        req.created_by, req.visibility, req.default_branch, req.description,
    )
    return _row_to_dict(row)


async def _pg_get(conn: asyncpg.Connection, repo_id: str) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT * FROM repositories WHERE repository_id = $1", repo_id
    )
    return _row_to_dict(row) if row else None


async def _pg_list(conn: asyncpg.Connection, organization_id: Optional[str]) -> List[dict]:
    if organization_id:
        rows = await conn.fetch(
            "SELECT * FROM repositories WHERE organization_id = $1 ORDER BY created_at DESC",
            organization_id,
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM repositories ORDER BY created_at DESC"
        )
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_repository(req: CreateRepositoryRequest) -> dict:
    """Register a new repository. Raises ValueError on validation failure."""
    if not _validate_url(req.url):
        raise ValueError(f"Invalid repository URL: {req.url!r}")

    repo_id = f"repo_{uuid.uuid4().hex[:12]}"

    if _pool:
        async with _pool.acquire() as conn:
            # Check for duplicate URL
            existing = await conn.fetchval(
                "SELECT repository_id FROM repositories WHERE url = $1", req.url
            )
            if existing:
                raise ValueError(
                    f"Repository already exists with id={existing} for url={req.url!r}"
                )
            record = await _pg_create(conn, repo_id, req)
        logger.info("Repository registered (pg): id=%s name=%s", repo_id, req.name)
        return record

    # In-memory fallback
    if req.url in _url_index:
        existing_id = _url_index[req.url]
        raise ValueError(
            f"Repository already exists with id={existing_id} for url={req.url!r}"
        )
    record = _make_record(repo_id, req)
    _store[repo_id] = record
    _url_index[req.url] = repo_id
    logger.info("Repository registered (mem): id=%s name=%s", repo_id, req.name)
    return record


async def get_repository(repo_id: str) -> Optional[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            return await _pg_get(conn, repo_id)
    return _store.get(repo_id)


async def list_repositories(organization_id: Optional[str] = None) -> List[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            return await _pg_list(conn, organization_id)
    repos = list(_store.values())
    if organization_id:
        repos = [r for r in repos if r["organizationId"] == organization_id]
    return repos


async def update_repository(repo_id: str, req: UpdateRepositoryRequest) -> Optional[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            # Build dynamic SET clause for only the provided fields
            fields, params, idx = [], [], 1

            if req.name is not None:
                fields.append(f"name = ${idx}"); params.append(req.name); idx += 1
            if req.description is not None:
                fields.append(f"description = ${idx}"); params.append(req.description); idx += 1
            if req.default_branch is not None:
                fields.append(f"default_branch = ${idx}"); params.append(req.default_branch); idx += 1
            if req.visibility is not None:
                fields.append(f"visibility = ${idx}"); params.append(req.visibility); idx += 1

            fields.append(f"updated_at = ${idx}"); params.append(datetime.now(timezone.utc)); idx += 1
            params.append(repo_id)

            row = await conn.fetchrow(
                f"UPDATE repositories SET {', '.join(fields)} "
                f"WHERE repository_id = ${idx} RETURNING *",
                *params,
            )
            if row is None:
                return None

            # Determine which fields actually changed
            record = _row_to_dict(row)
            changes = []
            if req.name is not None:        changes.append("name")
            if req.description is not None: changes.append("description")
            if req.default_branch is not None: changes.append("defaultBranch")
            if req.visibility is not None:  changes.append("visibility")

            logger.info("Repository updated (pg): id=%s changes=%s", repo_id, changes)
            return {"record": record, "changes": changes, "updatedBy": req.updated_by}

    # In-memory fallback
    record = _store.get(repo_id)
    if record is None:
        return None

    changes: List[str] = []
    if req.name is not None and req.name != record["name"]:
        record["name"] = req.name; changes.append("name")
    if req.description is not None and req.description != record.get("description"):
        record["description"] = req.description; changes.append("description")
    if req.default_branch is not None and req.default_branch != record["defaultBranch"]:
        record["defaultBranch"] = req.default_branch; changes.append("defaultBranch")
    if req.visibility is not None and req.visibility != record["visibility"]:
        record["visibility"] = req.visibility; changes.append("visibility")

    record["updatedAt"] = _now_iso()
    _store[repo_id] = record
    logger.info("Repository updated (mem): id=%s changes=%s", repo_id, changes)
    return {"record": record, "changes": changes, "updatedBy": req.updated_by}


async def delete_repository(repo_id: str, deleted_by: str) -> Optional[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM repositories WHERE repository_id = $1 RETURNING *", repo_id
            )
            if row is None:
                return None
            record = _row_to_dict(row)
        logger.info("Repository deleted (pg): id=%s by=%s", repo_id, deleted_by)
        return record

    record = _store.pop(repo_id, None)
    if record is None:
        return None
    _url_index.pop(record["url"], None)
    logger.info("Repository deleted (mem): id=%s by=%s", repo_id, deleted_by)
    return record


async def sync_repository(repo_id: str) -> Optional[dict]:
    """Mark a repository as synced (stub — real VCS pull in Phase 2)."""
    if _pool:
        async with _pool.acquire() as conn:
            now = datetime.now(timezone.utc)
            row = await conn.fetchrow(
                """
                UPDATE repositories
                SET last_synced_at = $1, updated_at = $1
                WHERE repository_id = $2
                RETURNING *
                """,
                now, repo_id,
            )
            if row is None:
                return None
            logger.info("Repository synced (pg): id=%s", repo_id)
            return _row_to_dict(row)

    record = _store.get(repo_id)
    if record is None:
        return None
    now = _now_iso()
    record["lastSyncedAt"] = now
    record["updatedAt"] = now
    _store[repo_id] = record
    logger.info("Repository synced (mem): id=%s", repo_id)
    return record
