import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.kafka import EventSubscriber

subscriber = EventSubscriber(
    group_id="graph-service-group", 
    topics=["repository.created", "documentation.uploaded"]
)

async def handle_event(topic: str, value: dict):
    event_type = value.get("eventType")
    print(f"[Graph Service] Processing {event_type} event: {value['eventId']}")
    
    if event_type == "RepositoryCreated":
        repo_id = value['payload']['repositoryId']
        print(f"[Graph Service] Created node (Repository {{id: '{repo_id}'}})")
    
    elif event_type == "DocumentationUploaded":
        doc_id = value['payload']['documentId']
        repo_id = value['payload']['repositoryId']
        print(f"[Graph Service] Created node (Document {{id: '{doc_id}'}})")
        print(f"[Graph Service] Created relationship (Document)-[:DESCRIBES]->(Repository)")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()

app = FastAPI(title="Graph Service API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "graph-service"}
