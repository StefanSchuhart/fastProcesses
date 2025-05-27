import json
import logging
import sys

from celery import Celery
from fastapi.encoders import jsonable_encoder
from kombu.serialization import register

from fastprocesses.core.logging import InterceptHandler, logger
from fastprocesses.core.cache import TempResultCache
from fastprocesses.core.config import OGCProcessesSettings

settings = OGCProcessesSettings()

settings.print_settings()


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
    broker=settings.celery_broker.connection.unicode_string(),
    backend=settings.celery_result.connection.unicode_string(),
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
    task_time_limit=settings.FP_CELERY_TASK_TLIMIT_HARD,  # Hard limit in seconds
    task_soft_time_limit=settings.FP_CELERY_TASK_TLIMIT_SOFT,  # Soft limit in seconds
    result_expires=settings.FP_CELERY_RESULTS_TTL_DAYS * 86000,  # Time in seconds before results expire
    worker_send_task_events=True,  # Enable events to track task progress
    # task_acks_late=True,  # Acknowledge the task only after it has been executed)
)

temp_result_cache = TempResultCache(
    key_prefix="process_results",
    ttl_hours=settings.FP_RESULTS_TEMP_TTL_HOURS,
    connection=settings.results_cache.connection
)

job_status_cache = TempResultCache(
    key_prefix="job_status",
    ttl_hours=settings.FP_JOB_STATUS_TTL_DAYS,
    connection=settings.results_cache.connection
)

# Info logs to stdout

logger.add(
    sys.stdout, 
    level=settings.FP_LOG_LEVEL, 
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}",
    backtrace=True,
    diagnose=True,
)


# Intercept standard logging
logging.basicConfig(handlers=[InterceptHandler()], level=settings.FP_LOG_LEVEL)