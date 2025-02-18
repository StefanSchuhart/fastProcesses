from pydantic import RedisDsn, AnyUrl
from pydantic_settings import BaseSettings


class OGCProcessesSettings(BaseSettings):
    api_title: str = "Simple Process API"
    api_version: str = "1.0.0"
    api_description: str = "A simple API for running processes"
    celery_broker_url: RedisDsn = RedisDsn("redis://redis:6379/0")
    celery_result_backend: RedisDsn = RedisDsn("redis://redis:6379/0")
    redis_cache_url: RedisDsn = RedisDsn("redis://redis:6379/1")

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = OGCProcessesSettings()