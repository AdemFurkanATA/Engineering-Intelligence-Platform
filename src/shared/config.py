"""Centralised environment variable configuration for all services."""
import os


# Kafka
KAFKA_BOOTSTRAP_SERVERS: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

# Databases
POSTGRES_DSN: str = os.getenv(
    "POSTGRES_DSN", "postgresql://eip_user:eip_password@localhost:5432/eip_db"
)
NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "eip_password")
QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

# Auth
JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "changeme-super-secret-key-for-development")
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

# Service ports (for local dev)
API_GATEWAY_PORT: int = int(os.getenv("API_GATEWAY_PORT", "8000"))
AUTH_SERVICE_PORT: int = int(os.getenv("AUTH_SERVICE_PORT", "8001"))
REPOSITORY_SERVICE_PORT: int = int(os.getenv("REPOSITORY_SERVICE_PORT", "8002"))
DOCUMENT_SERVICE_PORT: int = int(os.getenv("DOCUMENT_SERVICE_PORT", "8003"))
EMBEDDING_SERVICE_PORT: int = int(os.getenv("EMBEDDING_SERVICE_PORT", "8004"))
GRAPH_SERVICE_PORT: int = int(os.getenv("GRAPH_SERVICE_PORT", "8005"))
SEARCH_SERVICE_PORT: int = int(os.getenv("SEARCH_SERVICE_PORT", "8006"))
EVENT_SERVICE_PORT: int = int(os.getenv("EVENT_SERVICE_PORT", "8007"))
