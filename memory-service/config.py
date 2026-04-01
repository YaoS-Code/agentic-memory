"""Memory service configuration.

All values can be overridden via environment variables prefixed with MEMORY_.
Example: MEMORY_PG_HOST=db.example.com
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL (with pgvector extension)
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "memory_user"
    pg_password: str = "change-me"
    pg_database: str = "memory_db"

    # Redis (caching & dedup)
    redis_url: str = "redis://127.0.0.1:6379/0"

    # MinIO (S3-compatible file storage)
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "agent-memory"
    minio_secure: bool = False

    # Embedding model
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # Retrieval
    default_search_limit: int = 5
    max_search_limit: int = 20
    default_token_budget: int = 8000
    recall_token_budget: int = 4000
    cache_ttl_seconds: int = 900          # 15 minutes
    conversation_cache_ttl: int = 14400   # 4 hours

    # Decay half-lives (days) — how fast memories fade by category
    decay_conversation_highlight: float = 14.0
    decay_general: float = 30.0
    decay_project: float = 45.0
    decay_insight: float = 60.0
    decay_decision: float = 90.0
    decay_skill: float = 180.0

    # Timezone
    timezone: str = "America/Vancouver"

    # Service
    host: str = "127.0.0.1"
    port: int = 18800

    model_config = {"env_prefix": "MEMORY_"}


settings = Settings()

HALF_LIFE_MAP: dict[str, float] = {
    "conversation_highlight": settings.decay_conversation_highlight,
    "general": settings.decay_general,
    "project": settings.decay_project,
    "insight": settings.decay_insight,
    "decision": settings.decay_decision,
    "skill": settings.decay_skill,
}
