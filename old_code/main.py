from fastapi import FastAPI, HTTPException
from nlp_service.api.process_manager import ProcessManager
from nlp_service.api.models import ProcessRequest, ProcessList, Landing, Conformance, Link
from nlp_service.config import settings

class APIServer:
  def __init__(self):
    self.app = FastAPI(
      title=settings.API_TITLE,
      version=settings.API_VERSION,
      description=settings.API_DESCRIPTION
    )
    self.process_manager = ProcessManager()
    self._setup_routes()

  def _setup_routes(self):
    """Sets up the routes for the FastAPI application."""
    @self.app.get("/")
    async def landing_page() -> Landing:
      """Handles the landing page route.

      Returns:
          Landing: An instance of the Landing class containing the title, description, and links.
      """
      return Landing(
        title=settings.API_TITLE,
        description=settings.API_DESCRIPTION,
        links=[
          Link(href="/", rel="self", type="application/json"),
          Link(href="/conformance", rel="conformance", type="application/json"),
          Link(href="/processes", rel="processes", type="application/json")
        ]
      )

    @self.app.get("/conformance")
    async def conformance() -> Conformance:
      """Handles the conformance route.

      Returns:
          Conformance: An instance of the Conformance class containing the conformance information.
      """
      return Conformance(
        conformsTo=[
          "http://www.opengis.net/spec/ogcapi-processes/1.0/conf/core",
          "http://www.opengis.net/spec/ogcapi-processes/1.0/conf/json"
        ]
      )

    @self.app.get("/processes",
        summary="Retrieve the list of available processes.",
        description="The list of processes contains a summary of each process the OGC API - "
        "Processes offers, including the link to a more detailed description of the process.\n\n"
        "For more information, see [Section 7.9](https://docs.ogc.org/is/18-062r2/18-062r2.html#sc_process_list).",
        responses={
          200: {
            "model": ProcessList,
            "description": "All available processes were retrieved."
          }
        }
    )
    async def list_processes():
      return {
        "processes": self.process_manager.get_available_processes(),
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

    @self.app.get("/processes/{process_id}")
    async def describe_process(process_id: str):
      try:
        return self.process_manager.get_process_description(process_id)
      except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    @self.app.post("/processes/{process_id}/execution")
    async def execute_process(process_id: str, request: ProcessRequest):
      try:
        return self.process_manager.execute_process(process_id, request.data)
      except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    @self.app.get("/jobs/{job_id}")
    async def get_job_status(job_id: str):
      try:
        return self.process_manager.get_job_status(job_id)
      except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    @self.app.get("/jobs/{job_id}/results")
    async def get_job_result(job_id: str):
      try:
        return self.process_manager.get_job_result(job_id)
      except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    @self.app.delete("/jobs/{job_id}")
    async def delete_job(job_id: str):
      try:
        return self.process_manager.delete_job(job_id)
      except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))