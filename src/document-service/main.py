import sys
import os
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.models import DocumentationUploadedPayload, create_event
from shared.kafka import EventPublisher, EventSubscriber

publisher = EventPublisher()
subscriber = EventSubscriber(group_id="document-service-group", topics=["repository.created"])

async def handle_event(topic: str, value: dict):
    print(f"[Document Service] Received event on {topic}: {value['eventId']}")
    
    # Simulate processing repository and finding a README
    repo_id = value['payload']['repositoryId']
    org_id = value['organizationId']
    
    print(f"[Document Service] Scanning repository {repo_id} for documentation...")
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"
    
    # Publish DocumentationUploaded
    payload = DocumentationUploadedPayload(
        documentId=doc_id,
        repositoryId=repo_id,
        documentType="README",
        fileName="README.md",
        uploadedBy="system"
    )
    
    out_event = create_event(
        event_type="DocumentationUploaded",
        aggregate_id=doc_id,
        organization_id=org_id,
        payload=payload,
        correlation_id=uuid.UUID(value['correlationId'])
    )
    
    await publisher.publish("documentation.uploaded", out_event)
    print(f"[Document Service] Published DocumentationUploaded for doc {doc_id}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()

app = FastAPI(title="Document Service API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "document-service"}
