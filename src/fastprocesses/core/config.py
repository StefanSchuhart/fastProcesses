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


class CeleryResultConnectionConfig(BaseSettings):
    """Connection config for the Celery result backend.

    Defaults match the broker defaults so that deployments which do not yet
    have a dedicated result-backend Redis continue to work unchanged.  Set
    FP_CELERY_RESULT_HOST / PORT / DB to point at a separate Redis instance
    and decouple result-backend memory pressure from the task queue broker.
    """

    FP_CELERY_RESULT_HOST: str = "redis"
    FP_CELERY_RESULT_PORT: int = 6379
    FP_CELERY_RESULT_DB: str = "1"
    FP_CELERY_RESULT_PASSWORD: SecretStr = SecretStr("")

    @computed_field
    @property
    def connection(self) -> RedisDsn:
        return RedisDsn.build(
            scheme="redis",
            host=self.FP_CELERY_RESULT_HOST,
            port=self.FP_CELERY_RESULT_PORT,
            path=self.FP_CELERY_RESULT_DB,
            password=self.FP_CELERY_RESULT_PASSWORD.get_secret_value(),
        )

    @classmethod
    def get(cls) -> "CeleryResultConnectionConfig":
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
    celery_result: CeleryResultConnectionConfig = Field(
        default_factory=CeleryResultConnectionConfig.get
    )
    results_cache: ResultCacheConnectionConfig = Field(
        default_factory=ResultCacheConnectionConfig.get
    )
    FP_CORS_ALLOWED_ORIGINS: list[AnyUrl | str] = ["*"]
    FP_CELERY_RESULTS_TTL_DAYS: int = 365
    FP_CELERY_TASK_TLIMIT_HARD: int = 900 # seconds
    FP_CELERY_TASK_TLIMIT_SOFT: int = 600 # seconds
    FP_CELERY_QUEUE: str = Field(
        default="celery",
        description="Celery queue name for task routing. Must match the worker -Q flag.",
    )
    FP_CELERY_JOB_MODE: bool = Field(
        default=False,
        description="Enable job mode for graceful shutdown after task completion"
    )
    FP_RESULTS_TEMP_TTL_HOURS: int = Field(
        default=48,  # 2 days
        description="Time to live for cached results in days",
    )
    FP_JOB_STATUS_TTL_DAYS: int = Field(
        default=365,  # 7 days
        description="Time to live for job status in days",
    )
    FP_MAX_RESULT_SIZE_BYTES: int | None = Field(
        default=10_485_760,  # 10 MiB
        description=(
            "Maximum size (compressed, in bytes) of a cached process result. "
            "Guards against oversized values overwhelming Dragonfly/Redis' "
            "per-IO-thread pipeline buffer (default 128 MiB). Set to None to "
            "disable."
        ),
    )
    FP_MAX_READ_SIZE_BYTES: int | None = Field(
        default=52_428_800,  # 50 MiB
        description=(
            "Unconditional ceiling (compressed, in bytes) enforced when reading "
            "a cached result, independent of FP_MAX_RESULT_SIZE_BYTES. Protects "
            "against decoding/parsing an oversized value regardless of whether "
            "it was written under an older/looser limit. Should generally stay "
            ">= FP_MAX_RESULT_SIZE_BYTES. Set to None to disable."
        ),
    )
    FP_SYNC_EXECUTION_TIMEOUT_SECONDS: int = Field(
        default=10,
        description="Timeout in seconds for synchronous execution waiting for result."
    )
    FP_SKIP_INPUT_VALIDATION: bool = Field(
        default=False,
        description=(
            "Skip JSON-Schema input validation on the worker. "
            "Intended for debugging only — never enable in production."
        ),
    )
    FP_LOG_LEVEL: str = Field(
        default="INFO",
        description=(
            "Logging level for the application. "
            "Options: DEBUG, INFO, WARNING, ERROR, CRITICAL"
        ),
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
