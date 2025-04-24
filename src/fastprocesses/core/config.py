from pydantic import AnyUrl, Field, RedisDsn, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings

from fastprocesses.core.logging import logger


class ResultCacheConnectionConfig(BaseSettings):
    RESULT_CACHE_HOST: str = "redis"
    RESULT_CACHE_PORT: int = 6379
    RESULT_CACHE_DB: str = 1
    RESULT_CACHE_PASSWORD: SecretStr = ""

    @computed_field
    @property
    def connection(self) -> RedisDsn:
        return RedisDsn.build(
            scheme="redis",
            host=self.RESULT_CACHE_HOST,
            port=self.RESULT_CACHE_PORT,
            path=self.RESULT_CACHE_DB,
            password=self.RESULT_CACHE_PASSWORD.get_secret_value(),
        )


class CeleryConnectionConfig(BaseSettings):
    CELERY_BROKER_HOST: str = "redis"
    CELERY_BROKER_PORT: int = 6379
    CELERY_BROKER_DB: str = 0
    CELERY_BROKER_PASSWORD: SecretStr = ""

    @computed_field
    @property
    def connection(self) -> RedisDsn:
        return RedisDsn.build(
            scheme="redis",
            host=self.CELERY_BROKER_HOST,
            port=self.CELERY_BROKER_PORT,
            path=self.CELERY_BROKER_DB,
            password=self.CELERY_BROKER_PASSWORD.get_secret_value(),
        )


class OGCProcessesSettings(BaseSettings):
    api_title: str = "Simple Process API"
    api_version: str = "1.0.0"
    api_description: str = "A simple API for running processes"
    celery_broker: CeleryConnectionConfig = Field(
        default_factory=CeleryConnectionConfig
    )
    celery_result: CeleryConnectionConfig = Field(
        default_factory=CeleryConnectionConfig
    )
    results_cache: ResultCacheConnectionConfig = Field(
        default_factory=ResultCacheConnectionConfig
    )
    CORS_ALLOWED_ORIGINS: list[AnyUrl | str] = ["*"]
    CELERY_RESULTS_TTL_DAYS: int = 365
    CELERY_TASK_TLIMIT_HARD: int = 900 # seconds
    CELERY_TASK_TLIMIT_SOFT: int = 600 # seconds
    RESULTS_TEMP_TTL_HOURS: int = Field(
        default=48,  # 2 days
        description="Time to live for cached results in days",
    )
    JOB_STATUS_TTL_DAYS: int = Field(
        default=365,  # 7 days
        description="Time to live for job status in days",
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
