# worker/celery_app.py
import asyncio
import inspect
import json
import signal
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

from celery import Task, chord, group
from celery.exceptions import SoftTimeLimitExceeded
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError

from fastprocesses.common import (
    celery_app,
    job_status_cache,
    sigint_handler,
    sigterm_handler,
    temp_result_cache,
)
from fastprocesses.core.base_process import (
    BaseParallelProcess,
    BaseScatterProcess,
    get_parallel_steps,
)
from fastprocesses.core.exceptions import (
    InputValidationError,
    ProcessClassNotFoundError,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    CalculationTask,
    JobStatusCode,
    JobStatusInfo,
    Link,
)
from fastprocesses.processes.process_registry import get_process_registry

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


# Create a progress update function that captures the job_id
def update_job_status(
    job_id: str,
    progress: int,
    message: str | None = None,
    status: str | None = None,
    started: datetime | None = None,
) -> None:
    """
    Updates the progress of a job.

    Args:
        progress (int): The progress percentage (0-100).
        message (str): A message describing the current progress.
        status (str | None): The current status (e.g., "RUNNING", "SUCCESSFUL").
    """

    job_key = f"job:{job_id}"
    raw_job_info = job_status_cache.get(job_key)

    if not raw_job_info:
        logger.warning(
            "Job status for job_id={} not found in cache when updating; "
            "skipping status update.",
            job_id,
        )
        return

    try:
        job_info = JobStatusInfo.model_validate(raw_job_info)
    except Exception as exc:
        logger.error(
            "Failed to validate cached job status for job_id={}: {!r}", job_id, exc
        )
        return

    job_info.status = status or job_info.status
    job_info.progress = progress
    job_info.started = started or job_info.started
    job_info.updated = datetime.now(timezone.utc)

    if status == JobStatusCode.SUCCESSFUL:
        job_info.finished = datetime.now(timezone.utc)
        job_info.links.append(
            Link.model_validate(
                {
                    "href": f"/jobs/{job_info.jobID}/results",
                    "rel": "results",
                    "type": "application/json",
                }
            )
        )

    if message:
        job_info.message = message

    job_status_cache.put(job_key, job_info)
    logger.debug(f"Updated progress for job {job_id}: {progress}%, {message}")


# ---------------------------------------------------------------------------
# Parallel / scatter subtask progress tracking
# ---------------------------------------------------------------------------

_SUBTASK_PROGRESS_KEY = "fp:subtask_progress"


def _increment_and_report_progress(job_id: str, total: int) -> None:
    """
    Atomically increments the completed-subtask counter for *job_id* and
    reports proportional progress (0-90 %) to the job status cache.

    The remaining 10 % (90-100 %) is reserved for the merge step executed
    by ``finalize_parallel`` / ``finalize_scatter``.

    All exceptions are swallowed so that a Redis hiccup never causes a
    subtask to fail.
    """
    counter_key = f"{_SUBTASK_PROGRESS_KEY}:{job_id}"
    try:
        completed = job_status_cache.redis_connection._execute_redis_command(
            "incr", counter_key
        )
        if completed == 1:
            # Safety TTL so orphaned keys don't accumulate.
            job_status_cache.redis_connection._execute_redis_command(
                "expire", counter_key, 86400
            )
        progress = min(int(completed / total * 90), 90)
        update_job_status(
            job_id,
            progress,
            f"Completed {completed}/{total} subtasks.",
            JobStatusCode.RUNNING,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not update subtask progress for job %s: %r", job_id, exc
        )


def _cleanup_progress_counter(job_id: str) -> None:
    """Deletes the subtask progress counter key for *job_id*."""
    counter_key = f"{_SUBTASK_PROGRESS_KEY}:{job_id}"
    try:
        job_status_cache.redis_connection._execute_redis_command("delete", counter_key)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not clean up progress counter for job %s: %r", job_id, exc
        )


