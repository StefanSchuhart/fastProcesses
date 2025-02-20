from fastapi import APIRouter, HTTPException

from fastprocesses.api.manager import ProcessManager
from fastprocesses.core.logging import logger
from fastprocesses.core.models import (
    Conformance,
    Landing,
    Link,
    ProcessExecRequestBody,
    ProcessList,
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
                "http://www.opengis.net/spec/ogcapi-processes/1.0/conf/core",
                "http://www.opengis.net/spec/ogcapi-processes/1.0/conf/json"
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
    async def execute_process(process_id: str, request: ProcessExecRequestBody):
        logger.debug(f"Execute process endpoint accessed for process ID: {process_id}")
        try:
            return process_manager.execute_process(process_id, request)
        except ValueError as e:
            logger.error(f"Process {process_id} not found: {e}")
            raise HTTPException(status_code=404, detail=str(e))

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