from fastapi import FastAPI
from fastprocesses.api.server import OGCProcessesAPI
from fastprocesses.services.register_services import register_services

register_services()

app = OGCProcessesAPI(
    title="FastProcesses API",
    version="1.0.0",
    description="A FastAPI-based OGC API Processes wrapper"
).get_app()
