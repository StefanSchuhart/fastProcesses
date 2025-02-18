import uvicorn
from fastapi import FastAPI

from fastprocesses.api.manager import ProcessManager
from fastprocesses.api.router import get_router
from fastprocesses.core.config import OGCProcessesSettings
from fastprocesses.services.service_registry import get_process_registry
from fastprocesses.worker.celery_app import celery_app

from .simple_process import SimpleProcess

# Load settings
settings = OGCProcessesSettings()

# Register the simple process with the global service registry
service_registry = get_process_registry()
service_registry.register_service("simple_process", SimpleProcess())

# Create the ProcessManager
process_manager = ProcessManager()

# Create the FastAPI app
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description
)

# Include the router with the ProcessManager instance
app.include_router(get_router(process_manager))

# Define a Celery task for the simple process
@celery_app.task(name="simple_process_task")
def simple_process_task(input_text: str):
    process = SimpleProcess()
    inputs = {"input_text": input_text}
    return process.execute(inputs)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)