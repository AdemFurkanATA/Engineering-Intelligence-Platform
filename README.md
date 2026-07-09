# Engineering Intelligence Platform (MVP)

Welcome to the **Engineering Intelligence Platform**! This repository contains the Minimum Viable Product (MVP) of an event-driven, distributed architecture designed for maintaining "Living Knowledge" across software engineering teams.

## 🌟 Architecture Overview

The system is built on an **Event-Driven Architecture** utilizing **Kafka** as the central nervous system. It consists of 8 highly decoupled microservices written in Python (FastAPI).

### Core Components:

1. **API Gateway** (`:8000`): The entry point for all external requests. It dynamically routes requests to the appropriate microservices based on the URL path.
2. **Auth Service** (`:8001`): Manages authentication and issues JWT tokens.
3. **Repository Service** (`:8002`): Handles the registration of software repositories. It acts as an **Event Publisher**, broadcasting `RepositoryCreated` events to Kafka.
4. **Document Service** (`:8003`): An **Event Consumer**. Listens to repository creations, scans for documents, and publishes `DocumentationUploaded` events.
5. **Embedding Service** (`:8004`): Listens for new documentation, generates AI vector embeddings for semantic search, and publishes `EmbeddingGenerated` events.
6. **Graph Service** (`:8005`): Ingests all events to build a rich, interconnected **Knowledge Graph** (Neo4j) representing the relationships between repositories, documents, and teams.
7. **Search Service** (`:8006`): Indexes data into Qdrant/Elasticsearch to provide hybrid (full-text + semantic) search capabilities.
8. **Event Service** (`:8007`): Provides event cataloging and governance capabilities.

## 🚀 Getting Started

To run the MVP on your local machine, you will need **Docker Desktop** and **Python 3.8+**.

### 1. Start the Infrastructure
Make sure Docker Desktop is running. The `docker-compose.yml` file contains the required infrastructure (Kafka, Zookeeper, PostgreSQL, Neo4j, Qdrant).
```bash
make up
```

### 2. Install Dependencies
Install the required Python packages for all microservices in your virtual environment:
```bash
pip install -r requirements.txt
```

### 3. Start the Microservices
A convenient PowerShell script is provided to start all 8 microservices simultaneously in the background:
```powershell
.\start_all.ps1
```

### 4. Test the Event Chain
Send a test request to the API Gateway to register a new repository. This will trigger a cascade of Kafka events across the services!
```bash
curl -X POST http://localhost:8000/api/v1/repository-service/repositories \
     -H "Content-Type: application/json" \
     -d '{
       "organizationId": "org_001",
       "name": "core-payment-api",
       "url": "https://github.com/company/payment",
       "language": "Go",
       "createdBy": "user_42"
     }'
```

Watch the console logs of the microservices to see the events being published and consumed in real-time!

## 📂 Project Structure

```text
├── docs/                 # Architectural documentation and phase planning
├── src/
│   ├── api-gateway/      # Reverse proxy
│   ├── auth-service/     # Mock authentication
│   ├── document-service/ # Document ingestion
│   ├── embedding-service/# Vector generation
│   ├── event-service/    # Event tracking
│   ├── graph-service/    # Knowledge graph builder
│   ├── repository-service/# Repository management
│   ├── search-service/   # Search indexer
│   └── shared/           # Shared models, Kafka producers/consumers
├── docker-compose.yml    # Infrastructure
├── Makefile              # Lifecycle commands
└── start_all.ps1         # Startup script
```
