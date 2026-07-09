import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.kafka import EventSubscriber

subscriber = EventSubscriber(
    group_id="search-service-group", 
    topics=["repository.created", "documentation.uploaded", "embedding.generated"]
)

async def handle_event(topic: str, value: dict):
    event_type = value.get("eventType")
    print(f"[Search Service] Processing {event_type} event: {value['eventId']}")
    
    if event_type == "RepositoryCreated":
        repo_id = value['payload']['repositoryId']
        print(f"[Search Service] Added repository {repo_id} to search index.")
    
    elif event_type == "DocumentationUploaded":
        doc_id = value['payload']['documentId']
        print(f"[Search Service] Added document {doc_id} to full-text search index.")
        
    elif event_type == "EmbeddingGenerated":
        doc_id = value['payload']['documentId']
        print(f"[Search Service] Updated document {doc_id} with vector embeddings for hybrid search.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()

app = FastAPI(title="Search Service API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "search-service"}
