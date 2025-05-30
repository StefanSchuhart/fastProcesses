from pydantic import AnyUrl, Field, RedisDsn, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings

from fastprocesses.core.logging import logger


class ResultCacheConnectionConfig(BaseSettings):
    FP_RESULT_CACHE_HOST: str = "redis"
    FP_RESULT_CACHE_PORT: int = 6379
    FP_RESULT_CACHE_DB: str = "1"
    FP_RESULT_CACHE_PASSWORD: SecretStr = SecretStr("")

    @computed_field
    @property
    def connection(self) -> RedisDsn:
        return RedisDsn.build(
            scheme="redis",
            host=self.FP_RESULT_CACHE_HOST,
            port=self.FP_RESULT_CACHE_PORT,
            path=self.FP_RESULT_CACHE_DB,
            password=self.FP_RESULT_CACHE_PASSWORD.get_secret_value(),
        )


    @classmethod
    def get(cls) -> "ResultCacheConnectionConfig":
        return cls()

    class Config:
        env_file = ".env"
        extra = "ignore"


class CeleryConnectionConfig(BaseSettings):
    FP_CELERY_BROKER_HOST: str = "redis"
    FP_CELERY_BROKER_PORT: int = 6379
    FP_CELERY_BROKER_DB: str = "0"
    FP_CELERY_BROKER_PASSWORD: SecretStr = SecretStr("")

    @computed_field
    @property
    def connection(self) -> RedisDsn:
        return RedisDsn.build(
            scheme="redis",
            host=self.FP_CELERY_BROKER_HOST,
            port=self.FP_CELERY_BROKER_PORT,
            path=self.FP_CELERY_BROKER_DB,
            password=self.FP_CELERY_BROKER_PASSWORD.get_secret_value(),
        )

    @classmethod
    def get(cls) -> "CeleryConnectionConfig":
        return cls()

    class Config:
        env_file = ".env"
        extra = "ignore"


class OGCProcessesSettings(BaseSettings):
    FP_API_TITLE: str = "OGC API Processes"
    FP_API_VERSION: str = "1.0.0"
    FP_API_DESCRIPTION: str = "A simple API for running OGC API processes"
    celery_broker: CeleryConnectionConfig = Field(
        default_factory=CeleryConnectionConfig.get
    )
    celery_result: CeleryConnectionConfig = Field(
        default_factory=CeleryConnectionConfig.get
    )
    results_cache: ResultCacheConnectionConfig = Field(
        default_factory=ResultCacheConnectionConfig.get
    )
    FP_CORS_ALLOWED_ORIGINS: list[AnyUrl | str] = ["*"]
    FP_CELERY_RESULTS_TTL_DAYS: int = 365
    FP_CELERY_TASK_TLIMIT_HARD: int = 900 # seconds
    FP_CELERY_TASK_TLIMIT_SOFT: int = 600 # seconds
    FP_RESULTS_TEMP_TTL_HOURS: int = Field(
        default=48,  # 2 days
        description="Time to live for cached results in days",
    )
    FP_JOB_STATUS_TTL_DAYS: int = Field(
        default=365,  # 7 days
        description="Time to live for job status in days",
    )
    FP_SYNC_EXECUTION_TIMEOUT_SECONDS: int = Field(
        default=10,
        description="Timeout in seconds for synchronous execution waiting for result."
    )
    FP_LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level for the application. Options: DEBUG, INFO, WARNING, ERROR, CRITICAL",
    )

    @field_validator("FP_CORS_ALLOWED_ORIGINS", mode="before")
    def parse_cors_origins(cls, v) -> list[str]:
        if isinstance(v, str):
            # Handle comma-separated string
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        if isinstance(v, list):
            return [str(origin).strip() for origin in v if str(origin).strip()]

        raise ValueError(
            "FP_CORS_ALLOWED_ORIGINS must be a comma-separated string or list"
        )

    def print_settings(self):
        logger.info("Current settings:")
        logger.info(vars(self))

    class Config:
        env_file = ".env"
        extra = "ignore"
