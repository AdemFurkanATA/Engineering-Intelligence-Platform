from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from service import CreateRepositoryRequest, register_repository, publisher
import traceback

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect Kafka Producer
    await publisher.start()
    yield
    # Shutdown: Disconnect Kafka Producer
    await publisher.stop()

app = FastAPI(title="Repository Service API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "repository-service"}

@app.post("/repositories")
async def create_repository(req: CreateRepositoryRequest):
    try:
        result = await register_repository(req)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
