# Engineering Intelligence Platform

> **Phase 1 — Living Knowledge** | Event-Driven Microservices | FastAPI · Kafka · Neo4j · Qdrant

A distributed, event-driven platform that continuously collects, connects, and organizes engineering knowledge into a **Living Knowledge Graph** — enabling intelligent search, architectural analysis, and engineering intelligence at scale.

---

## Architecture

```
                      ┌───────────────┐
  External Clients ──▶│  API Gateway  │:8000
                      └───────┬───────┘
                              │  HTTP Reverse Proxy + Rate Limiting
          ┌───────────────────┼───────────────────────────────┐
          │                   │                               │
   ┌──────▼──────┐    ┌───────▼───────┐            ┌─────────▼──────────┐
   │Auth Service │    │  Repository   │            │   Event Service    │
   │   :8001     │    │  Service :8002│            │      :8007         │
   └─────────────┘    └───────┬───────┘            └────────────────────┘
                              │ RepositoryCreated
                              ▼
                         ┌──────────┐
                         │  KAFKA   │  (Event Bus)
                         └────┬─────┘
           ┌──────────────────┼──────────────────┐
           │                  │                  │
  ┌────────▼──────┐  ┌────────▼──────┐  ┌────────▼──────┐
  │Document Svc   │  │  Graph Svc    │  │  Search Svc   │
  │   :8003       │  │   :8005       │  │   :8006       │
  └───────┬───────┘  └───────────────┘  └───────────────┘
          │ DocumentProcessed
          ▼
  ┌───────────────┐
  │Embedding Svc  │
  │   :8004       │
  └───────────────┘
```

### Databases (via Docker Compose)
| Database    | Purpose                        | Port  |
|-------------|--------------------------------|-------|
| PostgreSQL  | Relational metadata            | 5432  |
| Neo4j       | Knowledge Graph                | 7687  |
| Qdrant      | Vector embeddings              | 6333  |
| Redis       | Caching & rate limiting        | 6379  |
| Kafka       | Event streaming                | 9092  |

---

## Services

| Service            | Port | Description                                           |
|--------------------|------|-------------------------------------------------------|
| `api-gateway`      | 8000 | Reverse proxy, rate limiting, CORS, health aggregation |
| `auth-service`     | 8001 | JWT auth, OAuth2 password flow, user management      |
| `repository-service`| 8002 | Repository CRUD, validation, event publishing        |
| `document-service` | 8003 | Text extraction, chunking, document management       |
| `embedding-service`| 8004 | Vector generation, embedding store management        |
| `graph-service`    | 8005 | Knowledge graph nodes and relationships              |
| `search-service`   | 8006 | BM25 + semantic hybrid search                        |
| `event-service`    | 8007 | Event catalog, observability log                     |

---

## Getting Started

### Prerequisites
- Python 3.8+
- Docker Desktop (for infrastructure)

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start Infrastructure (Kafka, Neo4j, Qdrant, PostgreSQL, Redis)

```bash
# Make sure Docker Desktop is running first
make up

# Or directly:
docker compose up -d
```

### 3. Start All Services

```powershell
.\start_all.ps1
```

Or start services individually:

```bash
# Set Python path so shared library is importable
$env:PYTHONPATH = "src"  # PowerShell
# export PYTHONPATH=src  # bash

# In separate terminals:
uvicorn main:app --port 8001 --app-dir src/auth-service
uvicorn main:app --port 8002 --app-dir src/repository-service
uvicorn main:app --port 8003 --app-dir src/document-service
uvicorn main:app --port 8004 --app-dir src/embedding-service
uvicorn main:app --port 8005 --app-dir src/graph-service
uvicorn main:app --port 8006 --app-dir src/search-service
uvicorn main:app --port 8007 --app-dir src/event-service
uvicorn main:app --port 8000 --app-dir src/api-gateway
```

> **Note:** Services start cleanly even without Kafka/Docker. When Kafka is unavailable, events are logged locally (no crash). This makes local development without Docker fully functional.

---

## API Reference

All public APIs are accessible via the **API Gateway** (`http://localhost:8000`).

### URL Format
```
http://localhost:8000/api/v1/{service}/{path}
```

### Authentication
```bash
# Get a JWT token
curl -X POST http://localhost:8001/auth/token \
  -d "username=admin&password=admin"

# Default credentials:
#   admin / admin   (role: admin)
#   engineer / engineer  (role: engineer)
```

