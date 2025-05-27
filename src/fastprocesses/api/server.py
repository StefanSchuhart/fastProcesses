# src/fastprocesses/api/server.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from fastprocesses.api.manager import ProcessManager
from fastprocesses.api.router import get_router
from fastprocesses.core.models import OGCExceptionResponse
from fastprocesses.common import settings


class OGCProcessesAPI:
    def __init__(
            self,
            contact: dict | None = None, license: dict | None = None,
            terms_of_service: str | None = None
            ):

        self.process_manager = ProcessManager()
        self.app = FastAPI(
            title=settings.FP_API_TITLE,
            version=settings.FP_API_VERSION,
            description=settings.FP_API_DESCRIPTION,
            contact=contact,
            license_info=license,
            terms_of_service=terms_of_service
        )
        self.app.include_router(
            get_router(
                self.process_manager,
                self.app.title,
                self.app.description
            )
        )

        # Add CORS middleware
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],  # Replace "*" with specific origins if needed
            allow_credentials=True,
            allow_methods=["*"],  # Allow all HTTP methods
            allow_headers=["*"],  # Allow all headers
        )

                # Register RFC 7807/OGC API Processes-compliant error handler
        @self.app.exception_handler(HTTPException)
        async def ogc_http_exception_handler(request: Request, exc: HTTPException):
            # If the detail is already a dict with RFC 7807 fields, use it directly
            if isinstance(exc.detail, OGCExceptionResponse):
                content = exc.detail.model_dump()
            else:
                # Fallback: wrap the detail in a minimal RFC 7807 structure
                content = {
                    "type": "about:blank",
                    "title": "HTTPException",
                    "status": exc.status_code,
                    "detail": str(exc.detail),
                    "instance": str(request.url)
                }
            return JSONResponse(status_code=exc.status_code, content=content)

    def get_app(self) -> FastAPI:
        return self.app
