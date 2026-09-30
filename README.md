# fastprocesses

A library to create a FastAPI-based OGC API Processes wrapper around existing projects. This library simplifies the process of defining and registering processes, making it easy to build and deploy OGC API Processes.

AI helped to create this code.

## Version: 0.24.0-rc5

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
- **Parallel Data Fan-out**: `BaseParallelProcess` — split a large input into chunks, process each chunk on a separate worker, then merge the results
- **Parallel Operation Fan-out**: `BaseScatterProcess` + `@parallel_step` — run multiple independent operations on the same input concurrently, then merge the named results
- **KEDA-compatible Parallelism**: Both parallel patterns use Celery chords — the orchestrating task returns immediately so workers do not block while waiting for sub-tasks
- **Pydantic Models**: Strong type validation for process inputs and outputs
- **Logging**: Uses `loguru` for modern logging with rotation support

`fastprocesses` uses Celery for async execution of arbitrary processes and result retrieval from a backend like Redis. For deterministic processes, that means processes that return the same results for identical inputs, redis is used as temporary cache. For both, the celery backend and the temporary Redis cache, time to live can be configured.

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
        BPP[BaseParallelProcess]
        BSP[BaseScatterProcess]
    end

    subgraph Worker
        CW[Celery Worker]
        CT[CacheResultTask]
        EPI[execute_parallel_item]
        ESS[execute_scatter_step]
        FP[finalize_parallel]
        FS[finalize_scatter]
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
    PR -.->|Registers| BPP
    PR -.->|Registers| BSP
    SP -->|Inherits| BP
    BPP -->|Inherits| BP
    BSP -->|Inherits| BP

    %% Worker flow — simple
    CW -->|Execute simple| SP
    CW -->|Cache Result| CT
    CT -->|Store| RC

    %% Worker flow — parallel (chord)
    CW -->|Dispatch chord| EPI
    EPI -->|Partial results| FP
    FP -->|Store merged| RC

    %% Worker flow — scatter (chord)
    CW -->|Dispatch chord| ESS
    ESS -->|Step results| FS
    FS -->|Store merged| RC

    %% Styling
    classDef api fill:#f9f,stroke:#333,stroke-width:2px
    classDef cache fill:#bbf,stroke:#333,stroke-width:2px
    classDef process fill:#bfb,stroke:#333,stroke-width:2px
    classDef worker fill:#fbb,stroke:#333,stroke-width:2px

    class API,Router api
    class RC,RR cache
    class BP,SP,BPP,BSP process
    class CW,CT,EPI,ESS,FP,FS worker
```

### Routes

```mermaid
graph TB
    %% Routes
    subgraph Routes
        RP["GET /processes"]:::route
        RPD["GET /processes/{process_id}"]:::route
        RE["POST /processes/{process_id}/execution"]:::route
        RJ["GET /jobs"]:::route
        RJS["GET /jobs/{job_id}"]:::route
        RJR["GET /jobs/{job_id}/results"]:::route
    end

    %% FastAPI Application
    subgraph FastAPI Application
        PM_get["ProcessManager.get_available_processes"]:::component
        PM_get_desc["ProcessManager.get_process_description"]:::component
        PM_exec["ProcessManager.execute_process"]:::component
        PM_list_jobs["ProcessManager.list_jobs"]:::component
        PM_job_status["ProcessManager.get_job_status"]:::component
        PM_job_results["ProcessManager.get_job_results"]:::component
        PR["ProcessRegistry"]:::component
        CT["CacheResultTask"]:::component
        CW["Celery Worker"]:::component

        %% Integrated Redis Stores
        RC["Redis Cache (Temporary Results)"]:::redis
        RR["Redis Registry (Process Metadata)"]:::redis
        CB["Redis Broker (Celery Tasks)"]:::redis
        CR["Redis Backend (Celery Results)"]:::redis
    end

    %% Routes to ProcessManager
    RP -->|List Processes| PM_get
    RPD -->|Get Process Description| PM_get_desc
    RE -->|Execute Process| PM_exec
    RJ -->|List Jobs| PM_list_jobs
    RJS -->|Get Job Status| PM_job_status
    RJR -->|Get Job Results| PM_job_results

    %% ProcessManager to Redis
    PM_get -->|Read Process Metadata| RR
    PM_get_desc -->|Read Process Metadata| RR
    PM_exec -->|Read/Write Temporary Results| RC
    PM_exec -->|Submit Task| CB
    PM_list_jobs -->|Read Job Metadata| CR
    PM_job_status -->|Read Job Metadata| CR
    PM_job_results -->|Read Job Results| CR

    %% ProcessManager to ProcessRegistry
    PM_get -->|Get Processes| PR
    PM_get_desc -->|Get Process| PR

    %% ProcessRegistry to Redis
    PR -->|Store/Retrieve Process Metadata| RR

    %% Celery Worker Flow
    CB -->|Distribute Tasks| CW
    CW -->|Execute Process| CT
    CT -->|Write Temporary Results| RC
    CW -->|Write Task Results| CR

    %% Styling
    classDef route fill:#f9f,stroke:#333,stroke-width:2px,color:#000
    classDef component fill:#bbf,stroke:#333,stroke-width:2px,color:#000
    classDef redis fill:#bfb,stroke:#333,stroke-width:2px,color:#000