@celery_app.task(bind=True, name="fastprocesses.execute_parallel_item")
def execute_parallel_item(
    self, process_id: str, job_id: str, total: int, serialized_item: str
) -> dict:
    """
    Executes a single parallel work item for a ``BaseParallelProcess``.

    This task is dispatched by the ``execute_process`` task (via a Celery
    ``group``) when the target process is a ``BaseParallelProcess``.  It
    calls ``execute_single`` on the process instance and returns the result
    as a plain dict so that Celery can serialise it through the result backend.

    After a successful execution the task atomically increments a Redis
    counter shared by all sibling tasks and updates the parent job's progress
    to ``(n_done / total) * 90 %``.
    """
    item: dict = json.loads(serialized_item)
    service = get_process_registry().get_process(process_id)

    partial = service.execute_single(item)
    if inspect.isawaitable(partial):
        if asyncio.iscoroutine(partial):
            partial = asyncio.run(partial)
        else:
            async def _await(p=partial):
                return await p
            partial = asyncio.run(_await())

    result = jsonable_encoder(partial)
    _increment_and_report_progress(job_id, total)
    return result


def _run_parallel(
    service: BaseParallelProcess,
    process_id: str,
    data: dict,
    job_id: str,
    serialized_data: str,
) -> None:
    """
    Dispatches a Celery chord for a ``BaseParallelProcess`` and returns
    immediately — no blocking poll.

    The chord header contains one ``execute_parallel_item`` task per item
    returned by ``split_inputs``.  Once all header tasks have completed the
    chord automatically triggers ``finalize_parallel``, which merges the
    results, updates the job status, and caches the final output.

    Because this function returns before any subtask runs, the parent
    ``execute_process`` worker slot is freed instantly.  This is compatible
    with ``worker_prefetch_multiplier=1`` and KEDA autoscaling.
    """
    items = service.split_inputs(data)
    total = len(items)

    if total == 0:
        raise ValueError(
            f"BaseParallelProcess.split_inputs returned an empty list "
            f"for process '{process_id}'."
        )

    logger.info(
        f"Dispatching parallel chord for process {process_id}: "
        f"{total} subtask(s)."
    )

    chord(
        [execute_parallel_item.s(process_id, job_id, total, json.dumps(item)) for item in items]
    )(finalize_parallel.s(process_id, job_id, serialized_data))


@celery_app.task(name="fastprocesses.finalize_parallel")
def finalize_parallel(
    sub_results: list[dict],
    process_id: str,
    job_id: str,
    serialized_data: str,
) -> dict:
    """
    Chord callback for ``BaseParallelProcess``.

    Receives the ordered list of partial results produced by the chord header
    (one dict per ``execute_parallel_item`` task), then:

    1. Calls ``service.merge_results`` to produce the final output.
    2. Caches the merged result under the same key used by ``CacheResultTask``.
    3. Updates the job status to SUCCESSFUL (or FAILED on error).
    """
    service = get_process_registry().get_process(process_id)
    try:
        update_job_status(
            job_id, 95, "Merging parallel results.", JobStatusCode.RUNNING
        )
        result = service.merge_results(sub_results)
        merged = jsonable_encoder(result)

        try:
            calculation_task = CalculationTask(**json.loads(serialized_data))
            temp_result_cache.put(key=calculation_task.celery_key, value=merged)
            # Also store under job_id so get_job_result can retrieve it when
            # execute_process returned None (chord-dispatched tasks).
            temp_result_cache.put(key=job_id, value=merged)
            logger.info(
                f"Cached parallel result for process {process_id} (job {job_id})."
            )
        except Exception as cache_err:
            logger.error(
                f"Failed to cache parallel result for job {job_id}: {cache_err}"
            )

        update_job_status(
            job_id, 100, "Process completed.", JobStatusCode.SUCCESSFUL
        )
        _cleanup_progress_counter(job_id)
        logger.info(
            f"Parallel process {process_id} (job {job_id}) completed successfully."
        )
        return merged
    except Exception as e:
        update_job_status(job_id, 0, str(e), JobStatusCode.FAILED)
        logger.error(
            f"Parallel finalization failed for process {process_id} "
            f"(job {job_id}): {e}"
        )
        raise


@celery_app.task(bind=True, name="fastprocesses.execute_scatter_step")
def execute_scatter_step(
    self, process_id: str, job_id: str, total: int, step_name: str, serialized_data: str
) -> dict:
    """
    Executes one ``@parallel_step`` of a ``BaseScatterProcess``.

    This task is dispatched by the ``execute_process`` task (via a Celery
    ``group``) when the target process is a ``BaseScatterProcess``.  It calls
    the named step method on the process instance and returns the result as a
    plain dict so that Celery can serialise it through the result backend.

    After a successful execution the task atomically increments a Redis
    counter shared by all sibling tasks and updates the parent job's progress
    to ``(n_done / total) * 90 %``.
    """
    data: dict = json.loads(serialized_data)
    service = get_process_registry().get_process(process_id)

    steps = get_parallel_steps(service)
    if step_name not in steps:
        raise ValueError(
            f"Step '{step_name}' not found on process '{process_id}'. "
            f"Available steps: {list(steps)}"
        )

    partial = steps[step_name](data)
    if inspect.isawaitable(partial):
        if asyncio.iscoroutine(partial):
            partial = asyncio.run(partial)
        else:
            async def _await(p=partial):
                return await p
            partial = asyncio.run(_await())

    result = jsonable_encoder(partial)
    _increment_and_report_progress(job_id, total)
    return result


