import json
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError

import celery.exceptions
import kombu.exceptions
from celery.result import AsyncResult

from fastprocesses.common import (
    celery_app,
    job_status_cache,
    settings,
    temp_result_cache,
)
from fastprocesses.core.exceptions import (
    BrokerUnavailableError,
    InputValidationError,
    JobFailedError,
    JobNotFoundError,
    JobNotReadyError,
    OutputValidationError,
    ProcessClassNotFoundError,
    ProcessNotFoundError,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    CalculationTask,
    ExecutionMode,
    JobStatusCode,
    JobStatusInfo,
    Link,
    ProcessDescription,
    ProcessExecRequestBody,
    ProcessExecResponse,
)
from fastprocesses.processes.process_registry import get_process_registry


class ExecutionStrategy(ABC):
    """
    Abstract base class implementing the Strategy pattern for process execution.
    Different execution modes (sync/async) implement this interface.
    """

    def __init__(self, process_manager):
        self.process_manager: ProcessManager = process_manager

    @abstractmethod
    def execute(
        self, process_id: str, calculation_task: CalculationTask
    ) -> ProcessExecResponse:
        pass


class AsyncExecutionStrategy(ExecutionStrategy):
    """
    Handles asynchronous process execution by:
    1. Submitting task to Celery queue
    2. Creating initial job status in cache
    3. Returning immediately with job ID
    """

    def execute(
        self, process_id: str, calculation_task: CalculationTask
    ) -> ProcessExecResponse:
        # Check cache first
        response = self.process_manager._check_cache(calculation_task, process_id)
        if response:
            logger.info(f"Result found in cache for process {process_id}")

            # return immediately if cache was hit
            return response

        # dump data to json
        serialized_data = json.dumps(
            calculation_task.model_dump(include={"inputs", "outputs", "response"})
        )

        # Submit task to Celery worker queue for background processing
        send_start = time.monotonic()
        try:
            task = self.process_manager.celery_app.send_task(
                "fastprocesses.execute_process", args=[process_id, serialized_data]
            )
        except kombu.exceptions.OperationalError as exc:
            logger.error(
                "Broker unavailable when submitting async task for process_id={}: {}",
                process_id,
                exc,
            )
            raise BrokerUnavailableError(str(exc)) from exc
        send_elapsed = time.monotonic() - send_start
        logger.info(
            "Task queued: process_id={}, job_id={}, mode=async, send_time={:.3f}s",
            process_id,
            task.id,
            send_elapsed,
        )
        if send_elapsed > 1.0:
            logger.warning(
                "Celery async send_task for process_id={} took {:.2f}s",
                process_id,
                send_elapsed,
            )

        # Initialize job metadata in cache with status 'accepted'
        job_status = JobStatusInfo.model_validate(
            {
                "jobID": task.id,
                "status": JobStatusCode.ACCEPTED,
                "type": "process",
                "processID": process_id,
                "created": datetime.now(timezone.utc),
                "progress": 0,
                "links": [
                    Link.model_validate(
                        {
                            "href": f"/jobs/{task.id}",
                            "rel": "self",
                            "type": "application/json",
                        }
                    )
                ],
            }
        )
        self.process_manager.job_status_cache.put(f"job:{task.id}", job_status)

        return ProcessExecResponse(status="accepted", jobID=task.id, type="process")

    def execute_raw(self, process_id: str, raw_body: bytes) -> ProcessExecResponse:
        """Fast path for async execution with large request bodies.

        Sends the original request bytes to Celery without any Python-level
        re-serialisation (no jsonable_encoder, no model_dump, no json.dumps).
        Cache lookup is skipped here and delegated to the worker, which already
        checks the cache before executing.

        This eliminates up to 4 full Python-level passes through the input data
        before returning 201 Accepted, cutting response time from O(payload_size)
        to O(network_upload + Redis_send).
        """
        # raw_body is already valid JSON matching the CalculationTask shape
        # (ProcessExecRequestBody has the same inputs/outputs/response fields;
        # any extra field like 'mode' is silently ignored by CalculationTask).
        serialized_data = raw_body.decode()

        send_start = time.monotonic()
        try:
            task = self.process_manager.celery_app.send_task(
                "fastprocesses.execute_process", args=[process_id, serialized_data]
            )
        except kombu.exceptions.OperationalError as exc:
            logger.error(
                "Broker unavailable when submitting async task for process_id={}: {}",
                process_id,
                exc,
            )
            raise BrokerUnavailableError(str(exc)) from exc
        send_elapsed = time.monotonic() - send_start
        logger.info(
            "Task queued: process_id={}, job_id={},"
            " mode=async (raw), send_time={:.3f}s",
            process_id,
            task.id,
            send_elapsed,
        )
        if send_elapsed > 1.0:
            logger.warning(
                "Celery async send_task for process_id={} took {:.2f}s",
                process_id,
                send_elapsed,
            )

        job_status = JobStatusInfo.model_validate(
            {
                "jobID": task.id,
                "status": JobStatusCode.ACCEPTED,
                "type": "process",
                "processID": process_id,
                "created": datetime.now(timezone.utc),
                "progress": 0,
                "links": [
                    Link.model_validate(
                        {
                            "href": f"/jobs/{task.id}",
                            "rel": "self",
                            "type": "application/json",
                        }
                    )
                ],
            }
        )
        self.process_manager.job_status_cache.put(f"job:{task.id}", job_status)

        return ProcessExecResponse(status="accepted", jobID=task.id, type="process")


