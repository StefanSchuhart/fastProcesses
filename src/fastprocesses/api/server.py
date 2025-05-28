# src/fastprocesses/api/server.py
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from fastprocesses.api.manager import ProcessManager
from fastprocesses.api.router import get_router
from fastprocesses.common import settings
from fastprocesses.core.models import OGCExceptionResponse


class OGCProcessesAPI:
    def __init__(
        self,
        contact: dict | None = None,
        license: dict | None = None,
        terms_of_service: str | None = None,
    ):
        self.process_manager = ProcessManager()
        self.app = FastAPI(
            title=settings.FP_API_TITLE,
            version=settings.FP_API_VERSION,
            description=settings.FP_API_DESCRIPTION,
            contact=contact,
            license_info=license,
            terms_of_service=terms_of_service,
        )
        self.app.include_router(
            get_router(self.process_manager, self.app.title, self.app.description)
        )
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Resolve static and template directories to real filesystem paths at startup
        static_dir = str(resources.files("fastprocesses").joinpath("static"))
        templates_dir = str(resources.files("fastprocesses").joinpath("templates"))
        self.app.mount("/static", StaticFiles(directory=static_dir), name="static")
        templates = Jinja2Templates(directory=templates_dir)

        @self.app.exception_handler(HTTPException)
        async def ogc_http_exception_handler(request: Request, exc: HTTPException):
            if isinstance(exc.detail, OGCExceptionResponse):
                content = exc.detail.model_dump()
            else:
                content = {
                    "type": "about:blank",
                    "title": "HTTPException",
                    "status": exc.status_code,
                    "detail": str(exc.detail),
                    "instance": str(request.url),
                }
            return JSONResponse(status_code=exc.status_code, content=content)

        @self.app.get("/", response_class=HTMLResponse)
        async def landing_page(request: Request):
            # Content negotiation: check Accept header and 'f' query param
            f = request.query_params.get("f")
            accept = request.headers.get("accept", "")
            
            if f == "json" or ("application/json" in accept and f != "html"):
                # Return JSON landing page (OGC API Processes conformance)
                return JSONResponse(self.api_description())
            
            # Prepare context for Jinja2 template
            api = self.api_description()
            
            # Generate table rows for API endpoints
            links = api.get("links", [])
            table_rows = "".join(
                f"<tr><td><a href='{link['href']}'>{link['href']}</a></td>"
                f"<td>{link['rel'].capitalize()}</td></tr>"
                for link in links
            )

            contact = self.app.contact or {}
            contact_line = " | ".join(
                filter(None, [
                    f"<a href='{contact.get('url')}'>{contact.get('name')}</a>" if contact.get("url") else None,
                    f"<a href='mailto:{contact.get('email')}'>{contact.get('email')}</a>" if contact.get("email") else None,
                ])
            )

            license_info = self.app.license_info or {}
            license_line = " | ".join(
                filter(None, [
                    f"<a href='{license_info.get('url')}'>{license_info.get('name')}</a>" if license_info.get("url") else None,
                ])
            )

            terms_of_service = self.app.terms_of_service
            terms_line = f"<a href='{terms_of_service}'>terms of service</a>" if terms_of_service else ""

            context = {
                "request": request,
                "title": self.app.title,
                "version": self.app.version,
                "description": self.app.description,
                "contact_line": Markup(f"{contact_line} |"),
                "license_line": Markup(f"{license_line} |"),
                "terms_of_service": Markup(terms_line),
                # Mark powered_by as safe HTML for Jinja2
                "powered_by": Markup(
                    "<a href='https://github.com/StefanSchuhart/fastProcesses'>fastprocesses</a>"
                ),
                "css": "/static/style.css",
                "table_rows": Markup(table_rows),
            }

            return templates.TemplateResponse("landing.html", context)

    def get_app(self) -> FastAPI:
        return self.app

    def api_description(self):
        """
        Returns the OGC API landing page JSON description (root resource),
        as required by OGC API Processes.
        """
        return {
            "title": self.app.title,
            "description": self.app.description,
            "version": self.app.version,
            "links": [
                {"rel": "self", "type": "application/json", "href": "/"},
                {
                    "rel": "conformance",
                    "type": "application/json",
                    "href": "/conformance",
                },
                {"rel": "processes", "type": "application/json", "href": "/processes"},
                {"rel": "jobs", "type": "application/json", "href": "/jobs"},
                {"rel": "interactive documentation", "type": "text/html", "href": "/docs"},
                {"rel": "alternative documentation", "type": "text/html", "href": "/redoc"},
                {"rel":"OpenAPI",
                 "type": "application/vnd.oai.openapi+json;version=3.0",
                 "href": "/openapi.json"},
            ],
            "contact_line": self.app.contact,
            "license_line": self.app.license_info,
        }