def _run_scatter(
    service: BaseScatterProcess,
    process_id: str,
    data: dict,
    job_id: str,
    serialized_data: str,
) -> None:
    """
    Dispatches a Celery chord for a ``BaseScatterProcess`` and returns
    immediately — no blocking poll.

    The chord header contains one ``execute_scatter_step`` task per
    ``@parallel_step`` method, all receiving the *same* serialised *data*.
    Once all steps have completed the chord triggers ``finalize_scatter``,
    which merges the named results, updates the job status, and caches the
    final output.
    """
    steps = get_parallel_steps(service)
    if not steps:
        raise NotImplementedError(
            f"{service.__class__.__name__} defines no @parallel_step methods."
        )

    step_names = list(steps)
    total = len(step_names)
    serialized_input = json.dumps(data)

    logger.info(
        f"Dispatching scatter chord for process {process_id}: "
        f"{total} step(s): {step_names}."
    )

    chord(
        [
            execute_scatter_step.s(process_id, job_id, total, name, serialized_input)
            for name in step_names
        ]
    )(finalize_scatter.s(step_names, process_id, job_id, serialized_data))


@celery_app.task(name="fastprocesses.finalize_scatter")
def finalize_scatter(
    step_results: list[dict],
    step_names: list[str],
    process_id: str,
    job_id: str,
    serialized_data: str,
) -> dict:
    """
    Chord callback for ``BaseScatterProcess``.

    Receives the ordered list of per-step results produced by the chord header
    (one dict per ``execute_scatter_step`` task), re-associates them with
    their step names, then:

    1. Calls ``service.merge_results({step_name: result_dict, ...})``.
    2. Caches the merged result under the same key used by ``CacheResultTask``.
    3. Updates the job status to SUCCESSFUL (or FAILED on error).
    """
    service = get_process_registry().get_process(process_id)
    try:
        named_results = dict(zip(step_names, step_results))
        exec_body: dict = json.loads(serialized_data)

        update_job_status(
            job_id, 95, "Merging scatter results.", JobStatusCode.RUNNING
        )
        result = service.merge_results(named_results, exec_body)
        merged = jsonable_encoder(result)

        try:
            calculation_task = CalculationTask(**json.loads(serialized_data))
            temp_result_cache.put(key=calculation_task.celery_key, value=merged)
            # Also store under job_id so get_job_result can retrieve it when
            # execute_process returned None (chord-dispatched tasks).
            temp_result_cache.put(key=job_id, value=merged)
            logger.info(
                f"Cached scatter result for process {process_id} (job {job_id})."
            )
        except Exception as cache_err:
            logger.error(
                f"Failed to cache scatter result for job {job_id}: {cache_err}"
            )

        update_job_status(
            job_id, 100, "Process completed.", JobStatusCode.SUCCESSFUL
        )
        _cleanup_progress_counter(job_id)
        logger.info(
            f"Scatter process {process_id} (job {job_id}) completed successfully."
        )
        return merged
    except Exception as e:
        update_job_status(job_id, 0, str(e), JobStatusCode.FAILED)
        logger.error(
            f"Scatter finalization failed for process {process_id} "
            f"(job {job_id}): {e}"
        )
        raise


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
        job_key = f"job:{job_id}"
        raw_job_info = job_status_cache.get(job_key)

        if not raw_job_info:
            logger.warning(
                "Job status for job_id={} not found in cache during progress "
                "callback; skipping progress update.",
                job_id,
            )
            return

        try:
            job_info = JobStatusInfo.model_validate(raw_job_info)
        except Exception as exc:
            logger.error(
                "Failed to validate cached job status in progress callback for "
                "job_id={}: {!r}",
                job_id,
                exc,
            )
            return

        job_info.progress = progress
        job_info.updated = datetime.now(timezone.utc)

        if message:
            job_info.message = message

        job_status_cache.put(job_key, job_info)
        logger.debug(f"Updated progress for job {job_id}: {progress}%, {message}")

    chord_dispatched = False  # True when a parallel/scatter chord is dispatched
    result = None
    job_status = JobStatusCode.RUNNING
    job_message = ""
    data: dict = json.loads(serialized_data)

    logger.info(f"Executing process {process_id} with data {serialized_data[:80]}")
    job_id = self.request.id  # Get the task/job ID

    # First: Get the process
    try:
        logger.info(f"Worker retrieving process {process_id} from registry")
        service = get_process_registry().get_process(process_id)
    except ValueError as e:
        job_status = JobStatusCode.FAILED
        update_job_status(
            job_id,
            0,
            f"Process '{process_id}' not found.",
            job_status,
        )
        raise e
    except ProcessClassNotFoundError as e:
        raise e

    # Second: deep validation of inputs
    try:
        logger.info(f"Worker validating inputs for process {process_id}")
        # BUG: if redis returns no job_status, this fails and creates a ValueError too
        update_job_status(
            job_id,
            0,
            "Validating inputs. This may take a while for complex inputs.",
            job_status,
        )
        service.validate_inputs(data["inputs"])
    except ValueError as e:
        logger.error(f"Input validation failed for process {process_id}: {str(e)}")
        job_status = JobStatusCode.FAILED
        # BUG: if redis returns no job_status, this fails and creates a ValueError too
        update_job_status(
            job_id,
            0,
            str(e),
            job_status,
        )
        raise InputValidationError(process_id, repr(e))

    # Third: Execute the process
    try:
        logger.info("Worker executing process {}", process_id)
        job_status = JobStatusCode.RUNNING
        # BUG: if redis returns no job_status, this fails and creates a ValueError too
        update_job_status(
            job_id,
            0,
            "Process started",
            job_status,
            started=datetime.now(timezone.utc),
        )

        if isinstance(service, BaseParallelProcess):
            # Data fan-out: split → N parallel subtasks (same op) → merge.
            # Chord dispatched; this task returns immediately.
            _run_parallel(service, process_id, data, job_id, serialized_data)
            chord_dispatched = True
        elif isinstance(service, BaseScatterProcess):
            # Operation fan-out: N different steps on same input → merge.
            # Chord dispatched; this task returns immediately.
            _run_scatter(service, process_id, data, job_id, serialized_data)
            chord_dispatched = True
        else:
            result = service.run_execute(
                data, job_progress_callback=job_progress_callback
            )

    except SoftTimeLimitExceeded as e:
        logger.warning(f"Task {job_id} hit the soft time limit: {e}")
        if chord_dispatched:
            raise  # chord tasks are independent; cannot resume here
        # Attempt to resume the process
        try:
            result = service.run_execute(
                data, job_progress_callback=job_progress_callback
            )

        except Exception as inner_exception:
            logger.error(
                f"Error while completing task after soft time limit: {inner_exception}"
            )

            raise e

        logger.info(f"Process {process_id} completed after soft time limit")
        job_status = JobStatusCode.SUCCESSFUL

    # intercept all errors coming from the process` execution method
    except Exception as e:
        # Update job with error status
        job_status = JobStatusCode.FAILED

        # decide if its a validation error or a general error
        if isinstance(e, ValueError) or isinstance(e, ValidationError):
            logger.error(f"Validation error in process {process_id}: {e}")
            job_message = e
            raise e

        # get exception traceback for logging, in other cases
        # (hiding app interna for security reasons)
        user_frame = traceback.TracebackException.from_exception(e).stack

        logger.exception(str(e), exc_info=True)

        job_message = (
            f"Error in {service.__class__.__name__}.{user_frame[-1].name} "
            f"line: '{user_frame[-1].line}' (lineno: {user_frame[-1].lineno})"
        )

        # this information will be written to celery job
        # results and thus to /job/{job_id}/results
        raise Exception(
            job_message
        )

    finally:
        if chord_dispatched:
            # finalize_parallel / finalize_scatter handles status + caching.
            return None

        if result:
            result_dump = jsonable_encoder(result)
            logger.info(
                f"Process {process_id} executed "
                f"successfully with result {json.dumps(result_dump)[:80]}"
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
        f"Process {service.__class__.__name__} execution completed. No result returned"
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
