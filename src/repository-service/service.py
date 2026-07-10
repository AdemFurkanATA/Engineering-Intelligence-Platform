"""
Repository Service — business logic layer.

Responsible for:
- Repository registration (FR-101)
- Repository metadata management (FR-120)
- URL validation (FR-103)
- Duplicate rejection (FR-101)
- Publishing domain events to Kafka (EVENT_CATALOG.md §2)

For MVP, persistence is in-memory.  Replace `_store` with a real database
client (asyncpg / SQLAlchemy) without changing the public API.
"""
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory store (MVP — swap for Postgres in production)
# ---------------------------------------------------------------------------
_store: Dict[str, dict] = {}          # repo_id → repo record
_url_index: Dict[str, str] = {}       # url → repo_id  (for dedup)


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


def _make_record(repo_id: str, req: CreateRepositoryRequest) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    return {
        "repositoryId": repo_id,
        "organizationId": req.organization_id,
        "name": req.name,
        "url": req.url,
        "language": req.language,
        "createdBy": req.created_by,
        "visibility": req.visibility,
        "defaultBranch": req.default_branch,
        "description": req.description,
        "status": "active",
        "createdAt": now,
        "updatedAt": now,
        "lastSyncedAt": None,
    }


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

async def create_repository(req: CreateRepositoryRequest) -> dict:
    """Register a new repository. Raises ValueError on validation failure."""
    if not _validate_url(req.url):
        raise ValueError(f"Invalid repository URL: {req.url!r}")

    if req.url in _url_index:
        existing_id = _url_index[req.url]
        raise ValueError(
            f"Repository already exists with id={existing_id} for url={req.url!r}"
        )

    repo_id = f"repo_{uuid.uuid4().hex[:12]}"
    record = _make_record(repo_id, req)
    _store[repo_id] = record
    _url_index[req.url] = repo_id

    logger.info("Repository registered: id=%s name=%s", repo_id, req.name)
    return record


async def get_repository(repo_id: str) -> Optional[dict]:
    return _store.get(repo_id)


async def list_repositories(organization_id: Optional[str] = None) -> List[dict]:
    repos = list(_store.values())
    if organization_id:
        repos = [r for r in repos if r["organizationId"] == organization_id]
    return repos


async def update_repository(repo_id: str, req: UpdateRepositoryRequest) -> Optional[dict]:
    record = _store.get(repo_id)
    if record is None:
        return None

    changes: List[str] = []
    if req.name is not None and req.name != record["name"]:
        record["name"] = req.name
        changes.append("name")
    if req.description is not None and req.description != record.get("description"):
        record["description"] = req.description
        changes.append("description")
    if req.default_branch is not None and req.default_branch != record["defaultBranch"]:
        record["defaultBranch"] = req.default_branch
        changes.append("defaultBranch")
    if req.visibility is not None and req.visibility != record["visibility"]:
        record["visibility"] = req.visibility
        changes.append("visibility")

    record["updatedAt"] = datetime.utcnow().isoformat() + "Z"
    _store[repo_id] = record

    logger.info("Repository updated: id=%s changes=%s", repo_id, changes)
    return {"record": record, "changes": changes, "updatedBy": req.updated_by}


async def delete_repository(repo_id: str, deleted_by: str) -> Optional[dict]:
    record = _store.pop(repo_id, None)
    if record is None:
        return None
    _url_index.pop(record["url"], None)
    logger.info("Repository deleted: id=%s by=%s", repo_id, deleted_by)
    return record


async def sync_repository(repo_id: str) -> Optional[dict]:
    """Mark a repository as synced (stub — real impl would pull from VCS)."""
    record = _store.get(repo_id)
    if record is None:
        return None
    record["lastSyncedAt"] = datetime.utcnow().isoformat() + "Z"
    record["updatedAt"] = record["lastSyncedAt"]
    _store[repo_id] = record
    logger.info("Repository synced: id=%s", repo_id)
    return record
