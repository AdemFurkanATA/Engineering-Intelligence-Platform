import sys
import os
import uuid
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.models import EmbeddingGeneratedPayload, create_event
from shared.kafka import EventPublisher, EventSubscriber

publisher = EventPublisher()
subscriber = EventSubscriber(group_id="embedding-service-group", topics=["documentation.uploaded"])

async def handle_event(topic: str, value: dict):
    print(f"[Embedding Service] Received event on {topic}: {value['eventId']}")
    
    doc_id = value['payload']['documentId']
    repo_id = value['payload']['repositoryId']
    org_id = value['organizationId']
    
    print(f"[Embedding Service] Generating embeddings for document {doc_id}...")
    await asyncio.sleep(1) # Simulate AI processing time
    
    # Publish EmbeddingGenerated
    payload = EmbeddingGeneratedPayload(
        documentId=doc_id,
        repositoryId=repo_id,
        chunksProcessed=5
    )
    
    out_event = create_event(
        event_type="EmbeddingGenerated",
        aggregate_id=doc_id,
        organization_id=org_id,
        payload=payload,
        correlation_id=uuid.UUID(value['correlationId'])
    )
    
    await publisher.publish("embedding.generated", out_event)
    print(f"[Embedding Service] Published EmbeddingGenerated for doc {doc_id}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()

app = FastAPI(title="Embedding Service API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "embedding-service"}
