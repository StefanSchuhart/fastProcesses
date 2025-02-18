from typing import Dict, Any, List
from celery.result import AsyncResult
from fastprocesses.core.models import ProcessResponse, ProcessDescription
from fastprocesses.services.service_registry import get_service_registry
from fastprocesses.worker.celery_app import celery_app

class ProcessManager:
    """Manages processes, including execution, status checking, and job management."""

    def __init__(self):
        """Initializes the ProcessManager with Celery app and service registry."""
        self.celery_app = celery_app
        self.service_registry = get_service_registry()

    def get_available_processes(self) -> List[ProcessDescription]:
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
            raise ValueError(f"Process {process_id} not found!")

        service = self.service_registry.get_service(process_id)
        return service.get_description()

    def execute_process(self, process_id: str, data: Dict[str, Any]) -> ProcessResponse:
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
            raise ValueError(f"Process {process_id} not found!")

        task = self.celery_app.send_task('execute_process', args=[process_id, data])
        return ProcessResponse(status="accepted", jobID=task.id, type="process")

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
        result = AsyncResult(job_id)
        if result.ready():
            if result.successful():
                return {"status": "successful", "type": "process"}
            return {"status": "failed", "type": "process", "message": str(result.result)}
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
        result = AsyncResult(job_id)
        if result.ready():
            if result.successful():
                return {"status": "successful", "type": "process", "value": result.result}
            return {"status": "failed", "type": "process", "message": str(result.result)}
        return {"status": "running", "type": "process"}

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
        result = AsyncResult(job_id)
        if not result:
            raise ValueError("Job not found")
        result.forget()
        return {"status": "dismissed", "message": "Job dismissed"}