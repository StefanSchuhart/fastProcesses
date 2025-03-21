import json

from celery import Celery
from fastapi.encoders import jsonable_encoder
from kombu.serialization import register

from fastprocesses.core.cache import Cache
from fastprocesses.core.config import settings


def custom_json_serializer(obj):
    # Use jsonable_encoder to handle complex objects
    return json.dumps(jsonable_encoder(obj))


def custom_json_deserializer(data):
    # Deserialize JSON back into Python objects
    return json.loads(data)

# Register the custom serializer
register(
    "custom_json",
    custom_json_serializer,
    custom_json_deserializer,
    content_type="application/x-custom-json",
    content_encoding="utf-8",
)

celery_app = Celery(
    "ogc_processes",
    broker=str(settings.celery_broker_url),
    backend=str(settings.celery_result_backend),
    include=["fastprocesses.worker.celery_app"],  # Ensure the module is included
)

celery_app.conf.update(
    task_serializer="custom_json",
    result_serializer="custom_json",
    accept_content=["custom_json", "json"],  # Accept only the custom serializer
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "fastprocesses.worker.celery_app.execute_process": {
            "queue": "process_tasks",
            "routing_key": "process_tasks",
        }
    },
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    # set limits for long-running tasks
    task_time_limit=900,  # Hard limit in seconds
    task_soft_time_limit=600,  # Soft limit in seconds
    result_expires=3153600,  # 1 year in seconds
    worker_send_task_events=True,  # Enable events to track task progress
    # task_acks_late=True,  # Acknowledge the task only after it has been executed)

)

redis_cache = Cache(key_prefix="process_results", ttl_days=7)