### Register a Repository
```bash
curl -X POST http://localhost:8000/api/v1/repository/repositories \
  -H "Content-Type: application/json" \
  -d '{
    "organizationId": "org_001",
    "name": "payment-service",
    "url": "https://github.com/company/payment-service",
    "language": "Go",
    "createdBy": "user_42"
  }'
```

### List Repositories
```bash
curl http://localhost:8000/api/v1/repository/repositories
curl http://localhost:8000/api/v1/repository/repositories?organizationId=org_001
```

### Search
```bash
curl "http://localhost:8000/api/v1/search/search?q=payment&type=Repository"
```

### View Knowledge Graph
```bash
curl http://localhost:8000/api/v1/graph/graph/nodes
curl http://localhost:8000/api/v1/graph/graph/stats
curl http://localhost:8000/api/v1/graph/graph/relationships
```

### Upload a Document
```bash
curl -X POST http://localhost:8000/api/v1/document/documents \
  -H "Content-Type: application/json" \
  -d '{
    "repositoryId": "repo_xxx",
    "organizationId": "org_001",
    "fileName": "README.md",
    "content": "# My Service\n\nThis service handles payments."
  }'
```

### View Event Catalog
```bash
curl http://localhost:8000/api/v1/event/events/topics
curl http://localhost:8000/api/v1/event/events/log
```

### Health Checks
```bash
curl http://localhost:8000/health           # Gateway health
curl http://localhost:8000/health/all       # All services
```

---

## Event Chain (Phase 1)

When a repository is registered, the following event chain fires automatically (when Kafka is running):

```
POST /repositories
    → RepositoryCreated         (repository.created)
        → DocumentProcessed     (document.processed)   ← Document Service
            → EmbeddingGenerated (embedding.generated)  ← Embedding Service
        → GraphUpdated          (graph.updated)         ← Graph Service
        → Search Index Updated                          ← Search Service
```

---

## Environment Variables

| Variable                  | Default                    | Description               |
|---------------------------|----------------------------|---------------------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092`           | Kafka broker address      |
| `JWT_SECRET_KEY`          | `changeme-super-secret-key-for-development` | JWT signing key |
| `JWT_EXPIRE_MINUTES`      | `60`                       | Token expiry              |
| `NEO4J_URI`               | `bolt://localhost:7687`    | Neo4j connection          |
| `QDRANT_URL`              | `http://localhost:6333`    | Qdrant connection         |
| `POSTGRES_DSN`            | `postgresql://...`         | PostgreSQL connection     |
| `RATE_LIMIT_PER_MINUTE`   | `120`                      | Gateway rate limit        |

---

## Project Structure

```
├── docs/                     # Architecture documentation
│   ├── phases.md             # Development roadmap
│   ├── SYSTEM_ARCHITECTURE.md
│   ├── EVENT_CATALOG.md      # All Kafka event definitions
│   ├── DATABASE_DESIGN.md
│   ├── FUNCTIONAL_REQUIREMENTS.md
│   └── ...
├── src/
│   ├── shared/               # Shared library (models, kafka, config)
│   │   ├── models.py         # Pydantic event models (EventEnvelope, payloads)
│   │   ├── kafka.py          # EventPublisher & EventSubscriber
│   │   └── config.py         # Centralised env vars
│   ├── api-gateway/          # Reverse proxy entry point
│   ├── auth-service/         # JWT authentication
│   ├── repository-service/   # Repository management
│   ├── document-service/     # Document processing
│   ├── embedding-service/    # Vector embeddings
│   ├── graph-service/        # Knowledge Graph
│   ├── search-service/       # Hybrid search
│   └── event-service/        # Event catalog & observability
├── docker-compose.yml        # Infrastructure (Kafka, Neo4j, Qdrant, Postgres, Redis)
├── Makefile                  # Lifecycle shortcuts (make up, make down)
├── requirements.txt          # Python dependencies
└── start_all.ps1             # Start all services locally
```

---

## Phase Roadmap

| Phase | Name | Status |
|-------|------|--------|
| **Phase 1** | Living Knowledge | ✅ In Progress |
| Phase 2 | Engineering Intelligence | 📋 Planned |
| Phase 3 | Autonomous Engineering Intelligence | 📋 Planned |

See [`docs/phases.md`](docs/phases.md) for the full roadmap.

---

## Interactive API Docs

Every service exposes Swagger UI at `/docs`:

- Gateway: http://localhost:8000/docs
- Auth: http://localhost:8001/docs
- Repository: http://localhost:8002/docs
- Document: http://localhost:8003/docs
- Embedding: http://localhost:8004/docs
- Graph: http://localhost:8005/docs
- Search: http://localhost:8006/docs
- Event: http://localhost:8007/docs
