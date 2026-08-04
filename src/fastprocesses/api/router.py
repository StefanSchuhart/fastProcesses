import typing
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from fastprocesses.api.manager import ProcessManager
from fastprocesses.common import job_status_cache
from fastprocesses.core.exceptions import (
    BrokerUnavailableError,
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
    ProcessesSummary,
    ProcessExecRequestBody,
    ProcessExecResponse,
    ProcessList,
)
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.core.outputs_handler import serialize_result


def _get_result_class(
    process,
) -> type[BaseProcessResult] | None:
    """Return the concrete BaseProcessResult subclass for this process's output.

    Checks, in order:
    1. execute() return annotation — used by BaseProcess subclasses.
    2. merge_results() return annotation — used by BaseParallelProcess /
       BaseScatterProcess subclasses where the final result comes from the
       finalize/merge step, not from execute() directly.

    Returns None when no concrete subclass is found (legacy processes that
    return a plain BaseModel or have no annotation).
    """

    def _concrete(return_type: object) -> type[BaseProcessResult] | None:
        """Return return_type iff it is a concrete BaseProcessResult subclass."""
        if (
            isinstance(return_type, type)
            and issubclass(return_type, BaseProcessResult)
            and return_type is not BaseProcessResult
        ):
            return return_type
        return None

    cls = type(process)
    for method_name in ("execute", "merge_results"):
        try:
            hints = typing.get_type_hints(getattr(cls, method_name))
            result = _concrete(hints.get("return"))
            if result is not None:
                return result
        except Exception:
            pass
    return None


