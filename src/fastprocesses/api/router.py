from fastapi import APIRouter, HTTPException

from fastprocesses.api.manager import ProcessManager
from fastprocesses.core.models import (
    Conformance,
    Landing,
    Link,
    ProcessInputs,
    ProcessList,
)


def get_router(process_manager: ProcessManager) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def landing_page() -> Landing:
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
        return Conformance(
            conformsTo=[
                "http://www.opengis.net/spec/ogcapi-processes/1.0/conf/core",
                "http://www.opengis.net/spec/ogcapi-processes/1.0/conf/json"
            ]
        )

    @router.get("/processes")
    async def list_processes():
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
        try:
            return process_manager.get_process_description(process_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/processes/{process_id}/execution")
    async def execute_process(process_id: str, request: ProcessInputs):
        try:
            return process_manager.execute_process(process_id, request.inputs)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.get("/jobs/{job_id}")
    async def get_job_status(job_id: str):
        try:
            return process_manager.get_job_status(job_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.get("/jobs/{job_id}/results")
    async def get_job_result(job_id: str):
        try:
            return process_manager.get_job_result(job_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    return router