class SyncExecutionStrategy(ExecutionStrategy):
    """Strategy for synchronous execution."""

    def execute(
        self, process_id: str, calculation_task: CalculationTask
    ) -> ProcessExecResponse | Any:
        result: Any = None

        # Check cache first
        response = self.process_manager._get_cached_result(calculation_task)
        if response:
            logger.info(f"Result found in cache for process {process_id}")

            # return results immediately if cache was hit
            return response

        # Submit task to Celery worker queue for background processing
        serialized_data = json.dumps(
            calculation_task.model_dump(include={"inputs", "outputs", "response"})
        )
        send_start = time.monotonic()
        try:
            task = self.process_manager.celery_app.send_task(
                "fastprocesses.execute_process", args=[process_id, serialized_data]
            )
        except kombu.exceptions.OperationalError as exc:
            logger.error(
                "Broker unavailable when submitting sync task for process_id={}: {}",
                process_id,
                exc,
            )
            raise BrokerUnavailableError(str(exc)) from exc
        send_elapsed_sync = time.monotonic() - send_start
        logger.info(
            "Task queued: process_id={}, job_id={}, mode=sync, send_time={:.3f}s",
            process_id,
            task.id,
            send_elapsed_sync,
        )
        send_elapsed = time.monotonic() - send_start
        if send_elapsed > 1.0:
            logger.warning(
                "Celery sync send_task for process_id={} took {:.2f}s",
                process_id,
                send_elapsed,
            )

        # Initialize job metadata in cache with status 'running'
        job_status = JobStatusInfo.model_validate(
            {
                "jobID": task.id,
                "status": JobStatusCode.RUNNING,
                "type": "process",
                "processID": process_id,
                "created": datetime.now(timezone.utc),
                "progress": 0,
                "links": [
                    Link.model_validate(
                        {
                            "href": f"/jobs/{task.id}",
                            "rel": "self",
                            "type": "application/json",
                        }
                    )
                ],
            }
        )
        self.process_manager.job_status_cache.put(f"job:{task.id}", job_status)

        # Wait for result with timeout
        deadline = time.monotonic() + settings.FP_SYNC_EXECUTION_TIMEOUT_SECONDS
        async_result = AsyncResult(task.id)
        try:
            get_start = time.monotonic()
            result = async_result.get(
                timeout=settings.FP_SYNC_EXECUTION_TIMEOUT_SECONDS
            )
            get_elapsed = time.monotonic() - get_start
            if get_elapsed > 1.0:
                logger.warning(
                    "Celery AsyncResult.get for sync job_id={} took {:.2f}s",
                    task.id,
                    get_elapsed,
                )

        except celery.exceptions.TimeoutError:
            logger.error(
                f"Synchronous execution for job {task.id} timed out after "
                f"{settings.FP_SYNC_EXECUTION_TIMEOUT_SECONDS} seconds."
            )
            # Return ProcessExecResponse with status 'running', no result yet
            response = ProcessExecResponse(
                status="running", jobID=task.id, type="process"
            )
            return response
        except Exception as e:
            logger.error(f"Synchronous execution for job {task.id} failed: {e}")
            raise JobFailedError(task.id, repr(e))

        # Chord-dispatched processes (BaseParallelProcess / BaseScatterProcess)
        # return None from execute_process while the actual merged result is
        # stored in temp_result_cache by the finalize_* callback.  Poll the
        # cache under the job_id key for the remaining sync window.
        if result is None:
            while time.monotonic() < deadline:
                result = self.process_manager.cache.get(key=task.id)
                if result is not None:
                    break
                time.sleep(0.1)
            if result is None:
                # Finalize task did not complete within the sync timeout.
                # Tell the client to poll /jobs/{job_id}/results instead.
                response = ProcessExecResponse(
                    status="running", jobID=task.id, type="process"
                )
                return response

        # Update job status to successful
        job_status = JobStatusInfo.model_validate(
            {
                "jobID": task.id,
                "status": JobStatusCode.SUCCESSFUL,
                "type": "process",
                "processID": process_id,
                "created": job_status.created,
                "finished": datetime.now(timezone.utc),
                "updated": datetime.now(timezone.utc),
                "progress": 100,
                "links": [
                    Link.model_validate(
                        {
                            "href": f"/jobs/{task.id}/results",
                            "rel": "results",
                            "type": "application/json",
                        }
                    ),
                    Link.model_validate(
                        {
                            "href": f"/jobs/{task.id}",
                            "rel": "self",
                            "type": "application/json",
                        }
                    ),
                ],
            }
        )
        self.process_manager.job_status_cache.put(f"job:{task.id}", job_status)

        return result


