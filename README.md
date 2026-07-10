# Engineering Intelligence Platform

> **Phase 1 — Living Knowledge** | Event-Driven · Knowledge Graph · Autonomous Engineering Intelligence

Engineering Intelligence Platform is a goal-oriented platform that continuously transforms repositories, documentation, and engineering artifacts into an **evolving organizational knowledge graph** — capable of supporting autonomous engineering intelligence.

While most tools help engineers write code, this platform helps engineering organizations *understand* their own systems: what exists, how it connects, what has changed, and what should happen next. The Living Knowledge Graph is not a snapshot — it is a continuously evolving, semantically rich model of an organization's entire engineering reality.

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

## Search Engine — Technical Implementation

The `search-service` is the core algorithmic component of the platform.  
It was built to explicitly demonstrate classical computer science concepts applied at production scale.

### 1. Prefix Tree (Trie) — for autocomplete and O(k) prefix search

Every indexed token is inserted into a character-level **Trie**.  
Each node along the path stores the set of document IDs that contain a word passing through it,  
enabling O(k) prefix lookup (k = prefix length) without scanning the corpus.

```python
# src/search-service/trie.py
class TrieNode:
    __slots__ = ("children", "is_end", "doc_ids")

    def __init__(self):
        self.children: Dict[str, "TrieNode"] = {}
        self.is_end: bool = False
        self.doc_ids: Set[str] = set()   # docs containing any word through this node
```

**Live autocomplete endpoint:**
```bash
GET /search/suggest?q=pay
# → { "suggestions": ["payment", "paypal"] }
```

---

### 2. Fuzzy Search — Levenshtein edit distance

Handles typos and OCR errors using the **Wagner-Fischer dynamic programming** algorithm.  
Space optimised to O(n) using a single rolling row.  
An early-exit length filter skips candidates where `|len(a) - len(b)| > max_distance` in O(1).

```python
# src/search-service/fuzzy.py
def levenshtein_distance(s1: str, s2: str) -> int:
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1, start=1):
        curr = [i]
        for j, c2 in enumerate(s2, start=1):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j-1]+1, prev[j]+1, prev[j-1]+cost))
        prev = curr
    return prev[len(s2)]
```

**Live fuzzy search — "paymnt" (1 edit away from "payment"):**
```bash
GET /search?q=paymnt&maxFuzzyDistance=2
# → [
#     { "id": "repo_001", "score": -0.1, "matchType": "fuzzy(editDistance=1)" },
#     { "id": "repo_003", "score": -0.1, "matchType": "fuzzy(editDistance=1)" }
#   ]
```

Also implements **Jaro-Winkler similarity** for name/short-string matching (`fuzzy.jaro_winkler_similarity`).

---

### 3. TF-IDF Ranking — cosine similarity with lazy IDF updates

Ranking uses **TF-IDF cosine similarity** with a smooth IDF formula to avoid zero scores for universal terms.

```
TF(t, d)  = count(t in d) / |d|
IDF(t)    = log(N / (df(t) + 1)) + 1          ← smooth variant
w(t, d)   = TF(t,d) × IDF(t)
score     = cosine(query_vector, doc_vector)
```

IDF is computed **lazily** via a dirty flag — only recalculated when the corpus changes,  
not on every query. Per-document L2 norms are also cached and recomputed only for modified docs.

**Live explain endpoint — inspect IDF per token:**
```bash
GET /search/explain?q=payment+billing
# → {
#     "tokens": [
#       { "token": "payment", "idf": 1.2231, "documentFrequency": 3 },
#       { "token": "billing",  "idf": 1.5108, "documentFrequency": 2 }
#     ]
#   }
```
"billing" ranks higher (IDF 1.51) because it appears in fewer documents — correctly identified as more discriminative.

---

### 4. Thread Safety & Parallelism

The search engine uses **two levels of concurrency control**:

| Lock | Scope | Protects |
|------|-------|----------|
| `asyncio.Lock` | Coroutine level | Serialises concurrent `async` write operations on the index |
| `threading.RLock` | Thread level | Protects `TFIDFIndex` state accessed from `ThreadPoolExecutor` threads |

CPU-bound work (fuzzy matching over the full vocabulary) is offloaded to a `ThreadPoolExecutor`  
to keep the asyncio event loop free:

```python
# src/search-service/engine.py
fuzzy_distances = await loop.run_in_executor(
    _executor,          # ThreadPoolExecutor(max_workers=4)
    bulk_fuzzy_resolve, # pure Python, no asyncio — safe to run in thread
    tokens,
    inverted_snapshot,  # shallow copy taken before hand-off for thread safety
    max_fuzzy_distance,
)
```

Parallel document ingestion uses `asyncio.gather`:
```python
await asyncio.gather(*[self.add(e["id"], e["type"], e["text"], ...) for e in entries])
```

---

### 5. Hybrid Scoring Pipeline

Every search request passes through a staged pipeline:

```
Query
  │
  ├─ Inverted Index  ──→  exact matches          (+0.25 bonus)
  ├─ Trie            ──→  prefix matches          (+0.15 bonus)
  └─ Levenshtein     ──→  fuzzy matches           (−0.10 × edit_distance)
         │
         └─ TF-IDF cosine similarity (base score)
                  │
                  └─ Ranked results with matchType explanation
```

