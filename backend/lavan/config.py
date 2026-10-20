from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from lavan.storage import storage_root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    postgres_host: str = Field(min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(min_length=1)
    postgres_user: str = Field(min_length=1)
    postgres_password: SecretStr = Field(min_length=1)

    artifact_storage_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/lavan/artifacts"
    )
    zip_max_compressed_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    zip_max_uncompressed_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    zip_max_entries: int = Field(default=2000, ge=1, le=65534)
    zip_max_file_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    zip_max_filename_chars: int = Field(default=512, ge=1)
    upload_max_seconds: float = Field(default=60, gt=0)
    upload_max_active: int = Field(default=2, ge=1)

    run_max_lifetime_seconds: float = Field(default=900, gt=0, le=86400)
    worker_expiration_batch_size: int = Field(default=50, ge=1, le=200)
    worker_expiration_poll_seconds: float = Field(default=0.25, ge=0.02, le=5)

    worker_lease_seconds: float = Field(default=5, ge=0.2, le=60)
    worker_poll_seconds: float = Field(default=0.1, ge=0.01, le=1)
    worker_retry_seconds: float = Field(default=1, ge=0.05, le=30)
    simulator_scenario: Literal["success", "startup_failure", "slow_startup"] = (
        "success"
    )
    simulator_startup_seconds: float = Field(default=0.1, ge=0, le=10)
    simulator_slow_startup_seconds: float = Field(default=3, ge=0, le=30)
    simulator_duration_seconds: float = Field(default=0.2, ge=0, le=10)

    @field_validator("artifact_storage_dir")
    @classmethod
    def private_storage_directory(cls, value):
        return storage_root(value)

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
