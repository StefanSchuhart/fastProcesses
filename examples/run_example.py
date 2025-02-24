import uvicorn
from fastprocesses.api.server import OGCProcessesAPI
from .simple_process import SimpleProcess

# Create the FastAPI app
app = OGCProcessesAPI(
    title="Simple Process API",
    version="1.0.0",
    description="A simple API for running processes"
).get_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        log_level=None,
    )