import json
import logging
import signal
import sys

from celery import Celery
from celery.signals import worker_ready, worker_shutdown
from fastapi.encoders import jsonable_encoder
from kombu.serialization import register

from fastprocesses.core.cache import TempResultCache
from fastprocesses.core.config import OGCProcessesSettings
from fastprocesses.core.logging import InterceptHandler, logger


settings = OGCProcessesSettings()

logger.add(
    sys.stdout,
    level=settings.FP_LOG_LEVEL,
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}",
    backtrace=True,
    diagnose=True,
)

# Intercept standard logging
logging.basicConfig(handlers=[InterceptHandler()], level=settings.FP_LOG_LEVEL)

settings.print_settings()


# Graceful shutdown handler
def sigterm_handler(signum, frame):
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")


def sigint_handler(signum, frame):
    logger.info("Received SIGINT, initiating graceful shutdown...")
    sys.exit(0)

def custom_json_serializer(obj):
    # Use jsonable_encoder to handle complex objects
    return json.dumps(jsonable_encoder(obj))


def custom_json_deserializer(data):
    # Deserialize JSON back into Python objects
    return json.loads(data)


# Register signal handlers
signal.signal(signal.SIGTERM, sigterm_handler)
signal.signal(signal.SIGINT, sigint_handler)

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
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    # set limits for long-running tasks
    task_time_limit=settings.FP_CELERY_TASK_TLIMIT_HARD,  # Hard limit in seconds
    task_soft_time_limit=settings.FP_CELERY_TASK_TLIMIT_SOFT,  # Soft limit in seconds
    result_expires=settings.FP_CELERY_RESULTS_TTL_DAYS * 86000,  # Time in seconds before results expire
    # Worker behavior for graceful shutdown
    worker_send_task_events=True,  # Enable events to track task progress
    worker_prefetch_multiplier=1,  # one worker, one task: dont hold tasks in memory (needed for kedas and queue scaling based scaling)
    task_acks_late=True,  # Acknowledge the task only after it has been executed and finished
    # Connection settings for better resilience
    broker_transport_options={
        "visibility_timeout": settings.FP_CELERY_TASK_TLIMIT_HARD
        + 300,  # Task limit + 5 minutes buffer
        "retry_on_timeout": True,
        "retry_on_connection_failure": True,
        # Only add these if you actually use them:
        # 'master_name': 'mymaster',  # Only if using Redis Sentinel
        # 'priority_steps': list(range(10)),  # Only if using task priorities
    },
    # Result backend settings
    result_backend_transport_options={
        "retry_on_timeout": True,
        "retry_on_connection_failure": True,
    },
)

for key, value in celery_app.conf.items():
    logger.debug(f"Celery config: {key} = {value}")


# Celery signal handlers
@worker_shutdown.connect
def worker_shutdown_handler(sender, **kwargs):
    logger.info("Worker is shutting down gracefully, waiting for tasks to complete...")


@worker_ready.connect
def worker_ready_handler(sender, **kwargs):
    logger.info("Worker is ready and configured for graceful shutdown")
    logger.info(f"task_acks_late setting: {celery_app.conf.task_acks_late}")
    logger.info(
        f"worker_prefetch_multiplier: {celery_app.conf.worker_prefetch_multiplier}"
    )


temp_result_cache = TempResultCache(
    key_prefix="process_results",
    ttl_days=settings.FP_RESULTS_TEMP_TTL_HOURS,
    connection=settings.results_cache.connection,
)

job_status_cache = TempResultCache(
    key_prefix="job_status",
    ttl_days=settings.FP_JOB_STATUS_TTL_DAYS,
    connection=settings.results_cache.connection,
)
