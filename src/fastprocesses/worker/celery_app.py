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
from fastprocesses.core.base_process import (
    BaseParallelProcess,
    BaseScatterProcess,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    CalculationTask,
    JobStatusCode,
)
from fastprocesses.processes.process_registry import get_process_registry
from fastprocesses.worker.chord_tasks import _run_parallel, _run_scatter
from fastprocesses.worker.job_status import (
    _cleanup_progress_counter,
    _increment_and_report_progress,
    update_job_status,
)
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
        """
        Updates the progress of a job.

        Args:
            progress (int): The progress percentage (0-100).
            message (str): A message describing the current progress.
            status (str | None): The current status (e.g., "RUNNING", "SUCCESSFUL").
        """
        update_job_status(
            job_id,
            progress,
            message,
            JobStatusCode.RUNNING,
            process_id=process_id,
        )

    chord_dispatched = False  # True when a parallel/scatter chord is dispatched
    retrying = False  # True when self.retry() is in flight
    result = None
    job_status = JobStatusCode.RUNNING
    job_message = ""
    data, serialized_data_str = _deserialize(serialized_data)

    job_id = self.request.id  # Get the task/job ID
    task_start = time.monotonic()
    logger.info(
        "Worker picked up task: process_id={}, job_id={}",
        process_id,
        job_id,
    )

    process = _load_process(process_id, job_id)
    data = _run_pipeline(process, process_id, data, job_id)

    # Execute the process
    try:
        job_status = JobStatusCode.RUNNING
        # BUG: if redis returns no job_status, this fails and creates a ValueError too
        update_job_status(
            job_id,
            0,
            "Process started",
            job_status,
            started=datetime.now(timezone.utc),
        )

        if isinstance(process, BaseParallelProcess):
            # Data fan-out: split → N parallel subtasks (same op) → merge.
            # Chord dispatched; this task returns immediately.
            _run_parallel(process, process_id, data, job_id, serialized_data_str)
            chord_dispatched = True
        elif isinstance(process, BaseScatterProcess):
            # Operation fan-out: N different steps on same input → merge.
            # Chord dispatched; this task returns immediately.
            _run_scatter(process, process_id, data, job_id, serialized_data_str)
            chord_dispatched = True
        else:
            result = process.run_execute(
                data, job_progress_callback=job_progress_callback
            )

    except SoftTimeLimitExceeded as e:
        logger.warning(f"Task {job_id} hit the soft time limit: {e}")
        if chord_dispatched:
            raise  # chord tasks are independent; cannot resume here
        # Attempt to resume the process
        try:
            result = process.run_execute(
                data, job_progress_callback=job_progress_callback
            )

        except Exception as inner_exception:
            logger.error(
                f"Error while completing task after soft time limit: {inner_exception}"
            )

            raise e

        logger.info(f"Process {process_id} completed after soft time limit")
        job_status = JobStatusCode.SUCCESSFUL

    except kombu.exceptions.OperationalError as e:
        # Transient broker failure while dispatching the chord (Redis was down).
        # Retry the whole task so the chord is published once the broker recovers.
        # The job status stays RUNNING; no result has been produced yet.
        task_elapsed = time.monotonic() - task_start
        logger.warning(
            "Broker connection error dispatching chord for job {} after {:.2f}s, "
            "scheduling retry: {}",
            job_id,
            task_elapsed,
            e,
        )
        retrying = True
        raise self.retry(exc=e, countdown=10, max_retries=6)

    # intercept all errors coming from the process` execution method
    except Exception as e:
        # Update job with error status
        job_status = JobStatusCode.FAILED
        task_elapsed = time.monotonic() - task_start

        # decide if its a validation error or a general error
        if isinstance(e, ValueError) or isinstance(e, ValidationError):
            logger.error(
                "Task failed: process_id={}, job_id={}, duration={:.2f}s, reason={}",
                process_id,
                job_id,
                task_elapsed,
                e,
            )
            job_message = e
            raise e

        # Log the full traceback server-side only — never expose internal
        # class names, source lines, or line numbers to the client.
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

        raise Exception(job_message)

    finally:
        if chord_dispatched:
            # finalize_parallel / finalize_scatter handles status + caching.
            return None

        if retrying:
            # celery.exceptions.Retry is propagating — do not return here
            # (a return in finally would suppress it) and do not touch job status.
            pass
        elif result:
            task_elapsed = time.monotonic() - task_start
            logger.info(
                "Task completed: process_id={}, job_id={},"
                " duration={:.2f}s",
                process_id,
                job_id,
                task_elapsed,
            )
            job_status = JobStatusCode.SUCCESSFUL

            # Mark job as complete
            update_job_status(job_id, 100, "Process completed", job_status)

            # Return from the finally block (this will exit the function)
            return result.model_dump(exclude_none=True)

        else:
            job_status = JobStatusCode.FAILED
            # Update job status for failed jobs that didn't raise exceptions
            update_job_status(
                job_id,
                0,
                str(job_message),
                job_status,
            )

    logger.info(
        f"Process {process.__class__.__name__} execution completed. No result returned"
    )
    return None


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


# @task_failure.connect(sender=execute_process)
# def handle_execute_failure(
#     sender=None,
#     task_id=None,
#     exception=None,
#     traceback=None,
#     **kwargs
# ):
#     logger.error(
#         f"Task {task_id} failed. "
#         f"{repr(exception)}"
#         f"\nTraceback:\n{traceback}"
#     )
#         f"Task {task_id} failed. "
#         f"{repr(exception)}"
#         f"\nTraceback:\n{traceback}"
#     )
