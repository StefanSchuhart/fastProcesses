from typing import Dict, Any, List
from redis import Redis
from rq import Queue
from nlp_service.api.models import ProcessResponse, ProcessDescription
from nlp_service.services.service_registry import ServiceRegistry
from nlp_service.config import settings


class ProcessManager:
    """Manages processes, including execution, status checking, and job management."""

    def __init__(self):
        """Initializes the ProcessManager with Redis connection, queue, and service registry."""
        self.redis = Redis.from_url(settings.REDIS_URL)
        self.queue = Queue(settings.WORKER_QUEUE, connection=self.redis)
        self.service_registry = ServiceRegistry()

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
        Executes a process by enqueuing it in the queue.

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

        job = self.queue.enqueue(
            "nlp_service.worker.tasks.process_task", process_id, data
        )

        return ProcessResponse(status="accepted", jobID=job.id, type="process")

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
        job = self.queue.fetch_job(job_id)
        if not job:
            raise ValueError("Job not found")

        if job.is_finished:
            return {
                "status": "successful",
                "type": "process",
            }
        elif job.is_failed:
            return {"status": "failed", "type": "process", "message": str(job.exc_info)}
        else:
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
        job = self.queue.fetch_job(job_id)
        if not job:
            raise ValueError("Job not found")

        if job.is_finished:
            return {"status": "successful", "type": "process", "value": job.result}
        else:
            return {"status": "failed", "type": "process"}

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
        job = self.queue.fetch_job(job_id)
        if not job:
            raise ValueError("Job not found")
        job.cancel()
        job.delete(remove_from_queue=True, delete_dependents=True)
        return {"status": "dismissed", "message": "Job dismissed"}