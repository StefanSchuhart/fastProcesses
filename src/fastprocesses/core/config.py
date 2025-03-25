from pydantic import AnyUrl, Field, RedisDsn, field_validator
from pydantic_settings import BaseSettings

from fastprocesses.core.logging import logger


class OGCProcessesSettings(BaseSettings):
    api_title: str = "Simple Process API"
    api_version: str = "1.0.0"
    api_description: str = "A simple API for running processes"
    celery_broker_url: RedisDsn = RedisDsn("redis://redis:6379/0")
    celery_result_backend: RedisDsn = RedisDsn("redis://redis:6379/0")
    RESULTS_CACHE_URL: RedisDsn = RedisDsn("redis://redis:6379/1")
    CORS_ALLOWED_ORIGINS: list[AnyUrl] = ["*"]
    RESULTS_CACHE_TTL: int = Field(
        default=365,  # 1 year
        description="Time to live for cached results in days",
    )

    @field_validator("CORS_ALLOWED_ORIGINS", mode="before")
    def parse_cors_origins(cls, v) -> list[str]:
        if isinstance(v, str):
            # Handle comma-separated string
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        if isinstance(v, list):
            return [str(origin).strip() for origin in v if str(origin).strip()]

        raise ValueError(
            "CORS_ALLOWED_ORIGINS must be a comma-separated string or list"
        )

    def print_settings(self):
        logger.info("Current %s settings:", self.__class__.__name__)
        logger.info(vars(self))

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = OGCProcessesSettings()

settings.print_settings()
