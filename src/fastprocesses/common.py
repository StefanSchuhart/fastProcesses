from celery import Celery
from fastprocesses.core.config import settings
from fastprocesses.core.cache import Cache

celery_app = Celery(
    "ogc_processes",
    broker=str(settings.celery_broker_url),
    backend=str(settings.celery_result_backend),
    include=["fastprocesses.worker.celery_app"]  # Ensure the module is included
)

redis_cache = Cache(key_prefix="process_results", ttl_days=7)
