import asyncio
import inspect
import json
from typing import Any, cast

from celery import chord
from fastapi.encoders import jsonable_encoder

from fastprocesses.common import celery_app, temp_result_cache
from fastprocesses.core.base_process import (
    BaseParallelProcess,
    BaseScatterProcess,
    get_parallel_steps,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import CalculationTask, JobStatusCode
from fastprocesses.processes.process_registry import get_process_registry
from fastprocesses.worker.job_status import (
    _cleanup_progress_counter,
    _increment_and_report_progress,
    update_job_status,
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
    try:
        item: dict = json.loads(serialized_item)
        process = cast(
            BaseParallelProcess,
            get_process_registry().get_process(process_id),
        )

        partial = process.execute_single(item)
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
    except Exception as exc:
        update_job_status(
            job_id,
            0,
            f"Parallel subtask failed: {exc}",
            JobStatusCode.FAILED,
            process_id=process_id,
        )
        raise


def _run_parallel(
    process: BaseParallelProcess,
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
    items = process.split_inputs(data)
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

    execute_parallel_item_task = cast(Any, execute_parallel_item)
    finalize_parallel_task = cast(Any, finalize_parallel)
    chord(
        [
            execute_parallel_item_task.s(
                process_id,
                job_id,
                total,
                json.dumps(item),
            )
            for item in items
        ]
    )(finalize_parallel_task.s(process_id, job_id, serialized_data))


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

    1. Calls ``process.merge_results`` to produce the final output.
    2. Caches the merged result under the same key used by ``CacheResultTask``.
    3. Updates the job status to SUCCESSFUL (or FAILED on error).
    """
    process = cast(BaseParallelProcess, get_process_registry().get_process(process_id))
    try:
        update_job_status(
            job_id, 95, "Merging parallel results.", JobStatusCode.RUNNING
        )
        result = process.merge_results(sub_results)
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
        update_job_status(job_id, 0, "Parallel merge failed. See server logs.", JobStatusCode.FAILED)
        logger.error(
            "Parallel finalization failed for process {} (job {}): {}",
            process_id,
            job_id,
            e,
            exc_info=True,
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
    try:
        data: dict = json.loads(serialized_data)
        process = cast(
            BaseScatterProcess,
            get_process_registry().get_process(process_id),
        )

        steps = get_parallel_steps(process)
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
    except Exception as exc:
        update_job_status(
            job_id,
            0,
            f"Scatter step '{step_name}' failed: {exc}",
            JobStatusCode.FAILED,
            process_id=process_id,
        )
        raise


def _run_scatter(
    process: BaseScatterProcess,
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
    steps = get_parallel_steps(process)
    if not steps:
        raise NotImplementedError(
            f"{process.__class__.__name__} defines no @parallel_step methods."
        )

    step_names = list(steps)
    total = len(step_names)
    serialized_input = json.dumps(data)

    logger.info(
        f"Dispatching scatter chord for process {process_id}: "
        f"{total} step(s): {step_names}."
    )

    execute_scatter_step_task = cast(Any, execute_scatter_step)
    finalize_scatter_task = cast(Any, finalize_scatter)
    chord(
        [
            execute_scatter_step_task.s(
                process_id,
                job_id,
                total,
                name,
                serialized_input,
            )
            for name in step_names
        ]
    )(finalize_scatter_task.s(step_names, process_id, job_id, serialized_data))


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

    1. Calls ``process.merge_results({step_name: result_dict, ...})``.
    2. Caches the merged result under the same key used by ``CacheResultTask``.
    3. Updates the job status to SUCCESSFUL (or FAILED on error).
    """
    process = cast(BaseScatterProcess, get_process_registry().get_process(process_id))
    try:
        named_results = dict(zip(step_names, step_results))
        exec_body: dict = json.loads(serialized_data)

        update_job_status(
            job_id, 95, "Merging scatter results.", JobStatusCode.RUNNING
        )
        result = process.merge_results(named_results, exec_body)
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
        update_job_status(job_id, 0, "Scatter merge failed. See server logs.", JobStatusCode.FAILED)
        logger.error(
            "Scatter finalization failed for process {} (job {}): {}",
            process_id,
            job_id,
            e,
            exc_info=True,
        )
        raise
