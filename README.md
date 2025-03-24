# fastprocesses

A library to create a FastAPI-based OGC API Processes wrapper around existing projects. This library simplifies the process of defining and registering processes, making it easy to build and deploy OGC API Processes.

AI was used to create this code.

## Version: 0.7.4

### Description

fastprocesses is a Python library that provides a simple and efficient way to create OGC API Processes using FastAPI. It allows you to define processes, register them, and expose them through a FastAPI application with minimal effort, following the OGC API Processes 1.0.0 specification.

### Features

- **OGC API Processes Compliance**: Fully implements the OGC API Processes 1.0.0 Core specification
- **FastAPI Integration**: Leverages FastAPI for building high-performance APIs
- **Process Management**: Supports both synchronous and asynchronous process execution
- **Job Control**: Implements job control options (sync-execute, async-execute)
- **Output Handling**: Supports various output transmission modes (value, reference)
- **Result Caching**: Built-in Redis-based caching for process results
- **Celery Integration**: Asynchronous task processing using Celery
- **Pydantic Models**: Strong type validation for process inputs and outputs
- **Logging**: Uses `loguru` for modern logging with rotation support

### Architecture

```mermaid
graph TB
    subgraph Client
        CLI[Client Request]
    end

    subgraph FastAPI Application
        API[OGCProcessesAPI]
        Router[API Router]
        PM[ProcessManager]
        PR[ProcessRegistry]
    end

    subgraph Redis
        RC[Redis Cache]
        RR[Redis Registry]
    end

    subgraph Process
        BP[BaseProcess]
        SP[SimpleProcess]
    end

    subgraph Worker
        CW[Celery Worker]
        CT[CacheResultTask]
    end

    %% Client interactions
    CLI -->|HTTP Request| API
    API -->|Route Request| Router
    Router -->|Execute Process| PM

    %% Process Manager flow
    PM -->|Get Process| PR
    PM -->|Check Cache| RC
    PM -->|Submit Task| CW
    PM -->|Get Result| RC

    %% Process Registry
    PR -->|Store/Retrieve| RR
    PR -.->|Registers| SP
    SP -->|Inherits| BP

    %% Worker flow
    CW -->|Execute| SP
    CW -->|Cache Result| CT
    CT -->|Store| RC

    %% Styling
    classDef api fill:#f9f,stroke:#333,stroke-width:2px
    classDef cache fill:#bbf,stroke:#333,stroke-width:2px
    classDef process fill:#bfb,stroke:#333,stroke-width:2px
    classDef worker fill:#fbb,stroke:#333,stroke-width:2px

    class API,Router api
    class RC,RR cache
    class BP,SP process
    class CW,CT worker
```

### Usage

1. **Define a Process**: Create a new process by subclassing `BaseProcess` and using the `@register_process` decorator.

```python
from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.models import (
    ProcessDescription,
    ProcessInput,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.processes.process_registry import register_process

@register_process("simple_process")
class SimpleProcess(BaseProcess):
    # Define process description as a class variable
    process_description = ProcessDescription(
        id="simple_process",
        title="Simple Process",
        version="1.0.0",
        description="A simple example process",
        jobControlOptions=[
            ProcessJobControlOptions.SYNC_EXECUTE,
            ProcessJobControlOptions.ASYNC_EXECUTE
        ],
        outputTransmission=[
            ProcessOutputTransmission.VALUE
        ],
        inputs={
            "input_text": ProcessInput(
                title="Input Text",
                description="Text to process",
                schema=Schema(
                    type="string",
                    minLength=1,
                    maxLength=1000
                )
            )
        },
        outputs={
            "output_text": ProcessOutput(
                title="Output Text",
                description="Processed text",
                schema=Schema(
                    type="string"
                )
            )
        },
        keywords=["text", "processing"],
        metadata={
            "created": "2024-02-19",
            "provider": "Example Organization"
        }
    )

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        input_text = inputs["inputs"]["input_text"]
        output_text = input_text.upper()
        return {"output_text": output_text}
```

2. **Create the FastAPI Application**:

```python
import uvicorn
from fastprocesses.api.server import OGCProcessesAPI

app = OGCProcessesAPI(
    title="Simple Process API",
    version="1.0.0",
    description="A simple API for running processes"
).get_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

3. **Start the Services**:

Start Redis (required for caching and Celery):
```bash
docker run -d -p 6379:6379 redis
```

Start the Celery worker:
```bash
celery -A fastprocesses.worker.celery_app worker
```

Start the FastAPI application:
```bash
poetry run python examples/run_example.py
```

4. **Use the API**:

Execute a process (async):
```bash
curl -X POST "http://localhost:8000/processes/simple_process/execution" \
     -H "Content-Type: application/json" \
     -d '{
       "inputs": {
         "input_text": "hello world"
       },
       "mode": "async"
     }'
```

Execute a process (sync):
```bash
curl -X POST "http://localhost:8000/processes/simple_process/execution" \
     -H "Content-Type: application/json" \
     -d '{
       "inputs": {
         "input_text": "hello world"
       },
       "mode": "sync"
     }'
```

### API Endpoints

- `GET /`: Landing page
- `GET /conformance`: OGC API conformance declaration
- `GET /processes`: List available processes
- `GET /processes/{process_id}`: Get process description
- `POST /processes/{process_id}/execution`: Execute a process
- `GET /jobs`: List all jobs
- `GET /jobs/{job_id}`: Get job status
- `GET /jobs/{job_id}/results`: Get job results

### Configuration

The library can be configured using environment variables:

```bash
REDIS_CACHE_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2
```

### Notes:
How to serialize pydantic models within celery? -> https://benninger.ca/posts/celery-serializer-pydantic/

!IMPORTANT!: Cache hash key is based on original unprocessed inputs always this ensures consistent caching and cache retrieval which does not depend on arbitrary processed data, which can change when the process is updated or changed!

### Version Notes
- **Version: 0.7.4**: added paging to processes and jobs, including limit and offset query params
- **0.5.0**: Extended Schema model
- **0.4.0**: Added full OGC API Processes 1.0.0 Core compliance
- **0.3.0**: Added job control and output transmission options
- **0.2.0**: Added Redis caching and Celery integration
- **0.1.0**: Initial release with basic process support