class ProcessManager:
    """Manages processes, including execution, status checking, and job management."""

    def __init__(self):
        """Initializes the ProcessManager with Celery app and process registry."""
        self.celery_app = celery_app
        self.process_registry = get_process_registry()
        self.cache = temp_result_cache
        self.job_status_cache = job_status_cache

    def get_worker_status(self) -> Dict[str, Any]:
        """Return basic Celery worker status derived from broker inspect.

        This is primarily intended for API health/diagnostics endpoints so
        external systems (or operators) can see whether any workers are
        currently online and how many tasks they report as active/reserved.
        """

        try:
            inspector = self.celery_app.control.inspect(timeout=1.0)
        except kombu.exceptions.OperationalError as exc:
            logger.error(
                "Celery worker status check failed due to broker error: {}",
                exc,
            )
            raise BrokerUnavailableError(str(exc)) from exc

        if inspector is None:
            # No response from any workers; treat as zero-online but not fatal.
            return {"workers_online": 0, "stats": {}, "active": {}, "reserved": {}}

        stats = inspector.stats() or {}
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}

        # Only expose lightweight aggregates by default; raw stats may be large.
        return {
            "workers_online": len(stats),
            "active": {name: len(tasks or []) for name, tasks in active.items()},
            "reserved": {name: len(tasks or []) for name, tasks in reserved.items()},
            "stats": stats,
        }

    def get_available_processes(
        self, limit: int, offset: int
    ) -> Tuple[List[ProcessDescription], str | None]:
        """
        Retrieves a list of available processes.

        Returns:
            List[ProcessDescription]: A list of process descriptions.
        """
        
        logger.info("Retrieving available processes")
        process_ids = self.process_registry.get_process_ids()

        processes = [
            self.get_process_description(process_id)
            for process_id in process_ids[offset : offset + limit]
        ]
        next_link = None

        if offset + limit < len(process_ids):
            next_link = f"/processes?limit={limit}&offset={offset + limit}"
        return processes, next_link

    def get_process_description(self, process_id: str) -> ProcessDescription:
        """
        Retrieves the description of a specific process.

        Args:
            process_id (str): The ID of the process.

        Returns:
            ProcessDescription: The description of the process.

        Raises:
            ValueError: If the process is not found.
        """

        logger.info(f"Retrieving description for process ID: {process_id}")
        if not self.process_registry.has_process(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ProcessNotFoundError(process_id)

        try:
            process = self.process_registry.get_process(process_id)

        except ValueError as e:
            raise e
        except ProcessClassNotFoundError as e:
            raise e
        
        
        return process.get_description()

    def execute_process(
        self,
        process_id: str,
        raw_body: bytes,
        execution_mode: ExecutionMode,
    ) -> ProcessExecResponse | Any:
        """
        Main process execution orchestration:
        1. Validates process existence and input data
        2. Checks result cache to avoid recomputation
        3. Selects execution strategy (sync/async)
        4. Delegates execution to appropriate strategy

        Args:
            process_id: Identifier for the process to execute
            raw_body: Raw JSON request body bytes
            execution_mode: Sync or async execution mode

        Returns:
            ProcessExecResponse with job status and ID

        Raises:
            ValueError: If process not found or input validation fails
        """
        logger.info(f"Executing process ID: {process_id}")

        # Validate process exists
        if not self.process_registry.has_process(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ProcessNotFoundError(process_id)

        logger.debug(f"Process {process_id} found in registry")

        # Parse once with Pydantic v2's Rust-based JSON parser for validation.
        # For async execution the parsed object is NOT used for re-serialisation;
        # the original bytes are forwarded to Celery directly.
        try:
            data = ProcessExecRequestBody.model_validate_json(raw_body)
        except ValidationError as e:
            logger.error(f"Request body validation failed for process {process_id}: {e}")
            raise InputValidationError(process_id, repr(e))

        # Get process and validate inputs
        process = self.process_registry.get_process(process_id)

        try:
            process.quick_validate_inputs(data.inputs)
        except ValueError as e:
            logger.error(f"Input validation failed for process {process_id}: {str(e)}")
            raise InputValidationError(process_id, repr(e))

        try:
            process.validate_outputs(data.outputs)
        except ValueError as e:
            logger.error(f"Output validation failed for process {process_id}: {str(e)}")
            raise OutputValidationError(process_id, repr(e))

        if execution_mode == ExecutionMode.ASYNC:
            # Fast path: send original bytes to Celery without re-serialisation.
            # Avoids jsonable_encoder + model_dump + json.dumps — up to 4 full
            # Python-level passes through the input data — before returning 201.
            # Cache lookup is handled by the worker (see find_result_in_cache).
            return AsyncExecutionStrategy(self).execute_raw(process_id, raw_body)

        # Sync path: build CalculationTask for cache key and result retrieval.
        # Sync execution is intended for fast/small jobs where this overhead is acceptable.
        calculation_task = CalculationTask(
            inputs=data.inputs, outputs=data.outputs, response=data.response
        )
        return SyncExecutionStrategy(self).execute(process_id, calculation_task)

    def get_job_status(self, job_id: str) -> JobStatusInfo:
        """
        Retrieves the status of a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The status of the job.

        Raises:
            ValueError: If the job is not found.
        """
        # Retrieve the job from Redis
        job_info_raw = self.job_status_cache.get(f"job:{job_id}")

        if not job_info_raw:
            logger.error(f"Job {job_id} not found in cache")
            raise JobNotFoundError(f"Job {job_id} not found")

        job_info = JobStatusInfo.model_validate(job_info_raw)

        return job_info

    def get_job_result(self, job_id: str) -> Dict[str, Any]:
        """
        Retrieves the result of a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The result of the job.

        Raises:
            ValueError: If the job is not found.
        """
        # Check if job exists in Redis first
        job_info = self.job_status_cache.get(f"job:{job_id}")
        if not job_info:
            logger.error(f"Job {job_id} not found in cache")
            raise JobNotFoundError(f"Job {job_id} not found")

        result = AsyncResult(job_id)

        # TODO: if the job was found, but result is retrieved from cache AND celery worker is not running,
        # job status is successful, but result is not ready yet
        if result.state in ("PENDING", "STARTED", "RETRY"):
            logger.error(f"Result for job ID {job_id} is not ready")
            raise JobNotReadyError(job_id)

        if result.state == "FAILURE":
            logger.error(f"J{result.result}")
            raise JobFailedError(job_id, repr(result.result))

        if result.state == "SUCCESS":
            logger.info(f"Job ID {job_id} completed successfully")

        task_result: dict[str, Any] | None = result.result

        # Chord-dispatched processes (BaseParallelProcess / BaseScatterProcess)
        # have execute_process return None while the actual merged result is
        # stored in temp_result_cache by the finalize_* callback task.  Fall
        # back to that secondary key when the Celery result is None.
        if task_result is None:
            job_status_val = job_info.get("status") if job_info else None
            if job_status_val == JobStatusCode.SUCCESSFUL:
                task_result = self.cache.get(key=job_id)
                if task_result is None:
                    raise JobNotReadyError(job_id)
            else:
                raise JobNotReadyError(job_id)

        # in case of SUCCESS only, get the results directly (non-blocking)
        return task_result

    def delete_job(self, job_id: str) -> Dict[str, Any]:
        """
        Deletes a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The status of the deletion.

        Raises:
            ValueError: If the job is not found.
        """

        logger.info(f"Deleting job ID: {job_id}")
        result = AsyncResult(job_id)
        if not result:
            logger.error("Job not found")
            raise ValueError("Job not found")
        result.forget()
        return {"status": "dismissed", "message": "Job dismissed"}

    def get_jobs(
        self, limit: int, offset: int
    ) -> Tuple[List[JobStatusInfo], str | None]:
        """
        Retrieves a list of all jobs and their status.

        Returns:
            List[Dict[str, Any]]: List of job status information
        """
        # Get all job IDs from Redis
        job_keys = self.job_status_cache.keys("job:*")
        jobs: List[JobStatusInfo] = []

        for job_key in job_keys[offset : offset + limit]:
            try:
                job_info = JobStatusInfo.model_validate(
                    self.job_status_cache.get(job_key)
                )
                if job_info:
                    jobs.append(job_info)

            except Exception as e:
                logger.error(f"Error retrieving job {job_key}: {e}")

        next_link = None
        if offset + limit < len(job_keys):
            next_link = f"/jobs?limit={limit}&offset={offset + limit}"

        return jobs, next_link

    # TODO: fast worker lane: dedicated queue for cache retrieval
    def _check_cache(
        self, calculation_task: CalculationTask, process_id: str
    ) -> ProcessExecResponse | None:
        """
        Optimizes performance by checking if identical calculation exists in cache.
        Uses task input hash as cache key.

        Args:
            calculation_task: Task containing input parameters

        Returns:
            Cached response if found, None otherwise
        """
        cached_result = temp_result_cache.get(key=calculation_task.celery_key)

        if cached_result:
            logger.info(f"Cache hit for key {calculation_task.celery_key}")

            task = self.celery_app.send_task(
                "fastprocesses.find_result_in_cache", args=[calculation_task.celery_key]
            )

            job_info = JobStatusInfo.model_validate(
                {
                    "jobID": task.id,
                    "processID": process_id,
                    "status": JobStatusCode.ACCEPTED,
                    "type": "process",
                    "created": datetime.now(timezone.utc),
                    "started": datetime.now(timezone.utc),
                    "finished": None,
                    "updated": None,
                    "progress": 0,
                    "message": "Result will be retrieved from cache.",
                    "links": [
                        Link.model_validate(
                            {
                                "href": f"/jobs/{task.id}",
                                "rel": "self",
                                "type": "application/json",
                            }
                        ),
                    ],
                }
            )
            self.job_status_cache.put(f"job:{task.id}", job_info)

            return ProcessExecResponse(
                status="accepted", jobID=task.id, type="process"
            )

        return None

    def _get_cached_result(self, calculation_task: CalculationTask) -> Any | None:
        """
        Checks if the result for the given calculation task is already cached.
        If found, retrieves the result from the cache.
        Args:
            calculation_task (CalculationTask): The task containing input parameters.
        Returns:
            ProcessExecResponse | None: The cached result if found, otherwise None.
        """
        # first, check for existence of the cached result
        cached_result = temp_result_cache.get(key=calculation_task.celery_key)

        if cached_result:
            # Retrieve and return the actual result
            send_start = time.monotonic()
            task = self.celery_app.send_task(
                "fastprocesses.find_result_in_cache", args=[calculation_task.celery_key]
            )
            send_elapsed = time.monotonic() - send_start
            if send_elapsed > 1.0:
                logger.warning(
                    "Celery send_task for cache retrieval key={} took {:.2f}s",
                    calculation_task.celery_key,
                    send_elapsed,
                )

            # for synchronous execution, we can block here, but must set a graceful timeout
            get_start = time.monotonic()
            result = task.get(timeout=settings.FP_SYNC_EXECUTION_TIMEOUT_SECONDS)
            get_elapsed = time.monotonic() - get_start
            if get_elapsed > 1.0:
                logger.warning(
                    "Celery cache retrieval task.get for key={} took {:.2f}s",
                    calculation_task.celery_key,
                    get_elapsed,
                )
            return result

        return None