```

### Usage

#### Parallel execution patterns

In addition to the standard `BaseProcess`, the library ships two base classes for
fan-out workloads.  Both patterns use a Celery **chord** internally: the
orchestrating `execute_process` task returns immediately after dispatching the
chord, so workers are never blocked while waiting for sub-tasks.  This makes
both patterns fully compatible with KEDA autoscaling.

##### `BaseParallelProcess` — data fan-out (split → map → merge)

Use this when a single large input can be split into independent chunks, each
processed by a separate worker, and the partial results merged into a final
output.

```python
from fastprocesses.core.base_process import BaseParallelProcess
from fastprocesses.processes.process_registry import register_process

@register_process("batch_upper_process")
class BatchUpperProcess(BaseParallelProcess):
    process_description = ProcessDescription(
        id="batch_upper_process",
        title="Batch Upper",
        version="1.0.0",
        description="Upper-cases a list of words, processing chunks in parallel.",
        jobControlOptions=[ProcessJobControlOptions.ASYNC_EXECUTE],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "words": ProcessInput(
                title="Words",
                description="List of words to upper-case",
                scheme=Schema(type="array", items={"type": "string"}),
            )
        },
        outputs={
            "words": ProcessOutput(
                title="Words",
                description="Upper-cased words",
                scheme=Schema(type="array", items={"type": "string"}),
            )
        },
    )

    def split_inputs(self, exec_body: dict) -> list[dict]:
        """Divide the word list into chunks of three."""
        words = exec_body["inputs"]["words"]
        chunk_size = 3
        return [
            {"inputs": {"words": words[i:i + chunk_size]}}
            for i in range(0, len(words), chunk_size)
        ]

    def execute_single(self, item: dict, job_progress_callback=None):
        """Process one chunk — called once per worker."""
        return {"words": [w.upper() for w in item["inputs"]["words"]]}

    def merge_results(self, results: list[dict]):
        """Flatten all partial word lists into one."""
        return {"words": [w for r in results for w in r["words"]]}
```

```bash
# Async execution
curl -s -X POST http://localhost:8000/processes/batch_upper_process/execution \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"words": ["alpha","beta","gamma","delta","epsilon","zeta","eta"]},
       "outputs": {"words": {}}, "mode": "async"}'
```

##### `BaseScatterProcess` — operation fan-out (`@parallel_step` → merge)

Use this when the same input needs to be analysed by several independent
operations simultaneously and the named results merged into a single output.
Decorate each operation with `@parallel_step`.

```python
from fastprocesses.core.base_process import BaseScatterProcess, parallel_step
from fastprocesses.processes.process_registry import register_process

@register_process("text_analysis_process")
class TextAnalysisProcess(BaseScatterProcess):
    process_description = ProcessDescription(
        id="text_analysis_process",
        title="Text Analysis",
        version="1.0.0",
        description="Analyses text: word count, char count and unique words in parallel.",
        jobControlOptions=[ProcessJobControlOptions.ASYNC_EXECUTE],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "text": ProcessInput(
                title="Text", description="Text to analyse",
                scheme=Schema(type="string"),
            )
        },
        outputs={
            "word_count": ProcessOutput(
                title="Word Count", description="Number of words",
                scheme=Schema(type="integer"),
            ),
            "char_count": ProcessOutput(
                title="Char Count", description="Number of characters",
                scheme=Schema(type="integer"),
            ),
            "unique_words": ProcessOutput(
                title="Unique Words", description="Sorted list of unique words",
                scheme=Schema(type="array", items={"type": "string"}),
            ),
        },
    )

    @parallel_step
    def count_words(self, exec_body: dict):
        return {"word_count": len(exec_body["inputs"]["text"].split())}

    @parallel_step
    def count_chars(self, exec_body: dict):
        return {"char_count": len(exec_body["inputs"]["text"])}

    @parallel_step
    def extract_unique(self, exec_body: dict):
        words = exec_body["inputs"]["text"].lower().split()
        return {"unique_words": sorted(set(words))}

    def merge_results(self, results: dict[str, dict]):
        return {
            "word_count":  results["count_words"]["word_count"],
            "char_count":  results["count_chars"]["char_count"],
            "unique_words": results["extract_unique"]["unique_words"],
        }
