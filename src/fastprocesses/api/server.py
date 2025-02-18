# api/server.py
from fastapi import FastAPI
from fastprocesses.api.router import router

class OGCProcessesAPI:
    def __init__(self, title: str, version: str, description: str):
        self.app = FastAPI(
            title=title,
            version=version,
            description=description
        )
        self.app.include_router(router)
    
    def get_app(self) -> FastAPI:
        return self.app
