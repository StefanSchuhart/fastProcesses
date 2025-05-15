from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from fastprocesses.api.manager import ProcessManager
from fastprocesses.core.exceptions import (
    InputValidationError,
    JobFailedError,
    JobNotFoundError,
    JobNotReadyError,
    OutputValidationError,
    ProcessNotFoundError,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    Conformance,
    ExecutionMode,
    JobList,
    JobStatusInfo,
    Landing,
    Link,
    OGCExceptionResponse,
    ProcessDescription,
    ProcessExecRequestBody,
    ProcessExecResponse,
    ProcessList,
)


def get_router(
        process_manager: ProcessManager,
        title: str,
        description: str
    ) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def landing_page() -> Landing:
        logger.debug("Landing page accessed")
        return Landing(
            title=title,
            description=description,
            links=[
                Link(href="/", rel="self", type="application/json"),
                Link(href="/conformance", rel="conformance", type="application/json"),
                Link(href="/processes", rel="processes", type="application/json"),
                Link(href="/jobs", rel="jobs", type="application/json"),
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

    @router.get(
        "/processes",
        response_model_exclude_none=True,
        response_model=ProcessList
    )
    async def list_processes(
        limit: int = Query(10, ge=1, le=10000),
        offset: int = Query(0, ge=0)
    ):
        logger.debug("List processes endpoint accessed")

        processes, next_link = process_manager.get_available_processes(limit, offset)
        links = [Link(href="/processes", rel="self", type="application/json")]
        if next_link:
            links.append(Link(href=next_link, rel="next", type="application/json"))

        return ProcessList(
            processes=processes,
            links=links
        )

    @router.get(
            "/processes/{process_id}",
            response_model_exclude_none=True,
            response_model=ProcessDescription
    )
    async def describe_process(
        process_id: str
    ) -> ProcessDescription | OGCExceptionResponse:
        logger.debug(f"Describe process endpoint accessed for process ID: {process_id}")
        try:
            return process_manager.get_process_description(process_id)
        except ProcessNotFoundError as e:
            logger.error(f"Process {process_id} not found: {e}")
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Process Not Found",
                status=404,
                detail=f"Process '{process_id}' not found.",
                instance=f"/processes/{process_id}"
            )
            raise HTTPException(status_code=404, detail=exception)


    @router.post(
        "/processes/{process_id}/execution",
        response_model=ProcessExecResponse
    )
    async def execute_process(
        process_id: str,
        request: ProcessExecRequestBody,
        response: Response,
        prefer: str = Header(None, alias="Prefer")
    ) -> JSONResponse:
        logger.debug(f"Execute process endpoint accessed for process ID: {process_id}")
        
        execution_mode = ExecutionMode.ASYNC
        if prefer and "respond-sync" in prefer:
            execution_mode = ExecutionMode.SYNC
        
        logger.debug(f"Execution mode set to: {execution_mode}")

        try:
            result = process_manager.execute_process(
                process_id, request,
                execution_mode
            )
            
            # Set response status code based on execution mode
            if execution_mode == ExecutionMode.ASYNC:
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
                    # TODO: need to add link headers with location to output
            
            return result
        except ProcessNotFoundError as e:
            logger.error(f"Process {process_id} not found: {e}")
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Process Not Found",
                status=404,
                detail=f"Process {process_id} not found.",
                instance=f"/processes/{process_id}"
            )
            raise HTTPException(status_code=404, detail=exception)

        except InputValidationError as e:
            error_message = str(e)
            logger.error(f"Input validation error for process {process_id}: {error_message}")
            
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Validation error",
                status=400,
                detail=f"Process {process_id}: Input validation failed. {error_message}",
                instance=f"/processes/{process_id}"
            )
            raise HTTPException(status_code=400, detail=exception)

        except OutputValidationError as e:
            error_message = str(e)

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Validation error",
                status=400,
                detail=f"Process {process_id}: Output validation failed. {error_message}",
                instance=f"/processes/{process_id}"
            )

            logger.error(f"Output validation error for process {process_id}: {error_message}")
            
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=exception
            )

    @router.get(
        "/jobs",
        response_model_exclude_none=True,
        response_model=JobList
    )
    async def list_jobs(
        limit: int = Query(10, ge=1, le=1000),
        offset: int = Query(0, ge=0)
    ) -> JobList:
        """
        Lists all jobs.
        """
        logger.debug("List jobs endpoint accessed")
        jobs, next_link = process_manager.get_jobs(limit, offset)
        links = [Link(href="/jobs", rel="self", type="application/json")]
        if next_link:
            links.append(Link(href=next_link, rel="next", type="application/json"))

        return JobList(
            jobs=jobs,
            links=links
        )


    @router.get("/jobs/{job_id}")
    async def get_job_status(job_id: str) -> JobStatusInfo | OGCExceptionResponse:
        logger.debug(f"Get job status endpoint accessed for job ID: {job_id}")
        try:
            return process_manager.get_job_status(job_id)

        except JobNotFoundError as e:
            logger.error(f"Job {job_id} not found: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-job",
                title="Job Not Found",
                status=404,
                detail=f"Job {job_id} not found.",
                instance=f"/jobs/{job_id}"
            )
            raise HTTPException(status_code=404, detail=exception)

    @router.get("/jobs/{job_id}/results", response_model_exclude_none=True)
    async def get_job_result(job_id: str) -> dict | OGCExceptionResponse:
        logger.debug(f"Get job result endpoint accessed for job ID: {job_id}")
        try:
            return process_manager.get_job_result(job_id)
        
        # ValueError: Here, 'job id does not exist' is meant.
        except JobNotFoundError as e:
            logger.error(f"Job {job_id} not found: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-job",
                title="Job Not Found",
                status=404,
                detail=f"Job {job_id} not found.",
                instance=f"/jobs/{job_id}/results"
            )
            raise HTTPException(status_code=404, detail=exception)

        except JobNotReadyError as e:
            logger.info(f"Job {job_id} not ready: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/result-not-ready",
                title="Result Not Ready",
                status=404,
                detail=f"Result for job {job_id} is not ready.",
                instance=f"/jobs/{job_id}/results"
            )

            raise HTTPException(status_code=404, detail=exception)
        
        except JobFailedError as e:
            logger.error(f"Job {job_id} failed: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/job-failed",
                title="Job Failed",
                status=500,
                detail=f"{e.args[0]}. See logs for more details.",
                instance=f"/jobs/{job_id}/results"
            )

            raise HTTPException(status_code=500, detail=exception)

        except Exception as e:
            logger.error(f"Unexpected error for job {job_id}: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/internal-server-error",
                title="Internal Server Error",
                status=500,
                detail="An unexpected error occurred: See the log for details.",
                instance=f"/jobs/{job_id}/results"
            )

            raise HTTPException(status_code=500, detail=exception)

    return router