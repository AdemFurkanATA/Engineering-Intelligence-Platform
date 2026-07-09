import uuid
from datetime import datetime
from pydantic import BaseModel, Field
import sys
import os

# Add parent directory to path so we can import shared
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.models import RepositoryCreatedPayload, create_event
from shared.kafka import EventPublisher

publisher = EventPublisher()

class CreateRepositoryRequest(BaseModel):
    organization_id: str = Field(..., alias="organizationId")
    name: str
    url: str
    language: str
    created_by: str = Field(..., alias="createdBy")
    visibility: str = "private"
    default_branch: str = Field(default="main", alias="defaultBranch")

async def register_repository(req: CreateRepositoryRequest) -> dict:
    # 1. Generate unique IDs
    repo_id = f"repo_{uuid.uuid4().hex[:8]}"

    # 2. Emulate saving to transactional database...
    print(f"Saving repository {repo_id} to database...")
    
    # 3. Create Event Payload
    payload = RepositoryCreatedPayload(
        repositoryId=repo_id,
        organizationId=req.organization_id,
        name=req.name,
        url=req.url,
        defaultBranch=req.default_branch,
        language=req.language,
        visibility=req.visibility,
        createdBy=req.created_by,
        createdAt=datetime.utcnow()
    )

    # 4. Wrap in Event Envelope
    event = create_event(
        event_type="RepositoryCreated",
        aggregate_id=repo_id,
        organization_id=req.organization_id,
        payload=payload
    )

    # 5. Publish Event to Kafka
    topic = "repository.created"
    await publisher.publish(topic, event)
    print(f"Published event to topic {topic}: {event.event_id}")

    return {
        "status": "success",
        "repositoryId": repo_id,
        "eventId": str(event.event_id)
    }
