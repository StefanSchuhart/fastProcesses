import logging
import signal
import sys
import zlib
from typing import Any

import orjson
from celery import Celery
from celery.signals import worker_ready, worker_shutdown, task_postrun
from fastapi.encoders import jsonable_encoder
from kombu.serialization import register

from fastprocesses.core.cache import TempResultCache
from fastprocesses.core.config import OGCProcessesSettings
from fastprocesses.core.exceptions import ResultTooLargeError
from fastprocesses.core.logging import InterceptHandler, logger
from fastprocesses.core.redis_connection import RedisConnection


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
    # zlib-compressed orjson: cuts both broker payload size and result-backend
    # storage size (same amplification risk as TempResultCache, see cache.py).
    return zlib.compress(orjson.dumps(jsonable_encoder(obj)))


def custom_json_deserializer(data):
    return orjson.loads(zlib.decompress(data))

# Register the custom serializer
register(
    "custom_json",
    custom_json_serializer,
    custom_json_deserializer,
    content_type="application/x-custom-json",
    content_encoding="binary",
)

celery_app = Celery(
    "ogc_processes",
    broker=settings.celery_broker.connection.unicode_string(),
    backend=settings.celery_result.connection.unicode_string(),
    include=[
        "fastprocesses.worker.celery_app",
        "fastprocesses.worker.chord_tasks",
    ],  # Ensure the modules are included
)

celery_app.conf.update(
    task_default_queue=settings.FP_CELERY_QUEUE,
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
    result_expires=settings.FP_CELERY_RESULTS_TTL_DAYS * 3600 * 24,  # Time in seconds before results expire
    # Worker behavior for graceful shutdown
    worker_send_task_events=True,  # Enable events to track task progress
    worker_prefetch_multiplier=1,  # one worker, one task: dont hold tasks in memory (needed for kedas and queue scaling based scaling)
    task_acks_late=True,  # Acknowledge the task only after it has been executed and finished
    # Do NOT cancel in-flight tasks when the broker connection is lost.
    # With True (the Celery 6.0 default), a transient Redis outage triggers a
    # task-termination cascade that tries to write to the result backend (also
    # Redis) — which is equally unreachable — and raises a CRITICAL/Unrecoverable
    # error, crashing the worker before it can reconnect.  The unacked task then
    # sits in the Redis unacked hash for the full visibility_timeout (35 min).
    # With False the worker waits for the broker to come back, re-acks cleanly,
    # and no message is orphaned.
    worker_cancel_long_running_tasks_on_connection_loss=False,
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
    # Re-queue the task immediately when the worker process dies (OOM kill, pod
    # eviction, etc.) instead of waiting for visibility_timeout to expire.
    # Without this, KEDA sees queue depth=0 (task is in unacked, not in queue)
    # and never spawns a replacement worker — the task is orphaned until
    # visibility_timeout (currently >30 min) elapses.
    task_reject_on_worker_lost=True,
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
    logger.info(f"Job mode enabled: {settings.FP_CELERY_JOB_MODE}")

@task_postrun.connect
def shutdown_worker_after_task(
    sender=None, task_id=None,
    task=None, state=None, retval=None, **kwargs
):
    if settings.FP_CELERY_JOB_MODE:
        hostname = task.request.hostname if task and task.request else None
        if hostname:
            logger.info(
                "Job mode: shutting down worker {} after task {}.",
                hostname,
                task_id,
            )
            try:
                celery_app.control.shutdown(destination=[hostname])
            except Exception as exc:
                # Broker may be temporarily unreachable (e.g. transient Redis
                # outage).  Log and move on — the pod will be reaped by K8s
                # activeDeadlineSeconds rather than a clean control shutdown.
                logger.warning(
                    "Job mode: failed to send shutdown command to {} ({}); "
                    "worker will be reaped by activeDeadlineSeconds.",
                    hostname,
                    exc,
                )
        else:
            logger.warning(
                "Job mode: could not determine worker hostname for task {}; "
                "skipping targeted shutdown.",
                task_id,
            )

# Shared pool: all three below point at the same results_cache Redis DB, so
# they share one bounded connection pool instead of each growing its own
# (separate pools each carry their own never-shrinking large-reply buffers).
_results_cache_connection = RedisConnection(str(settings.results_cache.connection))

temp_result_cache = TempResultCache(
    key_prefix="process_results",
    ttl_days=settings.FP_RESULTS_TEMP_TTL_HOURS,
    redis_connection=_results_cache_connection,
    max_size_bytes=settings.FP_MAX_RESULT_SIZE_BYTES,
    hard_read_ceiling_bytes=settings.FP_MAX_READ_SIZE_BYTES,
)

job_status_cache = TempResultCache(
    key_prefix="job_status",
    ttl_days=settings.FP_JOB_STATUS_TTL_DAYS,
    redis_connection=_results_cache_connection,
)

# Holds per-request output/format preferences (outputs, response mode) keyed
# by job_id, so GET /jobs/{job_id}/results can honour them. Distinct from
# job_status_cache, which holds only JobStatusInfo records.
job_request_cache = TempResultCache(
    key_prefix="job_request",
    ttl_days=settings.FP_JOB_STATUS_TTL_DAYS,
    redis_connection=_results_cache_connection,
)


def cache_computed_result(
    cache: TempResultCache, celery_key: str, value: Any, *, job_id: str | None = None
) -> None:
    """Writes a computed result to the dedup cache under its celery_key.

    This is the single entry point for caching results, used both by
    CacheResultTask.on_success (plain BaseProcess) and the finalize_parallel/
    finalize_scatter chord callbacks, so both paths behave identically.

    `cache` is passed in (rather than using the module-level temp_result_cache
    directly) so callers keep using their own imported reference — this keeps
    call sites patchable/mockable in tests the same way they were before.

    job_id must only be passed for chord-dispatched processes: execute_process
    returns None for those, so Celery's own result backend never gets a
    result under job_id, and get_job_result needs a pointer to bridge to the
    celery_key entry. Plain BaseProcess results are retrieved via Celery's
    AsyncResult(job_id) directly and must not pass job_id here.
    """
    try:
        cache.put(key=celery_key, value=value)
        if job_id is not None:
            cache.put(key=job_id, value={"__result_ref__": celery_key})
    except ResultTooLargeError:
        # Job-fatal: callers must mark the job FAILED rather than let it
        # silently succeed with no cached/retrievable result.
        raise
    except Exception as e:
        logger.error(f"Error caching result for key {celery_key}: {e}")
