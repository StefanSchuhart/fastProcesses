from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import JSONResponse

from fastprocesses.api.manager import ProcessManager
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    Conformance,
    ExecutionMode,
    Landing,
    Link,
    ProcessExecRequestBody,
)


def get_router(process_manager: ProcessManager) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def landing_page() -> Landing:
        logger.debug("Landing page accessed")
        return Landing(
            title="API Title",
            description="API Description",
            links=[
                Link(href="/", rel="self", type="application/json"),
                Link(href="/conformance", rel="conformance", type="application/json"),
                Link(href="/processes", rel="processes", type="application/json")
            ]
        )

    @router.get("/conformance")
    async def conformance() -> Conformance:
        logger.debug("Conformance endpoint accessed")
        return Conformance(
            conformsTo=[
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/core",
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/json"
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/job-list"
            ]
        )

    @router.get("/processes")
    async def list_processes():
        logger.debug("List processes endpoint accessed")
        return {
            "processes": process_manager.get_available_processes(),
            "links": [
                {
                    "href": "/processes",
                    "rel": "self",
                    "type": "application/json",
                    "hreflang": None,
                    "title": None
                }
            ]
        }

    @router.get("/processes/{process_id}")
    async def describe_process(process_id: str):
        logger.debug(f"Describe process endpoint accessed for process ID: {process_id}")
        try:
            return process_manager.get_process_description(process_id)
        except ValueError as e:
            logger.error(f"Process {process_id} not found: {e}")
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/processes/{process_id}/execution")
    async def execute_process(
        process_id: str,
        request: ProcessExecRequestBody,
        response: Response
    ) -> JSONResponse:
        logger.debug(f"Execute process endpoint accessed for process ID: {process_id}")
        try:
            result = process_manager.execute_process(process_id, request)
            
            # Set response status code based on execution mode
            if request.mode == ExecutionMode.ASYNC:
                response.status_code = status.HTTP_201_CREATED
                # Add Location header for async execution
                response.headers["Location"] = f"/jobs/{result.jobID}"
            else:
                # For sync execution with results
                if result.value:
                    response.status_code = status.HTTP_200_OK
                # For sync execution without results
                else:
                    response.status_code = status.HTTP_204_NO_CONTENT
            
            return result
        except ValueError as e:
            error_message = str(e)
            if "Input validation failed" in error_message:
                logger.error(f"Input validation error for process {process_id}: {error_message}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "type": "process",
                        "error": "InvalidParameterValue",
                        "message": error_message,
                        "process_id": process_id
                    }
                )
            logger.error(f"Process {process_id} not found: {error_message}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "type": "process",
                    "error": "NotFound",
                    "message": error_message
                }
            )

    @router.get("/jobs")
    async def list_jobs():
        """
        Lists all jobs.
        """
        logger.debug("List jobs endpoint accessed")
        jobs = process_manager.get_jobs()
        return {
            "jobs": jobs,
            "links": [
                {
                    "href": "/jobs",
                    "rel": "self",
                    "type": "application/json"
                }
            ]
        }

    @router.get("/jobs/{job_id}")
    async def get_job_status(job_id: str):
        logger.debug(f"Get job status endpoint accessed for job ID: {job_id}")
        try:
            return process_manager.get_job_status(job_id)
        except ValueError as e:
            logger.error(f"Job {job_id} not found: {e}")
            raise HTTPException(status_code=404, detail=str(e))

    @router.get("/jobs/{job_id}/results")
    async def get_job_result(job_id: str):
        logger.debug(f"Get job result endpoint accessed for job ID: {job_id}")
        try:
            return process_manager.get_job_result(job_id)
        except ValueError as e:
            logger.error(f"Job {job_id} not found: {e}")
            raise HTTPException(status_code=404, detail=str(e))

    return router