def get_router(
    process_manager: ProcessManager, title: str, description: str
) -> APIRouter:
    router = APIRouter()

    @router.get("/health", tags=["Health"])
    async def health_check():
        """
        Basic health check endpoint for FastAPI.
        """
        return {"status": "ok"}

    @router.get("/health/ready", tags=["Health"])
    async def readiness_check():
        """Readiness probe: verifies Redis connectivity for job status cache.

        This is intentionally a bit stricter than the basic liveness check and
        is meant to be used as a Kubernetes readinessProbe target. If Redis is
        unavailable, the endpoint returns HTTP 503 so the pod is marked unready
        without forcing a restart.
        """
        try:
            # Use the bounded Redis connection logic from TempResultCache
            job_status_cache.redis_connection._execute_redis_command("ping")
            return {"status": "ready"}
        except Exception as exc:  # pragma: no cover - defensive logging path
            logger.error("Readiness check failed: {!r}", exc)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "unready",
                    "reason": "redis_unreachable",
                },
            )

    @router.get("/health/worker", tags=["Health"])
    async def worker_health():
        """Worker health probe: inspects Celery to see if workers are online.

        This endpoint is useful with KEDA-style autoscaling where workers may
        be scaled to zero. It reports how many workers are currently visible
        to Celery and returns 503 when none are online or when the broker is
        unreachable.
        """

        try:
            worker_status = process_manager.get_worker_status()
        except BrokerUnavailableError as exc:  # pragma: no cover - defensive path
            logger.error("Worker health check failed due to broker error: {}", exc)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "unready",
                    "reason": "broker_unreachable",
                    "workers_online": 0,
                },
            )

        http_status = (
            status.HTTP_200_OK
            if worker_status.get("workers_online", 0) > 0
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )

        return JSONResponse(status_code=http_status, content=worker_status)

    @router.get("/conformance")
    async def conformance() -> Conformance:
        logger.debug("Conformance endpoint accessed")
        return Conformance(
            conformsTo=[
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/core",
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/json",
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/job-list",
                "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/ogc-process-description"
            ]
        )

    @router.get(
        "/processes", response_model_exclude_none=True, response_model=ProcessesSummary
    )
    async def list_processes(
        limit: int = Query(10, ge=1, le=10000), offset: int = Query(0, ge=0)
    ) -> ProcessesSummary:
        logger.debug("List processes endpoint accessed")

        processes, next_link = process_manager.get_available_processes(limit, offset)
        links = [Link(href="/processes", rel="self", type="application/json")]
        if next_link:
            links.append(Link(href=next_link, rel="next", type="application/json"))

        return ProcessesSummary(
            processes=ProcessList.validate_python(
                [desc.model_dump() for desc in processes]
            ),
            links=links,
        )

    @router.get(
        "/processes/{process_id}",
        response_model_exclude_none=True,
        response_model_exclude_unset=True,
        response_model=ProcessDescription,
    )
    async def describe_process(
        process_id: str,
    ) -> ProcessDescription | OGCExceptionResponse:
        logger.debug(f"Describe process endpoint accessed for process ID: {process_id}")
        
        try:
            return process_manager.get_process_description(process_id)
        except ValueError as e:
            logger.error(f"Process {process_id} not found: {e}")
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Process Not Found",
                status=404,
                detail=f"Process '{process_id}' not found.",
                instance=f"/processes/{process_id}",
            )
            raise HTTPException(status_code=404, detail=exception)
        except ProcessNotFoundError as e:
            logger.exception(e)
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Process Not Found",
                status=404,
                detail=f"Process '{process_id}' not found.",
                instance=f"/processes/{process_id}",
            )
            raise HTTPException(status_code=404, detail=exception)

    @router.post("/processes/{process_id}/execution")
    async def execute_process(
        process_id: str,
        http_request: Request,
        response: Response,
        prefer: str = Header(None, alias="Prefer"),
    ) -> ProcessExecResponse | OGCExceptionResponse | Any:
        # Log before body read so this always appears in logs, even for large payloads.
        logger.debug(f"Execute process endpoint accessed for process ID: {process_id}")

        execution_mode = ExecutionMode.ASYNC
        if prefer and "respond-sync" in prefer:
            execution_mode = ExecutionMode.SYNC

        logger.debug(f"Execution mode set to: {execution_mode}")

        # Read raw bytes and pass them to the manager unchanged.  For async
        # execution the manager sends these bytes directly to Celery, avoiding
        # redundant Python-level re-serialisation of potentially large payloads.
        try:
            body = await http_request.body()
        except Exception as exc:
            # Starlette raises ClientDisconnect (a subclass of Exception) when the
            # client closes the TCP connection while the body is still being uploaded.
            logger.error(
                "Client disconnected while uploading request body for process {}: {!r}",
                process_id,
                exc,
            )
            raise HTTPException(status_code=499, detail="Client disconnected during upload")

        try:
            result: ProcessExecResponse | Any = process_manager.execute_process(
                process_id, body, execution_mode
            )

            # If result is not a ProcessExecResponse, treat as ready result (sync)
            if not isinstance(result, ProcessExecResponse):
                response.status_code = status.HTTP_200_OK
                exec_body = ProcessExecRequestBody.model_validate_json(body)
                process = process_manager.process_registry.get_process(process_id)
                result_class = _get_result_class(process)
                if result_class is not None and isinstance(result, dict):
                    return serialize_result(
                        result_class.model_validate(result),
                        (exec_body.outputs or {}),
                        exec_body.response or "raw",
                        process.process_description,
                        process_manager.output_reference_publisher,
                    )
                return JSONResponse(content=result)

            # Async or Timeout: return job info
            response.status_code = status.HTTP_201_CREATED
            base_url = str(http_request.base_url).rstrip("/")
            response.headers["Location"] = f"{base_url}/jobs/{result.jobID}"
            return result

        except JobFailedError as e:
            logger.error(f"Job failed for process {process_id}: {e}")
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/job-failed",
                title="Sync execution failed",
                status=500,
                detail=f"Job failed: {e.args[0]}. See logs for more details.",
                instance=f"/processes/{process_id}/execution",
            )
            raise HTTPException(status_code=500, detail=exception)

        except ProcessNotFoundError as e:
            logger.error(f"Process {process_id} not found: {e}")
            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Process Not Found",
                status=404,
                detail=f"Process {process_id} not found.",
                instance=f"/processes/{process_id}",
            )
            raise HTTPException(status_code=404, detail=exception)

        except InputValidationError as e:
            error_message = str(e)
            logger.error(
                f"Input validation error for process {process_id}: {error_message}"
            )

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/invalid-parameter",
                title="Validation error",
                status=400,
                detail=(
                    f"Process {process_id}: Input validation failed. {error_message}"
                ),
                instance=f"/processes/{process_id}",
            )
            raise HTTPException(status_code=400, detail=exception)

        except OutputValidationError as e:
            error_message = str(e)

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/invalid-parameter",
                title="Validation error",
                status=400,
                detail=(
                    f"Process {process_id}: Output validation failed. {error_message}"
                ),
                instance=f"/processes/{process_id}",
            )

            logger.error(
                f"Output validation error for process {process_id}: {error_message}"
            )

            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=exception
            )

        except BrokerUnavailableError as e:
            logger.error(
                f"Broker unavailable when executing process {process_id}: {e}"
            )
            exception = OGCExceptionResponse(
                type=(
                    "http://www.opengis.net/def/exceptions/"
                    "ogcapi-processes-1/1.0/server-error"
                ),
                title="Service Unavailable",
                status=503,
                detail=(
                    "The task broker is currently unreachable. "
                    "Please try again later."
                ),
                instance=f"/processes/{process_id}/execution",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exception
            )

    @router.get("/jobs", response_model_exclude_none=True, response_model=JobList)
    async def list_jobs(
        limit: int = Query(10, ge=1, le=1000), offset: int = Query(0, ge=0)
    ) -> JobList:
        """
        Lists all jobs.
        """
        logger.debug("List jobs endpoint accessed")
        jobs, next_link = process_manager.get_jobs(limit, offset)
        links = [Link(href="/jobs", rel="self", type="application/json")]
        if next_link:
            links.append(Link(href=next_link, rel="next", type="application/json"))

        return JobList(jobs=jobs, links=links)

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
                instance=f"/jobs/{job_id}",
            )
            raise HTTPException(status_code=404, detail=exception)

    @router.get("/jobs/{job_id}/results", response_model_exclude_none=True)
    async def get_job_result(job_id: str) -> Any:
        logger.debug(f"Get job result endpoint accessed for job ID: {job_id}")
        try:
            result = process_manager.get_job_result(job_id)
            job_status = process_manager.get_job_status(job_id)
            process_id = job_status.processID
            if process_id is not None:
                process = process_manager.process_registry.get_process(process_id)
                result_class = _get_result_class(process)
                if result_class is not None and isinstance(result, dict):
                    # Retrieve the original requested outputs and response mode
                    # that were stored when the job was submitted.  Falls back
                    # to {} (all outputs, document mode) for legacy job records.
                    job_request = process_manager.job_request_cache.get(job_id)
                    requested_outputs = (job_request or {}).get("outputs") or {}
                    response_mode = (job_request or {}).get("response") or "document"
                    return serialize_result(
                        result_class.model_validate(result),
                        requested_outputs,
                        response_mode,
                        process.process_description,
                        process_manager.output_reference_publisher,
                    )
            return JSONResponse(content=result)

        # ValueError: Here, 'job id does not exist' is meant.
        except JobNotFoundError as e:
            logger.error(f"Job {job_id} not found: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-job",
                title="Job Not Found",
                status=404,
                detail=f"Job {job_id} not found.",
                instance=f"/jobs/{job_id}/results",
            )
            raise HTTPException(status_code=404, detail=exception)

        except JobNotReadyError as e:
            logger.info(f"Job {job_id} not ready: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/result-not-ready",
                title="Result Not Ready",
                status=404,
                detail=f"Result for job {job_id} is not ready.",
                instance=f"/jobs/{job_id}/results",
            )

            raise HTTPException(status_code=404, detail=exception)

        except JobFailedError as e:
            logger.error(f"Job {job_id} failed: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/job-failed",
                title="Job Failed",
                status=500,
                detail=f"{e.args[0]}. See logs for more details.",
                instance=f"/jobs/{job_id}/results",
            )

            raise HTTPException(status_code=500, detail=exception)

        except Exception as e:
            logger.error(f"Unexpected error for job {job_id}: {e}")

            exception = OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/internal-server-error",
                title="Internal Server Error",
                status=500,
                detail="An unexpected error occurred: See the log for details.",
                instance=f"/jobs/{job_id}/results",
            )

            raise HTTPException(status_code=500, detail=exception)

    return router
