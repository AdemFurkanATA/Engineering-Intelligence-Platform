"""
Repository Service — FastAPI application entry point.

Exposes REST APIs for repository management (FR-101, FR-110, FR-120).
Publishes domain events to Kafka on every state change.
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher
from shared.models import (
    RepositoryCreatedPayload,
    RepositoryUpdatedPayload,
    RepositoryDeletedPayload,
    create_event,
)
from service import (
    CreateRepositoryRequest,
    UpdateRepositoryRequest,
    create_repository,
    delete_repository,
    get_repository,
    list_repositories,
    sync_repository,
    update_repository,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

publisher = EventPublisher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    yield
    await publisher.stop()


app = FastAPI(
    title="Repository Service",
    description="Manages software repository lifecycle and publishes domain events.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "repository-service"}


# ---------------------------------------------------------------------------
# Repository CRUD
# ---------------------------------------------------------------------------

@app.post("/repositories", status_code=201, tags=["Repositories"])
async def register_repository(req: CreateRepositoryRequest):
    """FR-101 — Register a new repository and publish RepositoryCreated event."""
    try:
        record = await create_repository(req)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    payload = RepositoryCreatedPayload(
        repositoryId=record["repositoryId"],
        organizationId=record["organizationId"],
        name=record["name"],
        url=record["url"],
        defaultBranch=record["defaultBranch"],
        language=record["language"],
        visibility=record["visibility"],
        createdBy=record["createdBy"],
    )
    event = create_event(
        event_type="RepositoryCreated",
        aggregate_id=record["repositoryId"],
        organization_id=record["organizationId"],
        payload=payload,
    )
    await publisher.publish("repository.created", event)

    return {"data": record, "eventId": str(event.event_id)}


@app.get("/repositories", tags=["Repositories"])
async def list_repos(organization_id: Optional[str] = Query(None, alias="organizationId")):
    """List all registered repositories, optionally filtered by organization."""
    repos = await list_repositories(organization_id)
    return {"data": repos, "total": len(repos)}


@app.get("/repositories/{repo_id}", tags=["Repositories"])
async def get_repo(repo_id: str):
    """Get a single repository by ID."""
    record = await get_repository(repo_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repo_id}' not found.")
    return {"data": record}


@app.put("/repositories/{repo_id}", tags=["Repositories"])
async def update_repo(repo_id: str, req: UpdateRepositoryRequest):
    """Update repository metadata and publish RepositoryUpdated event."""
    result = await update_repository(repo_id, req)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repo_id}' not found.")

    record = result["record"]
    org_id = record["organizationId"]
    payload = RepositoryUpdatedPayload(
        repositoryId=repo_id,
        changes=result["changes"],
        updatedBy=result["updatedBy"],
    )
    event = create_event(
        event_type="RepositoryUpdated",
        aggregate_id=repo_id,
        organization_id=org_id,
        payload=payload,
    )
    await publisher.publish("repository.updated", event)
    return {"data": record, "eventId": str(event.event_id)}


@app.delete("/repositories/{repo_id}", tags=["Repositories"])
async def remove_repo(repo_id: str, deleted_by: str = Query(..., alias="deletedBy")):
    """Delete a repository and publish RepositoryDeleted event."""
    record = await delete_repository(repo_id, deleted_by)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repo_id}' not found.")

    payload = RepositoryDeletedPayload(
        repositoryId=repo_id,
        deletedBy=deleted_by,
    )
    event = create_event(
        event_type="RepositoryDeleted",
        aggregate_id=repo_id,
        organization_id=record["organizationId"],
        payload=payload,
    )
    await publisher.publish("repository.deleted", event)
    return {"message": "Repository deleted.", "eventId": str(event.event_id)}


@app.post("/repositories/{repo_id}/sync", tags=["Repositories"])
async def trigger_sync(repo_id: str):
    """FR-110 — Manually trigger repository synchronization."""
    record = await sync_repository(repo_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repo_id}' not found.")
    return {"data": record, "message": "Synchronization triggered."}
