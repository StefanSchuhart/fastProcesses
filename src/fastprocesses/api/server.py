# src/fastprocesses/api/server.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from fastprocesses.api.manager import ProcessManager
from fastprocesses.api.router import get_router
from fastprocesses.core.models import OGCExceptionResponse


class OGCProcessesAPI:
    def __init__(self, title: str, version: str, description: str):
        self.process_manager = ProcessManager()
        self.app = FastAPI(title=title, version=version, description=description)
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
