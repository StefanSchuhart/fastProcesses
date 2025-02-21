import hashlib
import json
from typing import Any, Dict, List

from celery.result import AsyncResult

from fastprocesses.common import redis_cache
from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    CalculationTask,
    ProcessDescription,
    ProcessExecRequestBody,
    ProcessExecResponse,
)
from fastprocesses.processes.process_registry import get_process_registry
from fastprocesses.worker.celery_app import celery_app


class ProcessManager:
    """Manages processes, including execution, status checking, and job management."""

    def __init__(self):
        """Initializes the ProcessManager with Celery app and service registry."""
        self.celery_app = celery_app
        self.service_registry = get_process_registry()
        self.cache = redis_cache

    def get_available_processes(self) -> List[ProcessDescription]:
        logger.info("Retrieving available processes")
        """
        Retrieves a list of available processes.

        Returns:
            List[ProcessDescription]: A list of process descriptions.
        """
        return [
            self.get_process_description(process_id)
            for process_id in self.service_registry.get_service_ids()
        ]

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
        if not self.service_registry.has_service(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        service = self.service_registry.get_service(process_id)
        return service.get_description()

    def execute_process(self, process_id: str, data: ProcessExecRequestBody) -> ProcessExecResponse:
        logger.info(f"Executing process ID: {process_id}")
        if not self.service_registry.has_service(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        # Get the service instance
        service = self.service_registry.get_service(process_id)

        try:
            # Validate inputs before processing
            service.validate_inputs(data.inputs)
        except ValueError as e:
            logger.error(f"Input validation failed for process {process_id}: {str(e)}")
            raise ValueError(f"Input validation failed: {str(e)}")
    
        # Generate a hash of the inputs
        calculation_task = CalculationTask(inputs=data.inputs)

        # Check cache using Celery task
        cache_check = self.celery_app.send_task(
            'check_cache',
            args=[calculation_task.model_dump()]
        )
        
        # Wait for cache check result (this is fast as it's just a Redis lookup)
        cache_status = cache_check.get(timeout=1)
        
        if cache_status["status"] == "HIT":
            logger.info(f"Returning cached result for process ID: {process_id}")
            # Create a task to fetch the cached result
            task = self.celery_app.send_task(
                'find_result_in_cache',
                args=[calculation_task.celery_key]
            )
            return ProcessExecResponse(status="successful", jobID=task.id, type="process")

        # No cached result, execute the process
        task = self.celery_app.send_task(
            'execute_process',
            args=[process_id, calculation_task.model_dump()]
        )

        logger.info(f"Enqueued process ID: {process_id} with task ID: {task.id}")
        return ProcessExecResponse(status="accepted", jobID=task.id, type="process")

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        logger.debug(f"Retrieving status for job ID: {job_id}")
        """
        Retrieves the status of a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The status of the job.

        Raises:
            ValueError: If the job is not found.
        """
        result = AsyncResult(job_id)
        if result.ready():
            if result.successful():
                return {"status": "successful", "type": "process"}
            return {"status": "failed", "type": "process", "message": str(result.result)}
        return {"status": "running", "type": "process"}

    def get_job_result(self, job_id: str) -> Dict[str, Any]:
        logger.debug(f"Retrieving result for job ID: {job_id}")
        """
        Retrieves the result of a specific job.

        Args:
            job_id (str): The ID of the job.

        Returns:
            Dict[str, Any]: The result of the job.

        Raises:
            ValueError: If the job is not found.
        """
        result = AsyncResult(job_id)
        if result.state == 'PENDING':
            logger.error(f"Result for job ID {job_id} is not ready")
            raise ValueError("Result not ready")
        elif result.state == 'FAILURE':
            logger.error(f"Job ID {job_id} failed with error: {result.result}")
            raise ValueError(f"Job failed: {result.result}")
        elif result.state == 'SUCCESS':
            logger.info(f"Job ID {job_id} completed successfully")
            return {"status": "successful", "type": "process", "value": result.result}
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