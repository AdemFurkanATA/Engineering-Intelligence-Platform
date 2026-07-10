"""
Document Service — FastAPI application.

Responsibilities (phases.md §Document Service):
- Listen to RepositoryCreated events
- Extract text from supported formats (Markdown, TXT, HTML)
- Split content into chunks
- Store document metadata (in-memory for MVP)
- Publish DocumentProcessed events
"""
import logging
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import DocumentProcessedPayload, create_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory document store
# ---------------------------------------------------------------------------
_documents: Dict[str, dict] = {}
_chunks: Dict[str, List[dict]] = {}  # doc_id → list of chunks

publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="document-service-group",
    topics=["repository.created", "repository.deleted"],
)


# ---------------------------------------------------------------------------
# Text processing helpers
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """Remove common Markdown syntax to get plain text."""
    text = re.sub(r"#+\s*", "", text)               # headings
    text = re.sub(r"\*\*|__|\*|_|~~", "", text)    # bold / italic / strikethrough
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)  # inline code / code blocks
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)  # links
    return text.strip()


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """Split text into overlapping chunks by word count."""
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


def _process_content(content: str, file_name: str) -> dict:
    """Extract and chunk content based on file type."""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "txt"
    if ext in ("md", "markdown"):
        plain = _strip_markdown(content)
        doc_type = "MARKDOWN"
    elif ext == "html":
        plain = re.sub(r"<[^>]+>", " ", content)
        doc_type = "HTML"
    else:
        plain = content
        doc_type = "TEXT"

    chunks = _chunk_text(plain)
    word_count = len(plain.split())
    return {"type": doc_type, "chunks": chunks, "word_count": word_count, "plain_text": plain}


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    if event_type == "RepositoryCreated":
        payload = value.get("payload", {})
        repo_id = payload.get("repositoryId", "")
        org_id = value.get("organizationId", "")

        # Simulate discovering a README.md for every registered repository
        readme_content = (
            f"# {payload.get('name', 'Repository')}\n\n"
            f"Language: {payload.get('language', 'Unknown')}\n"
            f"URL: {payload.get('url', '')}\n\n"
            "This is an auto-discovered README document.\n"
        )

        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        file_name = "README.md"
        processed = _process_content(readme_content, file_name)

        _documents[doc_id] = {
            "documentId": doc_id,
            "repositoryId": repo_id,
            "organizationId": org_id,
            "documentType": processed["type"],
            "fileName": file_name,
            "wordCount": processed["word_count"],
            "chunkCount": len(processed["chunks"]),
            "processedAt": datetime.utcnow().isoformat() + "Z",
        }
        _chunks[doc_id] = [
            {"chunkIndex": i, "content": c}
            for i, c in enumerate(processed["chunks"])
        ]

        out_payload = DocumentProcessedPayload(
            documentId=doc_id,
            repositoryId=repo_id,
            documentType=processed["type"],
            fileName=file_name,
            chunkCount=len(processed["chunks"]),
            wordCount=processed["word_count"],
        )
        event = create_event(
            event_type="DocumentProcessed",
            aggregate_id=doc_id,
            organization_id=org_id,
            payload=out_payload,
            correlation_id=None,
        )
        await publisher.publish("document.processed", event)
        logger.info("DocumentProcessed published: doc_id=%s chunks=%d", doc_id, len(processed["chunks"]))

    elif event_type == "RepositoryDeleted":
        repo_id = value.get("payload", {}).get("repositoryId", "")
        removed = [did for did, d in _documents.items() if d["repositoryId"] == repo_id]
        for did in removed:
            _documents.pop(did, None)
            _chunks.pop(did, None)
        logger.info("Removed %d document(s) for deleted repository %s", len(removed), repo_id)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Document Service",
    description="Extracts, chunks and indexes engineering documents.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "document-service"}


@app.get("/documents", tags=["Documents"])
def list_documents(repository_id: Optional[str] = Query(None, alias="repositoryId")):
    docs = list(_documents.values())
    if repository_id:
        docs = [d for d in docs if d["repositoryId"] == repository_id]
    return {"data": docs, "total": len(docs)}


@app.get("/documents/{document_id}", tags=["Documents"])
def get_document(document_id: str):
    doc = _documents.get(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found.")
    return {"data": doc}


@app.get("/documents/{document_id}/chunks", tags=["Documents"])
def get_chunks(document_id: str):
    if document_id not in _documents:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found.")
    return {"data": _chunks.get(document_id, []), "total": len(_chunks.get(document_id, []))}


class UploadDocumentRequest(BaseModel):
    repository_id: str = Field(..., alias="repositoryId")
    organization_id: str = Field(..., alias="organizationId")
    file_name: str = Field(..., alias="fileName")
    content: str

    model_config = ConfigDict(populate_by_name=True)


@app.post("/documents", status_code=201, tags=["Documents"])
async def upload_document(req: UploadDocumentRequest):
    """Manually upload a document for processing."""
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    processed = _process_content(req.content, req.file_name)

    _documents[doc_id] = {
        "documentId": doc_id,
        "repositoryId": req.repository_id,
        "organizationId": req.organization_id,
        "documentType": processed["type"],
        "fileName": req.file_name,
        "wordCount": processed["word_count"],
        "chunkCount": len(processed["chunks"]),
        "processedAt": datetime.utcnow().isoformat() + "Z",
    }
    _chunks[doc_id] = [
        {"chunkIndex": i, "content": c}
        for i, c in enumerate(processed["chunks"])
    ]

    out_payload = DocumentProcessedPayload(
        documentId=doc_id,
        repositoryId=req.repository_id,
        documentType=processed["type"],
        fileName=req.file_name,
        chunkCount=len(processed["chunks"]),
        wordCount=processed["word_count"],
    )
    event = create_event(
        event_type="DocumentProcessed",
        aggregate_id=doc_id,
        organization_id=req.organization_id,
        payload=out_payload,
    )
    await publisher.publish("document.processed", event)

    return {"data": _documents[doc_id], "eventId": str(event.event_id)}
