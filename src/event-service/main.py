from fastapi import FastAPI

app = FastAPI(title="Event Service API")

# Mock topics for MVP
TOPICS = [
    "repository.created",
    "repository.updated",
    "documentation.uploaded",
    "embedding.generated",
]

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "event-service"}

@app.get("/events/topics")
def list_topics():
    return {"topics": TOPICS}
