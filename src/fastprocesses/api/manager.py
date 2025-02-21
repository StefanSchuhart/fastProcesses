from datetime import datetime
from typing import Any, Dict, List

from celery.result import AsyncResult

from fastprocesses.common import redis_cache
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
            
            # Store job info for cache hit
            job_info = {
                "status": "successful",
                "type": "process",
                "process_id": process_id,
                "created": datetime.utcnow().isoformat(),
                "started": datetime.utcnow().isoformat(),
                "finished": datetime.utcnow().isoformat(),
                "updated": datetime.utcnow().isoformat(),
                "progress": 100,
                "message": "Result retrieved from cache"
            }
            self.cache.put(f"job:{task.id}", job_info)
            
            return ProcessExecResponse(status="successful", jobID=task.id, type="process")

        # No cached result, execute the process
        task = self.celery_app.send_task(
            'execute_process',
            args=[process_id, calculation_task.model_dump()]
        )

        # Store initial job info for new process execution
        job_info = {
            "status": "accepted",
            "type": "process",
            "process_id": process_id,
            "created": datetime.utcnow().isoformat(),
            "updated": datetime.utcnow().isoformat(),
            "progress": 0
        }
        self.cache.put(f"job:{task.id}", job_info)

        logger.info(f"Enqueued process ID: {process_id} with task ID: {task.id}")
        return ProcessExecResponse(status="accepted", jobID=task.id, type="process")

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
        # Check if job exists in Redis first
        job_info = self.cache.get(f"job:{job_id}")
        if not job_info:
            logger.error(f"Job {job_id} not found in cache")
            raise ValueError(f"Job {job_id} not found")

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

    def get_jobs(self) -> List[Dict[str, Any]]:
        """
        Retrieves a list of all jobs and their status.
        
        Returns:
            List[Dict[str, Any]]: List of job status information
        """
        # Get all job IDs from Redis
        job_keys = self.cache.keys("job:*")
        jobs = []

        for job_key in job_keys:
            try:
                job_info = self.cache.get(job_key)
                if job_info:
                    # Remove "job:" prefix for consistent job ID handling
                    job_id = job_key.replace("job:", "")
                    status_info = {
                        "jobID": job_id,  # Clean job ID without prefix
                        "status": job_info.get("status", "unknown"),
                        "type": "process",
                        "processID": job_info.get("process_id"),
                        "created": job_info.get("created"),
                        "started": job_info.get("started"),
                        "finished": job_info.get("finished"),
                        "updated": job_info.get("updated"),
                        "progress": job_info.get("progress"),
                        "links": [
                            {
                                "href": f"/jobs/{job_id}",  # Clean job ID in links
                                "rel": "self",
                                "type": "application/json"
                            },
                            {
                                "href": f"/jobs/{job_id}/results",  # Clean job ID in links
                                "rel": "results",
                                "type": "application/json"
                            }
                        ]
                    }
                    if "message" in job_info:
                        status_info["message"] = job_info["message"]
                    jobs.append(status_info)
            except Exception as e:
                logger.error(f"Error retrieving job {job_key}: {e}")

        return jobs