import hashlib
import json
from typing import Any, Dict, List

from celery.result import AsyncResult

from fastprocesses.core.cache import Cache
from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger
from fastprocesses.core.models import ProcessDescription, ProcessResponse
from fastprocesses.services.service_registry import get_process_registry
from fastprocesses.worker.celery_app import celery_app


class ProcessManager:
    """Manages processes, including execution, status checking, and job management."""

    def __init__(self):
        """Initializes the ProcessManager with Celery app and service registry."""
        self.celery_app = celery_app
        self.service_registry = get_process_registry()
        self.cache = Cache(key_prefix="process_results", ttl_days=7)

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

    def execute_process(self, process_id: str, data: Dict[str, Any]) -> ProcessResponse:
        logger.info(f"Executing process ID: {process_id}")
        """
        Executes a process by enqueuing it in the Celery queue.

        Args:
            process_id (str): The ID of the process.
            data (Dict[str, Any]): The data to be processed.

        Returns:
            ProcessResponse: The response containing the job status and ID.

        Raises:
            ValueError: If the process is not found.
        """
        if not self.service_registry.has_service(process_id):
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        # Generate a hash of the inputs
        input_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

        # Check if results are already cached in Redis
        cached_result = self.cache.get(key=input_hash)
        if cached_result:
            logger.info(f"Returning cached result for process ID: {process_id}")
            # Create a Celery task to fetch the cached result
            task = self.celery_app.send_task('find_result_in_cache', args=[input_hash])
            return ProcessResponse(status="SUCCESS", jobID=task.id, type="process")

        # Enqueue the task in Celery
        task = self.celery_app.send_task('execute_process', args=[process_id, data])

        # Store the task ID in Redis with the input hash as the key
        self.cache.put(key=input_hash, value={"task_id": task.id})

        logger.info(f"Enqueued process ID: {process_id} with task ID: {task.id}")
        return ProcessResponse(status="ACCEPTED", jobID=task.id, type="process")

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