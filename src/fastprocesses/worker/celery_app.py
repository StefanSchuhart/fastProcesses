# worker/celery_app.py
import asyncio
from typing import Any, Dict
from celery import Celery

from fastprocesses.core.config import settings
from fastprocesses.services.service_registry import get_service_registry

celery_app = Celery(
    "ogc_processes",
    broker=str(settings.celery_broker_url),
    backend=str(settings.celery_result_backend),
    include=["fastprocesses.worker.celery_app"]  # Ensure the module is included
)

@celery_app.task(name="execute_process")
def execute_process(process_id: str, data: Dict[str, Any]):
    service = get_service_registry().get_service(process_id)
    if asyncio.iscoroutinefunction(service.execute):
        result = asyncio.run(service.execute(data))
    else:
        result = service.execute(data)
    return result