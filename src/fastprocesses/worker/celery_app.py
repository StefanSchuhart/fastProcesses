# worker/celery_app.py
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from fastprocesses.common import celery_app, redis_cache
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


class CacheResultTask(Task):
    def on_success(self, retval: dict | BaseModel, task_id, args, kwargs):
        try:
            # Deserialize the original data
            original_data = json.loads(args[1])
            calculation_task = CalculationTask(**original_data)

            # Get the the hash key for the task
            key = calculation_task.celery_key

            # Store the result in cache
            # Use the task ID as the key
            redis_cache.put(key=key, value=retval)

            logger.info(f"Saved result with key {key} to cache: {retval}")
        except Exception as e:
            logger.error(f"Error caching results: {e}")


@celery_app.task(bind=True, name="execute_process", base=CacheResultTask)
def execute_process(self, process_id: str, version: str, serialized_data: Dict[str, Any]):
    """
    Executes a process with the given process ID and version.

    Args:
        process_id (str): The ID of the process to execute.
        version (str): The version of the process to execute.
        serialized_data (Dict[str, Any]): The serialized input data for the process.

    Returns:
        Any: The result of the process execution.
    """
    data = json.loads(serialized_data)

    logger.info(f"Executing process {process_id} (version {version}) with data {data}")
    job_id = self.request.id  # Get the task/job ID

    # Create a progress update function that captures the job_id
    def update_progress(progress: int, message: str = None, status: str | None = None):
        job_key = f"job:{job_id}"
        job_info = JobStatusInfo.model_validate(redis_cache.get(job_key))

        job_info.status = status or JobStatusCode.RUNNING
        job_info.progress = progress
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

        redis_cache.put(job_key, job_info)
        logger.debug(f"Updated progress for job {job_id}: {progress}%, {message}")

    try:
        # Initialize progress
        update_progress(0, "Starting process")

        # Retrieve the correct version of the process
        service = get_process_registry().get_process(process_id, version)

        if asyncio.iscoroutinefunction(service.execute):
            result = asyncio.run(
                service.execute(
                    data,
                    progress_callback=update_progress,
                )
            )
        else:
            result = service.execute(data)

    except SoftTimeLimitExceeded as e:
        logger.warning(f"Task {job_id} hit the soft time limit: {e}")
        # Attempt to resume the process
        try:
            if asyncio.iscoroutinefunction(service.execute):
                result = asyncio.run(service.execute(data))
            else:
                result = service.execute(data)

            logger.info(f"Process {process_id} completed after soft time limit")
            update_progress(
                100,
                "Process completed after soft time limit",
                status=JobStatusCode.SUCCESSFUL,
            )
            return result

        except Exception as inner_exception:
            logger.error(
                f"Error while completing task after soft time limit: {inner_exception}"
            )
            raise inner_exception

    except Exception as e:
        # Update job with error status

        update_progress(100, "Process completed", status=JobStatusCode.FAILED)

        logger.error(f"Error executing process {process_id}: {e}")
        raise

    result_dump = jsonable_encoder(result)
    logger.info(
        f"Process {process_id} executed "
        f"successfully with result {json.dumps(result_dump)[:80]}"
    )

    # Mark job as complete
    update_progress(100, "Process completed", status=JobStatusCode.SUCCESSFUL)

    return result


@celery_app.task(name="check_cache")
def check_cache(calculation_task: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check if results exist in cache and return status
    """
    task = CalculationTask(**calculation_task)
    cached_result = redis_cache.get(key=task.celery_key)

    if cached_result:
        logger.info(f"Cache hit for key {task.celery_key}")
        return {"status": "HIT", "result": cached_result}

    logger.info(f"Cache miss for key {task.celery_key}")
    return {"status": "MISS"}


@celery_app.task(name="find_result_in_cache")
def find_result_in_cache(celery_key: str) -> dict | None:
    """
    Retrieve result from cache
    """
    result = redis_cache.get(key=celery_key)
    if result:
        logger.info(f"Retrieved result from cache for key {celery_key}")
    return result