Each result includes `matchType: "exact" | "prefix" | "fuzzy(editDistance=N)"` for full transparency.

**Full search example:**
```bash
# Start service
export PYTHONPATH=src
uvicorn main:app --port 8006 --app-dir src/search-service

# Index a document
curl -X POST http://localhost:8006/search/index \
  -H "Content-Type: application/json" \
  -d '{"id":"repo_001","type":"Repository","text":"payment service golang microservice billing","metadata":{}}'

# Exact search
curl "http://localhost:8006/search?q=billing"

# Prefix search (Trie)
curl "http://localhost:8006/search?q=pay"

# Fuzzy search (typo tolerance)
curl "http://localhost:8006/search?q=paymnt&maxFuzzyDistance=2"

# Autocomplete
curl "http://localhost:8006/search/suggest?q=pay"

# Query explanation (TF-IDF)
curl "http://localhost:8006/search/explain?q=payment+billing"
```

**Source files:**
| File | Responsibility |
|------|---------------|
| [`src/search-service/trie.py`](src/search-service/trie.py) | Prefix Tree — O(k) prefix search, DFS autocomplete |
| [`src/search-service/fuzzy.py`](src/search-service/fuzzy.py) | Levenshtein DP, Jaro-Winkler, bulk fuzzy resolve |
| [`src/search-service/ranking.py`](src/search-service/ranking.py) | TF-IDF with cosine similarity, threading.RLock |
| [`src/search-service/engine.py`](src/search-service/engine.py) | Unified engine: asyncio.Lock, ThreadPoolExecutor, asyncio.gather |

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
| `search-service`   | 8006 | Hybrid search: TF-IDF cosine similarity + Trie prefix + Levenshtein fuzzy |
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

| Phase | Name | Status | Key Capability |
|-------|------|--------|----------------|
| **Phase 1** | Living Knowledge | ✅ **Complete** | Continuous ingestion, knowledge graph, hybrid search |
| Phase 2 | Engineering Intelligence | 🔵 Next | Goal API, AI-assisted analysis, workflow automation |
| Phase 3 | Autonomous Engineering Intelligence | 📋 Planned | Self-improving agents, predictive architecture guidance |

---

## Future Direction — Where This Is Going

Phase 1 establishes the foundation. The platform's true ambition is further:

```
Phase 1 (Now)            Phase 2                    Phase 3
─────────────────        ───────────────────────    ─────────────────────────────
Repositories      →      Goal API               →   Autonomous Agents
Documentation     →      Planner                →   Self-improving Knowledge
Knowledge Graph   →      Workflow Orchestration →   Predictive Architecture
Hybrid Search     →      AI-Assisted Analysis   →   Engineering Intelligence
Event Streams     →      Recommendations        →   Proactive Guidance
```

### Goal API (Phase 2)

Engineers express intent, the platform executes:
```
POST /goals
{
  "goal": "Identify all services with no owner and no documentation",
  "scope":  { "organizationId": "org_001" }
}
```
The platform decomposes the goal, retrieves relevant knowledge, executes analysis agents, and returns a structured result — not a chat response, but an engineering artifact.

### Planner + Workflow Engine (Phase 2)

Goals are broken into executable plans. Plans trigger workflows. Workflows coordinate agents.  
Every step is observable, auditable, and replayable via the event stream.

### Autonomous Agents (Phase 3)

Specialised agents operate continuously on the knowledge graph:

| Agent | Responsibility |
|-------|----------------|
| Repository Analysis Agent | Extracts architecture from source code |
| Documentation Agent | Identifies gaps, generates missing docs |
| Dependency Agent | Maps and monitors inter-service dependencies |
| ADR Agent | Surfaces relevant architecture decisions |
| Impact Agent | Predicts blast radius of proposed changes |

Agents are first-class platform principals — authenticated, authorized, and audited identically to human engineers (see [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md)).

### Why This Matters

Most engineering organizations suffer from **institutional amnesia** — knowledge locked in individuals, scattered across wikis, buried in commit history.  
This platform makes organizational engineering knowledge **persistent, searchable, connected, and continuously evolving**.

The long-term outcome: an engineering organization where the platform understands the system as well as its best engineers do.

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

---

## Documentation

Full architectural documentation is in [`docs/`](docs/):

| Document | Description |
|----------|-------------|
| [`VISION.md`](docs/VISION.md) | Platform vision and long-term objectives |
| [`phases.md`](docs/phases.md) | 3-phase development roadmap |
| [`SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md) | Full system architecture |
| [`API_SPECIFICATION.md`](docs/API_SPECIFICATION.md) | Complete REST API specification |
| [`EVENT_CATALOG.md`](docs/EVENT_CATALOG.md) | All Kafka event definitions with payloads |
| [`DATABASE_DESIGN.md`](docs/DATABASE_DESIGN.md) | Polyglot persistence design |
| [`KNOWLEDGE_GRAPH.md`](docs/KNOWLEDGE_GRAPH.md) | Living Knowledge Graph schema |
| [`DOMAIN_MODEL.md`](docs/DOMAIN_MODEL.md) | Domain-driven design model |
| [`AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) | Multi-agent orchestration design |
| [`FUNCTIONAL_REQUIREMENTS.md`](docs/FUNCTIONAL_REQUIREMENTS.md) | Traced requirements (FR-100 to FR-900) |
