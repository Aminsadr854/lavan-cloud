from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    artifact_dir: Path = Path("/tmp/lavan-private-artifacts")
    upload_limit: int = 50 * 1024 * 1024
    expanded_limit: int = 200 * 1024 * 1024
    file_limit: int = 1000
    log_limit: int = 256 * 1024
    lease_seconds: int = 30
    max_attempts: int = 5
    artifact_retention_seconds: int = 3600
    simulation_step_seconds: float = 1

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value):
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL psycopg URL required")
        return value

    @field_validator("artifact_dir")
    @classmethod
    def private_directory(cls, value):
        value = value.resolve()
        root = Path(__file__).resolve().parents[1]
        if value == root or root in value.parents:
            raise ValueError("Artifact directory must be outside source tree")
        return value


settings = Settings()
