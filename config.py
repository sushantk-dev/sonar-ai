"""
SonarAI — Configuration
All settings loaded from environment variables or .env file via Pydantic Settings.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── GCP / Vertex AI ──────────────────────────────────────────────────────
    gcp_project: str = Field(..., description="GCP project ID for Vertex AI")
    gcp_location: str = Field(default="us-central1", description="GCP region")
    vertex_model: str = Field(
        default="claude-sonnet-4-5@20251001",
        description="Vertex AI model (Claude or Gemini fallback)",
    )
    vertex_fallback_model: str = Field(
        default="gemini-1.5-pro-002",
        description="Fallback model if primary is unavailable",
    )
    embedding_model: str = Field(
        default="text-embedding-005",
        description="Vertex AI embedding model",
    )
    max_tokens: int = Field(default=8192, description="Max tokens per LLM call")

    # ── GitHub ────────────────────────────────────────────────────────────────
    github_token: str = Field(..., description="GitHub personal access token")
    github_base_url: str = Field(
        default="https://api.github.com",
        description="GitHub API base URL (override for GHE)",
    )

    # ── Sonar ─────────────────────────────────────────────────────────────────
    sonar_token: str = Field(default="", description="SonarQube/SonarCloud API token")
    sonar_host_url: str = Field(
        default="https://sonarcloud.io",
        description="SonarQube host URL",
    )

    # ── Pipeline behaviour ────────────────────────────────────────────────────
    max_critic_retries: int = Field(default=1, description="Max LLM fix retry loops")
    compile_timeout: int = Field(default=120, description="mvn compile timeout seconds")
    test_timeout: int = Field(default=180, description="mvn test timeout seconds")
    clone_dir: str = Field(default="/tmp/sonar-ai-repos", description="Base dir for cloned repos")
    escalation_dir: str = Field(default="escalations", description="Dir for escalation markdown files")

    # ── Confidence thresholds ─────────────────────────────────────────────────
    confidence_high_threshold: float = Field(default=0.8, description="Score >= this → HIGH confidence")
    confidence_medium_threshold: float = Field(default=0.5, description="Score >= this → MEDIUM confidence")


# Module-level singleton — import this everywhere
settings = Settings()
