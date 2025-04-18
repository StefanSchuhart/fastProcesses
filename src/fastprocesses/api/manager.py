import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from celery.result import AsyncResult

from fastprocesses.common import celery_app, redis_cache
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
    ProcessSummary,
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
        # dump data to json
        serialized_data = json.dumps(
            calculation_task.model_dump(include={"inputs", "outputs", "response"})
        )

        # Submit task to Celery worker queue for background processing
        task = self.process_manager.celery_app.send_task(
            "execute_process", args=[process_id, serialized_data]
        )

        # Initialize job metadata in cache with status 'accepted'
        job_status = JobStatusInfo.model_validate(
            {
                "jobID": task.id,
                "status": "accepted",
                "type": "process",
                "process_id": process_id,
                "created": datetime.now(timezone.utc),
                "updated": datetime.now(timezone.utc),
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
        self.process_manager.cache.put(f"job:{task.id}", job_status)

        return ProcessExecResponse(status="accepted", jobID=task.id, type="process")


class SyncExecutionStrategy(ExecutionStrategy):
    """Strategy for synchronous execution."""

    def execute(
        self, process_id: str, calculation_task: CalculationTask
    ) -> ProcessExecResponse:
        process = self.process_manager.process_registry.get_process(process_id)
        # TODO: response type and outputs must be passed, too
        result = process.execute(calculation_task.inputs)

        task = self.process_manager.celery_app.send_task(
            "store_result", args=[process_id, calculation_task.celery_key, result]
        )

        job_status = JobStatusInfo.model_validate(
            {
                "jobID": task.id,
                "status": JobStatusCode.ACCEPTED,
                "type": "process",
                "process_id": process_id,
                "created": datetime.now(timezone.utc),
                "updated": datetime.now(timezone.utc),
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
        self.process_manager.cache.put(f"job:{task.id}", job_status)

        return ProcessExecResponse(
            status="successful", jobID=task.id, type="process", value=result
        )


class ProcessManager:
    """Manages processes, including execution, status checking, and job management."""

    def __init__(self):
        """Initializes the ProcessManager with Celery app and process registry."""
        self.celery_app = celery_app
        self.process_registry = get_process_registry()
        self.cache = redis_cache

    def get_available_processes(
        self, limit: int, offset: int
    ) -> Tuple[List[ProcessSummary], str]:
        logger.info("Retrieving available processes")
        """
        Retrieves a list of available processes.

        Returns:
            List[ProcessDescription]: A list of process descriptions.
        """
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
        logger.info(f"Retrieving description for process ID: {process_id}")
        """
        Retrieves the description of a specific process.

        Args:
            process_id (str): The ID of the process.

        Returns:
            ProcessDescription: The description of the process.

        Raises:
            ValueError: If the process is not found.
        """
        if not self.process_registry.has_process(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        service = self.process_registry.get_process(process_id)
        return service.get_description()

    def execute_process(
        self,
        process_id: str,
        data: ProcessExecRequestBody,
        execution_mode: ExecutionMode,
    ) -> ProcessExecResponse:
        """
        Main process execution orchestration:
        1. Validates process existence and input data
        2. Checks result cache to avoid recomputation
        3. Selects execution strategy (sync/async)
        4. Delegates execution to appropriate strategy

        Args:
            process_id: Identifier for the process to execute
            data: Contains input parameters and execution mode

        Returns:
            ProcessExecResponse with job status and ID

        Raises:
            ValueError: If process not found or input validation fails
        """
        logger.info(f"Executing process ID: {process_id}")

        # Validate process exists
        if not self.process_registry.has_process(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        logger.debug(f"Process {process_id} found in registry")

        # Get service and validate inputs
        service = self.process_registry.get_process(process_id)
        try:
            service.validate_inputs(data.inputs)
        except ValueError as e:
            logger.error(f"Input validation failed for process {process_id}: {str(e)}")
            raise ValueError(f"Input validation failed: {str(e)}")

        try:
            service.validate_outputs(data.outputs)
        except ValueError as e:
            logger.error(f"Output validation failed for process {process_id}: {str(e)}")
            raise ValueError(f"Output validation failed: {str(e)}")

        # Create calculation task
        calculation_task = CalculationTask(
            inputs=data.inputs, outputs=data.outputs, response=data.response
        )

        # Check cache first
        cached_result = self._check_cache(calculation_task)
        if cached_result:
            logger.info(f"Result found in cache for process {process_id}")
            return cached_result

        # Select execution strategy based on mode
        execution_strategies = {
            ExecutionMode.SYNC: SyncExecutionStrategy(self),
            ExecutionMode.ASYNC: AsyncExecutionStrategy(self),
        }

        strategy: SyncExecutionStrategy | AsyncExecutionStrategy = execution_strategies[
            execution_mode
        ]

        return strategy.execute(process_id, calculation_task)

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Retrieves the status of a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The status of the job.

        Raises:
            ValueError: If the job is not found.
        """
        # Check if job exists in Redis first
        job_info = self.cache.get(f"job:{job_id}")
        if not job_info:
            logger.error(f"Job {job_id} not found in cache")
            raise ValueError(f"Job {job_id} not found")

        # Now check Celery status
        result = AsyncResult(job_id)
        if result.ready():
            if result.successful():
                return {"status": "successful", "type": "process"}
            return {
                "status": "failed",
                "type": "process",
                "message": str(result.result),
            }
        return {"status": "running", "type": "process"}

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
        job_info = self.cache.get(f"job:{job_id}")
        if not job_info:
            logger.error(f"Job {job_id} not found in cache")
            raise ValueError(f"Job {job_id} not found")

        result = AsyncResult(job_id)
        if result.state == "PENDING":
            logger.error(f"Result for job ID {job_id} is not ready")
            raise ValueError("Result not ready")
        elif result.state == "FAILURE":
            logger.error(f"Job ID {job_id} failed with error: {result.result}")
            raise ValueError(f"Job failed: {result.result}")
        elif result.state == "SUCCESS":
            logger.info(f"Job ID {job_id} completed successfully")
            return result.result
        else:
            return {"status": "running", "type": "process"}

    def delete_job(self, job_id: str) -> Dict[str, Any]:
        logger.info(f"Deleting job ID: {job_id}")
        """
        Deletes a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The status of the deletion.

        Raises:
            ValueError: If the job is not found.
        """
        result = AsyncResult(job_id)
        if not result:
            logger.error("Job not found")
            raise ValueError("Job not found")
        result.forget()
        return {"status": "dismissed", "message": "Job dismissed"}

    def get_jobs(self, limit: int, offset: int) -> List[Dict[str, Any]]:
        """
        Retrieves a list of all jobs and their status.

        Returns:
            List[Dict[str, Any]]: List of job status information
        """
        # Get all job IDs from Redis
        job_keys = self.cache.keys("job:*")
        jobs = []

        for job_key in job_keys[offset : offset + limit]:
            try:
                job_info = JobStatusInfo.model_validate(self.cache.get(job_key))
                if job_info:
                    jobs.append(job_info)

            except Exception as e:
                logger.error(f"Error retrieving job {job_key}: {e}")

        next_link = None
        if offset + limit < len(job_keys):
            next_link = f"/jobs?limit={limit}&offset={offset + limit}"

        return jobs, next_link

    def _check_cache(
        self, calculation_task: CalculationTask
    ) -> ProcessExecResponse | None:
        """
        Optimizes performance by checking if identical calculation exists in cache.
        Uses task input hash as cache key.

        Args:
            calculation_task: Task containing input parameters

        Returns:
            Cached response if found, None otherwise
        """
        cache_check = self.celery_app.send_task(
            "check_cache", args=[calculation_task.model_dump()]
        )

        cache_status = cache_check.get(timeout=10)
        if cache_status["status"] == "HIT":
            task = self.celery_app.send_task(
                "find_result_in_cache", args=[calculation_task.celery_key]
            )

            job_info = JobStatusInfo.model_validate(
                {
                    "jobID": task.id,
                    "status": JobStatusCode.SUCCESSFUL,
                    "type": "process",
                    "created": datetime.now(timezone.utc),
                    "started": datetime.now(timezone.utc),
                    "finished": datetime.now(timezone.utc),
                    "updated": datetime.now(timezone.utc),
                    "progress": 100,
                    "message": "Result retrieved from cache",
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
            self.cache.put(f"job:{task.id}", job_info)

            return ProcessExecResponse(
                status="successful", jobID=task.id, type="process"
            )

        return None
