# worker/celery_app.py
from typing import Any, Dict
from celery import Celery

from fastprocesses.core.config import settings
from fastprocesses.services.service_registry import ServiceRegistry

celery_app = Celery(
    "ogc_processes",
    broker=str(settings.celery_broker_url),
    backend=str(settings.celery_result_backend),
    include=["fastprocesses.worker.celery_app"]  # Ensure the module is included
)

@celery_app.task
def execute_process(process_id: str, data: Dict[str, Any]):
    service = ServiceRegistry().get_service(process_id)
    result = service.execute(data)
    return result
