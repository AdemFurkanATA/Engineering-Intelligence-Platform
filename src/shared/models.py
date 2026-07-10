"""
Shared Pydantic models for the Engineering Intelligence Platform.
All events follow the EventEnvelope standard defined in EVENT_CATALOG.md.
"""
from datetime import datetime
from typing import Any, Optional, List
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Core Event Envelope
# ---------------------------------------------------------------------------

class EventEnvelope(BaseModel):
    """Standard event wrapper for all domain events (see EVENT_CATALOG.md §1.6)."""

    event_id: UUID = Field(default_factory=uuid4, alias="eventId")
    event_type: str = Field(..., alias="eventType")
    event_version: int = Field(default=1, alias="eventVersion")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: UUID = Field(default_factory=uuid4, alias="correlationId")
    aggregate_id: str = Field(..., alias="aggregateId")
    organization_id: str = Field(..., alias="organizationId")
    payload: Any

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Repository Event Payloads
# ---------------------------------------------------------------------------

class RepositoryCreatedPayload(BaseModel):
    repository_id: str = Field(..., alias="repositoryId")
    organization_id: str = Field(..., alias="organizationId")
    name: str
    url: str
    default_branch: str = Field(default="main", alias="defaultBranch")
    language: str
    visibility: str = "private"
    created_by: str = Field(..., alias="createdBy")
    created_at: datetime = Field(default_factory=datetime.utcnow, alias="createdAt")

    model_config = ConfigDict(populate_by_name=True)


class RepositoryUpdatedPayload(BaseModel):
    repository_id: str = Field(..., alias="repositoryId")
    changes: List[str]
    updated_by: str = Field(..., alias="updatedBy")
    updated_at: datetime = Field(default_factory=datetime.utcnow, alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class RepositoryDeletedPayload(BaseModel):
    repository_id: str = Field(..., alias="repositoryId")
    deleted_by: str = Field(..., alias="deletedBy")
    deleted_at: datetime = Field(default_factory=datetime.utcnow, alias="deletedAt")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Document Event Payloads
# ---------------------------------------------------------------------------

class DocumentProcessedPayload(BaseModel):
    document_id: str = Field(..., alias="documentId")
    repository_id: str = Field(..., alias="repositoryId")
    document_type: str = Field(..., alias="documentType")
    file_name: str = Field(..., alias="fileName")
    chunk_count: int = Field(..., alias="chunkCount")
    word_count: int = Field(..., alias="wordCount")
    processed_by: str = Field(default="system", alias="processedBy")
    processed_at: datetime = Field(default_factory=datetime.utcnow, alias="processedAt")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Embedding Event Payloads
# ---------------------------------------------------------------------------

class EmbeddingGeneratedPayload(BaseModel):
    document_id: str = Field(..., alias="documentId")
    repository_id: str = Field(..., alias="repositoryId")
    chunks_processed: int = Field(..., alias="chunksProcessed")
    model_name: str = Field(default="stub-embedder-v1", alias="modelName")
    generated_at: datetime = Field(default_factory=datetime.utcnow, alias="generatedAt")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Graph Event Payloads
# ---------------------------------------------------------------------------

class GraphUpdatedPayload(BaseModel):
    trigger_event: str = Field(..., alias="triggerEvent")
    nodes_created: int = Field(default=0, alias="nodesCreated")
    relationships_created: int = Field(default=0, alias="relationshipsCreated")
    updated_at: datetime = Field(default_factory=datetime.utcnow, alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Search Event Payloads
# ---------------------------------------------------------------------------

class SearchIndexedPayload(BaseModel):
    entity_id: str = Field(..., alias="entityId")
    entity_type: str = Field(..., alias="entityType")
    indexed_at: datetime = Field(default_factory=datetime.utcnow, alias="indexedAt")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def create_event(
    event_type: str,
    aggregate_id: str,
    organization_id: str,
    payload: BaseModel,
    correlation_id: Optional[UUID] = None,
) -> EventEnvelope:
    """Wrap a payload in a standard EventEnvelope."""
    return EventEnvelope(
        eventType=event_type,
        aggregateId=aggregate_id,
        organizationId=organization_id,
        correlationId=correlation_id or uuid4(),
        payload=payload.model_dump(by_alias=True),
    )
