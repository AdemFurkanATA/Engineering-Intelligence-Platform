from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, ConfigDict

T = TypeVar("T")

class EventEnvelope(BaseModel, Generic[T]):
    event_id: UUID = Field(default_factory=uuid4, alias="eventId")
    event_type: str = Field(..., alias="eventType")
    event_version: int = Field(default=1, alias="eventVersion")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: UUID = Field(default_factory=uuid4, alias="correlationId")
    aggregate_id: str = Field(..., alias="aggregateId")
    organization_id: str = Field(..., alias="organizationId")
    payload: T

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={
            datetime: lambda v: v.isoformat() + "Z"
        }
    )

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


class DocumentationUploadedPayload(BaseModel):
    document_id: str = Field(..., alias="documentId")
    repository_id: str = Field(..., alias="repositoryId")
    document_type: str = Field(..., alias="documentType")
    file_name: str = Field(..., alias="fileName")
    uploaded_by: str = Field(..., alias="uploadedBy")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, alias="uploadedAt")

    model_config = ConfigDict(populate_by_name=True)


class EmbeddingGeneratedPayload(BaseModel):
    document_id: str = Field(..., alias="documentId")
    repository_id: str = Field(..., alias="repositoryId")
    chunks_processed: int = Field(..., alias="chunksProcessed")
    generated_at: datetime = Field(default_factory=datetime.utcnow, alias="generatedAt")

    model_config = ConfigDict(populate_by_name=True)


# Helper function to create an event
def create_event(
    event_type: str,
    aggregate_id: str,
    organization_id: str,
    payload: BaseModel,
    correlation_id: UUID = None
) -> EventEnvelope:
    return EventEnvelope(
        eventType=event_type,
        aggregateId=aggregate_id,
        organizationId=organization_id,
        correlationId=correlation_id or uuid4(),
        payload=payload
    )
