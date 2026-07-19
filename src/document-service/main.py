"""
Document Service — FastAPI application.

Responsibilities (phases.md §Document Service):
- Listen to RepositoryCreated events
- Extract text from supported formats (Markdown, TXT, HTML)
- Split content into chunks
- Store document metadata (PostgreSQL with in-memory fallback)
- Publish DocumentProcessed events

Storage strategy
----------------
When _pool is set (PostgreSQL available), documents and chunks are persisted
to the `documents` and `document_chunks` tables.  Otherwise the service falls
back to the original in-memory dictionaries with a WARNING log.
"""
import logging
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.database import create_pool, init_schema
from shared.kafka import EventPublisher, EventSubscriber
from shared.models import DocumentProcessedPayload, create_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend state
# ---------------------------------------------------------------------------
_pool: Optional[asyncpg.Pool] = None
_documents: Dict[str, dict] = {}          # in-memory fallback
_chunks: Dict[str, List[dict]] = {}       # in-memory fallback

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
    text = re.sub(r"\*\*|__|\\*|_|~~", "", text)    # bold / italic / strikethrough
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
# Storage helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _save_document(doc_id: str, repo_id: str, org_id: str, processed: dict, file_name: str) -> dict:
    """Persist document + chunks to PostgreSQL or in-memory fallback."""
    record = {
        "documentId":    doc_id,
        "repositoryId":  repo_id,
        "organizationId": org_id,
        "documentType":  processed["type"],
        "fileName":      file_name,
        "wordCount":     processed["word_count"],
        "chunkCount":    len(processed["chunks"]),
        "processedAt":   _now_iso(),
    }

    if _pool:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents
                    (document_id, repository_id, organization_id, document_type,
                     file_name, word_count, chunk_count)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (document_id) DO UPDATE
                    SET word_count=$6, chunk_count=$7, processed_at=NOW()
                """,
                doc_id, repo_id, org_id, processed["type"],
                file_name, processed["word_count"], len(processed["chunks"]),
            )
            # Delete old chunks (idempotent re-processing)
            await conn.execute("DELETE FROM document_chunks WHERE document_id=$1", doc_id)
            if processed["chunks"]:
                await conn.executemany(
                    "INSERT INTO document_chunks (document_id, chunk_index, content) VALUES ($1,$2,$3)",
                    [(doc_id, i, c) for i, c in enumerate(processed["chunks"])],
                )
        logger.info("Document saved (pg): id=%s chunks=%d", doc_id, len(processed["chunks"]))
    else:
        _documents[doc_id] = record
        _chunks[doc_id] = [
            {"chunkIndex": i, "content": c}
            for i, c in enumerate(processed["chunks"])
        ]
        logger.info("Document saved (mem): id=%s chunks=%d", doc_id, len(processed["chunks"]))

    return record


async def _get_document(doc_id: str) -> Optional[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM documents WHERE document_id=$1", doc_id)
            if row is None:
                return None
            d = dict(row)
            return {
                "documentId":    d["document_id"],
                "repositoryId":  d["repository_id"],
                "organizationId": d["organization_id"],
                "documentType":  d["document_type"],
                "fileName":      d["file_name"],
                "wordCount":     d["word_count"],
                "chunkCount":    d["chunk_count"],
                "processedAt":   d["processed_at"].isoformat(),
            }
    return _documents.get(doc_id)


async def _list_documents(repository_id: Optional[str]) -> List[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            if repository_id:
                rows = await conn.fetch(
                    "SELECT * FROM documents WHERE repository_id=$1 ORDER BY processed_at DESC",
                    repository_id,
                )
            else:
                rows = await conn.fetch("SELECT * FROM documents ORDER BY processed_at DESC")
            return [
                {
                    "documentId":    r["document_id"],
                    "repositoryId":  r["repository_id"],
                    "organizationId": r["organization_id"],
                    "documentType":  r["document_type"],
                    "fileName":      r["file_name"],
                    "wordCount":     r["word_count"],
                    "chunkCount":    r["chunk_count"],
                    "processedAt":   r["processed_at"].isoformat(),
                }
                for r in rows
            ]
    docs = list(_documents.values())
    if repository_id:
        docs = [d for d in docs if d["repositoryId"] == repository_id]
    return docs


async def _get_chunks(doc_id: str) -> List[dict]:
    if _pool:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_index, content FROM document_chunks WHERE document_id=$1 ORDER BY chunk_index",
                doc_id,
            )
            return [{"chunkIndex": r["chunk_index"], "content": r["content"]} for r in rows]
    return _chunks.get(doc_id, [])


async def _delete_documents_for_repo(repo_id: str) -> int:
    if _pool:
        async with _pool.acquire() as conn:
            # chunks deleted via CASCADE
            result = await conn.execute(
                "DELETE FROM documents WHERE repository_id=$1", repo_id
            )
            count = int(result.split()[-1])
        return count
    removed = [did for did, d in _documents.items() if d["repositoryId"] == repo_id]
    for did in removed:
        _documents.pop(did, None)
        _chunks.pop(did, None)
    return len(removed)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    if event_type == "RepositoryCreated":
        payload = value.get("payload", {})
        repo_id = payload.get("repositoryId", "")
        org_id  = value.get("organizationId", "")

        # Auto-discover README.md for every registered repository
        readme_content = (
            f"# {payload.get('name', 'Repository')}\n\n"
            f"Language: {payload.get('language', 'Unknown')}\n"
            f"URL: {payload.get('url', '')}\n\n"
            "This is an auto-discovered README document.\n"
        )

        doc_id    = f"doc_{uuid.uuid4().hex[:12]}"
        file_name = "README.md"
        processed = _process_content(readme_content, file_name)

        await _save_document(doc_id, repo_id, org_id, processed, file_name)

        # Phase 2: include per-chunk text in the event so embedding-service
        # can embed each chunk independently (more accurate than textPreview window).
        text_preview   = " ".join(processed["plain_text"].split()[:500])   # Phase 1 fallback
        chunk_previews = [c[:500] for c in processed["chunks"]]            # Phase 2: per-chunk


        out_payload = DocumentProcessedPayload(
            documentId=doc_id,
            repositoryId=repo_id,
            documentType=processed["type"],
            fileName=file_name,
            chunkCount=len(processed["chunks"]),
            wordCount=processed["word_count"],
            textPreview=text_preview,           # Phase 1 fallback preserved
            chunkPreviews=chunk_previews,       # Phase 2: per-chunk previews
        )
        event = create_event(
            event_type="DocumentProcessed",
            aggregate_id=doc_id,
            organization_id=org_id,
            payload=out_payload,
        )
        await publisher.publish("document.processed", event)
        logger.info(
            "DocumentProcessed published: doc_id=%s chunks=%d preview_words=%d chunk_previews=%d",
            doc_id, len(processed["chunks"]), len(text_preview.split()), len(chunk_previews),
        )

    elif event_type == "RepositoryDeleted":
        repo_id = value.get("payload", {}).get("repositoryId", "")
        count = await _delete_documents_for_repo(repo_id)
        logger.info("Removed %d document(s) for deleted repository %s", count, repo_id)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await create_pool()
    if _pool:
        await init_schema(_pool)

    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()
    if _pool:
        await _pool.close()


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
async def list_documents(repository_id: Optional[str] = Query(None, alias="repositoryId")):
    docs = await _list_documents(repository_id)
    return {"data": docs, "total": len(docs)}


@app.get("/documents/{document_id}", tags=["Documents"])
async def get_document(document_id: str):
    doc = await _get_document(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found.")
    return {"data": doc}


@app.get("/documents/{document_id}/chunks", tags=["Documents"])
async def get_chunks(document_id: str):
    doc = await _get_document(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found.")
    chunks = await _get_chunks(document_id)
    return {"data": chunks, "total": len(chunks)}


class UploadDocumentRequest(BaseModel):
    repository_id: str = Field(..., alias="repositoryId")
    organization_id: str = Field(..., alias="organizationId")
    file_name: str = Field(..., alias="fileName")
    content: str

    model_config = ConfigDict(populate_by_name=True)


@app.post("/documents", status_code=201, tags=["Documents"])
async def upload_document(req: UploadDocumentRequest):
    """Manually upload a document for processing."""
    doc_id    = f"doc_{uuid.uuid4().hex[:12]}"
    processed = _process_content(req.content, req.file_name)

    record = await _save_document(
        doc_id, req.repository_id, req.organization_id, processed, req.file_name
    )

    text_preview   = " ".join(processed["plain_text"].split()[:500])
    chunk_previews = [c[:500] for c in processed["chunks"]]  # Phase 2: per-chunk previews

    out_payload = DocumentProcessedPayload(
        documentId=doc_id,
        repositoryId=req.repository_id,
        documentType=processed["type"],
        fileName=req.file_name,
        chunkCount=len(processed["chunks"]),
        wordCount=processed["word_count"],
        textPreview=text_preview,         # Phase 1 fallback
        chunkPreviews=chunk_previews,     # Phase 2: accurate per-chunk text
    )
    event = create_event(
        event_type="DocumentProcessed",
        aggregate_id=doc_id,
        organization_id=req.organization_id,
        payload=out_payload,
    )
    await publisher.publish("document.processed", event)

    return {"data": record, "eventId": str(event.event_id)}