```

```bash
# Async execution
curl -s -X POST http://localhost:8000/processes/text_analysis_process/execution \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"text": "the quick brown fox jumps over the lazy dog"},
       "outputs": {"word_count": {}, "char_count": {}, "unique_words": {}},
       "mode": "async"}'
```

---

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

    async def execute(
        self,
        exec_body: Dict[str, Any],
        job_progress_callback: JobProgressCallback
    ) -> Dict[str, Any]:
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
     -H "Prefer: respond-async" \
     -d '{
            "inputs": {
                "input_text": "hello world"
            },
            "outputs": {
                "lower": {}
            }
       }'
```

Execute a process (sync):
```bash
curl -X POST "http://localhost:8000/processes/simple_process/execution" \
     -H "Content-Type: application/json" \
     -H "Prefer: respond-sync" \
     -d '{
            "inputs": {
                "input_text": "hello world"
            },
            "outputs": {
                "lower": {}
            }
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
RESULT_CACHE_HOST="redis"
RESULT_CACHE_PORT=6379
RESULT_CACHE_DB=1

CELERY_BROKER_HOST="redis"
CELERY_BROKER_PORT=6379
CELERY_BROKER_DB=0

CELERY_RESULTS_TTL_DAYS=365 # job results are stored for this time period
CELERY_TASK_TLIMIT_HARD=900 # seconds
CELERY_TASK_TLIMIT_SOFT=600 # seconds
RESULTS_TEMP_TTL_HOURS=48 # this period determines how long results can be retrieved from cache, when the inputs are exactly the same 
```

### Notes:
!IMPORTANT!: Cache hash key is based on original unprocessed inputs always. This ensures consistent caching and cache retrieval which does not depend on arbitrary processed data, which can change when the process is updated or changed!

### Version Notes

See [CHANGELOG.md](CHANGELOG.md) for the full history.

- **0.20.0**: Internal refactor of `celery_app` into modular components; no public API changes
- **0.19.0**: `resolve_remote_inputs` hook; `DataFetchError` exception; queue isolation for multi-instance deployments (`celery_queue` setting); process-specific validation hook (`validate_inputs`)
- **0.18.0**: Generic Helm chart; automated job status counter; `merge_results` receives `exec_body`; large request body handling
- **0.17.0**: Graceful handling of inaccessible remote JSON schemas
- **0.16.0**: `BaseParallelProcess` (data fan-out) and `BaseScatterProcess` + `@parallel_step` (operation fan-out); KEDA-compatible Celery chord execution
- **0.15.0**: Redis retry mechanism
- **0.14.0**: Renamed settings (`FP_` prefix); HTML landing page with content negotiation
- **0.14.0**: Renamed settings and allowed to add metadata to server app, added a html landing page
- **0.13.0**: Validation occurs against schema fragment provided by process description
- **0.12.0**: results will be retrieved from cache only if inputs and outputs are the same 
- **0.11.0**: improved error handling
- **0.10.0**: improved cache handling and added cache settings
- **0.9.0**: read process description from file, added set execution mode via Prefer-header
- **0.8.0**: added retry mechanism to Cache class and allow for separate connections each for Celery and results/jobs Cache
- **0.7.0**: added progress callback for job updates and SoftTimeLimit for tasks
- **0.6.0**: added paging to processes and jobs, including limit and offset query params
- **0.5.0**: Extended Schema model
- **0.4.0**: Added full OGC API Processes 1.0.0 Core compliance
- **0.3.0**: Added job control and output transmission options
- **0.2.0**: Added Redis caching and Celery integration
- **0.1.0**: Initial release with basic process support