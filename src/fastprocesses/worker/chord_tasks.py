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


# ---------------------------------------------------------------------------
# Claim-check key helpers
#
# Large payloads are stored in temp_result_cache under these keys so that
# Celery task messages in the broker only carry small string references.
# This prevents the broker queue from growing proportionally to the number
# of scatter/parallel steps and decouples payload memory pressure from the
# task-queue infrastructure.
# ---------------------------------------------------------------------------


def _payload_key(job_id: str) -> str:
    """Scatter: stores {"step_input": data, "original_input": original_dict}."""
    return f"chord:payload:{job_id}"


def _item_key(job_id: str, index: int) -> str:
    """Parallel: stores one split item."""
    return f"chord:item:{job_id}:{index}"


def _meta_key(job_id: str) -> str:
    """Parallel finalize: stores the original input dict."""
    return f"chord:meta:{job_id}"


def _result_key(job_id: str, identifier: str) -> str:
    """Subtask result: stored in temp_result_cache, not in the Celery result backend."""
    return f"chord:result:{job_id}:{identifier}"


@celery_app.task(bind=True, name="fastprocesses.execute_parallel_item")
def execute_parallel_item(
    self, process_id: str, job_id: str, total: int, item_key: str
) -> dict:
    """
    Executes a single parallel work item for a ``BaseParallelProcess``.

    The item data is loaded from ``temp_result_cache`` via the claim-check
    key ``item_key`` so that the broker task message only carries a short
    string.  The result is stored back in ``temp_result_cache`` and only a
    tiny marker dict ``{"__claim_check__": rkey}`` is returned through the
    Celery result backend, keeping result-backend memory usage negligible
    regardless of payload size.
    """
    item = temp_result_cache.get(item_key)
    if item is None:
        raise RuntimeError(
            f"Claim-check data not found in cache for key '{item_key}'. "
            "The temp_result_cache entry may have expired or been evicted."
        )
    try:
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
        rkey = _result_key(job_id, self.request.id)
        temp_result_cache.put(key=rkey, value=result)
        _increment_and_report_progress(job_id, total)
        return {"__claim_check__": rkey}
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
    Stores split items and original input in ``temp_result_cache``
    (claim-check) then dispatches a Celery chord without embedding large
    payloads in task messages.
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

    item_keys: list[str] = []
    for i, item in enumerate(items):
        key = _item_key(job_id, i)
        temp_result_cache.put(key=key, value=item)
        item_keys.append(key)

    meta = _meta_key(job_id)
    temp_result_cache.put(key=meta, value=json.loads(serialized_data))

    execute_parallel_item_task = cast(Any, execute_parallel_item)
    finalize_parallel_task = cast(Any, finalize_parallel)
    chord(
        [
            execute_parallel_item_task.s(process_id, job_id, total, key)
            for key in item_keys
        ]
    )(finalize_parallel_task.s(process_id, job_id, meta))


@celery_app.task(name="fastprocesses.finalize_parallel")
def finalize_parallel(
    sub_results: list[dict],
    process_id: str,
    job_id: str,
    meta_key: str,
) -> dict:
    """
    Chord callback for ``BaseParallelProcess``.

    Resolves result claim-checks from ``temp_result_cache``, merges the
    actual results, caches the final output, and updates the job status.
    """
    process = cast(BaseParallelProcess, get_process_registry().get_process(process_id))
    try:
        actual_results: list[dict] = []
        for result_ref in sub_results:
            rkey = result_ref["__claim_check__"]
            result = temp_result_cache.get(rkey)
            if result is None:
                raise RuntimeError(f"Result claim-check not found in cache: {rkey}")
            actual_results.append(result)
            temp_result_cache.delete(rkey)

        update_job_status(
            job_id, 95, "Merging parallel results.", JobStatusCode.RUNNING
        )
        merged = jsonable_encoder(process.merge_results(actual_results))

        original_input = temp_result_cache.get(meta_key)
        temp_result_cache.delete(meta_key)

        try:
            if original_input is not None:
                calculation_task = CalculationTask(**original_input)
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
    self, process_id: str, job_id: str, total: int, step_name: str, payload_key: str
) -> dict:
    """
    Executes one ``@parallel_step`` of a ``BaseScatterProcess``.

    Input data is loaded from ``temp_result_cache`` via ``payload_key`` to
    avoid embedding the full dataset in every broker task message.  The
    result is stored back in ``temp_result_cache`` under a deterministic key
    ``chord:result:{job_id}:{step_name}`` and only a tiny marker dict is
    returned through the Celery result backend.
    """
    payload = temp_result_cache.get(payload_key)
    if payload is None:
        raise RuntimeError(
            f"Claim-check payload not found in cache for key '{payload_key}'. "
            "The temp_result_cache entry may have expired or been evicted."
        )
    data: dict = payload["step_input"]
    try:
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
        rkey = _result_key(job_id, step_name)
        temp_result_cache.put(key=rkey, value=result)
        _increment_and_report_progress(job_id, total)
        return {"__claim_check__": rkey}
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
    Stores the chord payload in ``temp_result_cache`` (claim-check) and
    dispatches a Celery chord without embedding large payloads in task
    messages.  All scatter steps share the same input data, so only one
    cache entry is needed regardless of the number of steps.
    """
    steps = get_parallel_steps(process)
    if not steps:
        raise NotImplementedError(
            f"{process.__class__.__name__} defines no @parallel_step methods."
        )

    step_names = list(steps)
    total = len(step_names)

    logger.info(
        f"Dispatching scatter chord for process {process_id}: "
        f"{total} step(s): {step_names}."
    )

    pkey = _payload_key(job_id)
    temp_result_cache.put(
        key=pkey,
        value={
            "step_input": data,
            "original_input": json.loads(serialized_data),
        },
    )

    execute_scatter_step_task = cast(Any, execute_scatter_step)
    finalize_scatter_task = cast(Any, finalize_scatter)
    chord(
        [
            execute_scatter_step_task.s(process_id, job_id, total, name, pkey)
            for name in step_names
        ]
    )(finalize_scatter_task.s(step_names, process_id, job_id, pkey))


@celery_app.task(name="fastprocesses.finalize_scatter")
def finalize_scatter(
    step_results: list[dict],
    step_names: list[str],
    process_id: str,
    job_id: str,
    payload_key: str,
) -> dict:
    """
    Chord callback for ``BaseScatterProcess``.

    Resolves result claim-checks and the original input from
    ``temp_result_cache``, merges the step results, caches the final output,
    and updates the job status.
    """
    process = cast(BaseScatterProcess, get_process_registry().get_process(process_id))
    try:
        named_results: dict[str, dict] = {}
        for step_name, result_ref in zip(step_names, step_results):
            rkey = result_ref["__claim_check__"]
            result = temp_result_cache.get(rkey)
            if result is None:
                raise RuntimeError(f"Result claim-check not found in cache: {rkey}")
            named_results[step_name] = result
            temp_result_cache.delete(rkey)

        payload = temp_result_cache.get(payload_key)
        temp_result_cache.delete(payload_key)
        exec_body: dict = payload["original_input"] if payload else {}

        update_job_status(
            job_id, 95, "Merging scatter results.", JobStatusCode.RUNNING
        )
        result = process.merge_results(named_results, exec_body)
        merged = jsonable_encoder(result)

        try:
            calculation_task = CalculationTask(**exec_body)
            temp_result_cache.put(key=calculation_task.celery_key, value=merged)
            # Also store under job_id so get_job_result can retrieve it.
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
