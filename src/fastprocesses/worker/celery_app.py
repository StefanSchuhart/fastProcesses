# worker/celery_app.py
import json
import signal
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

import kombu.exceptions
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from pydantic import BaseModel, ValidationError

from fastprocesses.common import (
    celery_app,
    sigint_handler,
    sigterm_handler,
    temp_result_cache,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    CalculationTask,
    JobStatusCode,
)
from fastprocesses.worker.executors import get_executor
from fastprocesses.worker.job_status import update_job_status
from fastprocesses.worker.pipeline import (
    _deserialize,
    _load_process,
    _run_pipeline,
)

# NOTE: Cache hash key is based on original unprocessed inputs always
# this ensures consistent caching and cache retrieval
# which does not depend on arbitrary processed data, which can change
# when the process is updated or changed!

# Register signal handlers
signal.signal(signal.SIGTERM, sigterm_handler)
signal.signal(signal.SIGINT, sigint_handler)



class CacheResultTask(Task):
    def on_success(self, retval: dict | BaseModel, task_id, args, kwargs):
        if retval is None:
            # Parallel/scatter execute_process tasks return None after dispatching
            # a chord; the finalize_* callback task handles caching instead.
            return
        try:
            # Deserialize the original data
            original_data = json.loads(args[1])
            calculation_task = CalculationTask(**original_data)

            # Get the the hash key for the task
            key = calculation_task.celery_key

            # Store the result in cache
            # Use the task ID as the key
            serialized_result = temp_result_cache.put(key=key, value=retval)

            # TODO: shorten retval log!
            logger.info(
                f"Saved result with key {key} to cache: {serialized_result[:80]}"
            )
        except Exception as e:
            logger.error(f"Error caching results: {e}")

# ---------------------------------------------------------------------------
# Parallel / scatter subtask progress tracking
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="fastprocesses.execute_process", base=CacheResultTask)
def execute_process(self, process_id: str, serialized_data: str | bytes):
    def job_progress_callback(progress: int, message: str | None = None):
        update_job_status(
            job_id,
            progress,
            message,
            JobStatusCode.RUNNING,
            process_id=process_id,
        )

    data, serialized_data_str = _deserialize(serialized_data)
    job_id = self.request.id
    task_start = time.monotonic()
    logger.info(
        "Worker picked up task: process_id={}, job_id={}",
        process_id,
        job_id,
    )
    update_job_status(
        job_id,
        0,
        "Worker started, task picked up.",
        process_id=process_id,
    )

    # Check cache before running the pipeline — avoids re-execution for
    # identical inputs (especially important for parallel/scatter processes
    # where the full chord would otherwise be dispatched again).
    try:
        calculation_task = CalculationTask(**data)
        cached_result = temp_result_cache.get(key=calculation_task.celery_key)
        if cached_result is not None:
            logger.info(
                "Cache hit in worker for process_id={}, job_id={}, key={}",
                process_id,
                job_id,
                calculation_task.celery_key,
            )
            update_job_status(
                job_id,
                100,
                "Result retrieved from cache.",
                JobStatusCode.SUCCESSFUL,
                started=datetime.now(timezone.utc),
                process_id=process_id,
            )
            return cached_result
    except Exception as e:
        logger.debug("Cache lookup failed (non-fatal), proceeding: {}", e)

    process = _load_process(process_id, job_id)
    data = _run_pipeline(
        process, process_id, data, job_id, job_progress_callback
    )

    try:
        update_job_status(
            job_id,
            0,
            "Process started",
            JobStatusCode.RUNNING,
            started=datetime.now(timezone.utc),
            process_id=process_id,
        )
        result = get_executor(process).execute(
            process, process_id, data, job_id, serialized_data_str, job_progress_callback
        )

    except SoftTimeLimitExceeded:
        logger.warning(f"Task {job_id} hit the soft time limit.")
        update_job_status(
            job_id, 0, f"Process exceeded time limit (job_id={job_id}).", JobStatusCode.FAILED
        )
        raise

    except kombu.exceptions.OperationalError as e:
        task_elapsed = time.monotonic() - task_start
        logger.warning(
            "Broker connection error dispatching chord for job {} after {:.2f}s, "
            "scheduling retry: {}",
            job_id,
            task_elapsed,
            e,
        )
        raise self.retry(exc=e, countdown=10, max_retries=6)

    except (ValueError, ValidationError) as e:
        task_elapsed = time.monotonic() - task_start
        logger.error(
            "Task failed: process_id={}, job_id={}, duration={:.2f}s, reason={}",
            process_id,
            job_id,
            task_elapsed,
            e,
        )
        update_job_status(job_id, 0, str(e), JobStatusCode.FAILED)
        raise

    except Exception as e:
        task_elapsed = time.monotonic() - task_start
        logger.error(
            "Task failed: process_id={}, job_id={}, duration={:.2f}s, reason={}",
            process_id,
            job_id,
            task_elapsed,
            e,
        )
        logger.debug(
            "Full traceback for job {}:\n{}",
            job_id,
            traceback.format_exc(),
        )
        job_message = (
            f"Process execution failed. "
            f"Consult server logs for details (job_id={job_id})."
        )
        update_job_status(job_id, 0, job_message, JobStatusCode.FAILED)
        
        raise e

    if result is None:
        # Chord dispatched — finalize_* handles status + caching.
        return None

    task_elapsed = time.monotonic() - task_start
    logger.info(
        "Task completed: process_id={}, job_id={}, duration={:.2f}s",
        process_id,
        job_id,
        task_elapsed,
    )
    update_job_status(job_id, 100, "Process completed", JobStatusCode.SUCCESSFUL)

    return result.model_dump(mode="json")


@celery_app.task(name="fastprocesses.check_cache")
def check_cache(calculation_task: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check if results exist in cache and return status
    """
    task = CalculationTask(**calculation_task)
    cached_result = temp_result_cache.get(key=task.celery_key)

    if cached_result:
        logger.info(f"Cache hit for key {task.celery_key}")
        return {"status": "HIT", "result": cached_result}

    logger.info(f"Cache miss for key {task.celery_key}")
    return {"status": "MISS"}


@celery_app.task(bind=True, name="fastprocesses.find_result_in_cache")
def find_result_in_cache(self, celery_key: str) -> dict | None:
    """
    Retrieve result from cache
    """
    result = temp_result_cache.get(key=celery_key)
    if result:
        logger.info(f"Retrieved result from cache for key {celery_key}")
        update_job_status(
            job_id=self.request.id,
            progress=100,
            message="Result retrieved from cache.",
            status=JobStatusCode.SUCCESSFUL,
        )
    return result



