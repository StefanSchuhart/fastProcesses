# src/fastprocesses/api/server.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastprocesses.api.manager import ProcessManager
from fastprocesses.api.router import get_router


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

    def get_app(self) -> FastAPI:
        return self.app
