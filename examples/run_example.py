"""
Example processes for fastProcesses — three execution styles.

Start the API (one terminal):
    python examples/run_example.py

Start a Celery worker (second terminal):
    celery -A fastprocesses.common.celery_app worker --loglevel=info

─────────────────────────────────────────────────────────────────────────────
1.  simple_process / simple_process_2  — BaseProcess (single worker)
─────────────────────────────────────────────────────────────────────────────

Async execution (fire-and-forget, returns jobID):

    curl -s -X POST http://localhost:8000/processes/simple_process/execution \
         -H "Content-Type: application/json" \
         -d '{
               "inputs": {"input_text": "hello world"},
               "outputs": {"upper": {}, "lower": {}},
               "mode": "async"
             }' | python3 -m json.tool

Poll status  (replace <jobID> with the id from the response above):

    curl -s http://localhost:8000/jobs/<jobID> | python3 -m json.tool

Fetch result once status is "successful":

    curl -s http://localhost:8000/jobs/<jobID>/results | python3 -m json.tool

Sync execution (blocks until done, returns result directly):

    curl -s -X POST http://localhost:8000/processes/simple_process_2/execution \\
         -H "Content-Type: application/json" \\
         -d '{
               "inputs": {"input_text": "hello"},
               "outputs": {"output_text": {}},
               "mode": "sync"
             }' | python3 -m json.tool

─────────────────────────────────────────────────────────────────────────────
2.  batch_upper_process — BaseParallelProcess (data fan-out)
─────────────────────────────────────────────────────────────────────────────

The word list is split into chunks of 3.  Each chunk is queued as an
independent Celery task so N workers process N chunks truly in parallel.

    curl -s -X POST http://localhost:8000/processes/batch_upper_process/execution \
         -H "Content-Type: application/json" \
         -d '{
               "inputs": {
                 "words": ["alpha","beta","gamma","delta","epsilon","zeta","eta"]
               },
               "outputs": {"words": {}},
               "mode": "async"
             }' | python3 -m json.tool

    # → 3 tasks land in the Redis queue (one per chunk)
    # Poll and fetch result the same way as above.

─────────────────────────────────────────────────────────────────────────────
3.  text_analysis_process — BaseScatterProcess (operation fan-out)
─────────────────────────────────────────────────────────────────────────────

Three @parallel_step methods (count_words, count_chars, extract_unique)
each receive the full input and run on separate workers simultaneously.

    curl -s -X POST http://localhost:8000/processes/text_analysis_process/execution \\
         -H "Content-Type: application/json" \\
         -d '{
               "inputs": {"text": "the quick brown fox jumps over the lazy dog"},
               "outputs": {"result": {}},
               "mode": "async"
             }' | python3 -m json.tool

    # → 3 tasks land in the Redis queue (one per step)
    # Poll and fetch result the same way as above.
"""
import asyncio
from typing import Any, Callable

import uvicorn
from pydantic import BaseModel

from fastprocesses.api.server import OGCProcessesAPI
from fastprocesses.core.base_process import (
    BaseParallelProcess,
    BaseProcess,
    BaseScatterProcess,
    parallel_step,
)
from fastprocesses.core.models import (
    ProcessDescription,
    ProcessInput,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.core.types import JobProgressCallback
from fastprocesses.processes.process_registry import register_process


class TextModel(BaseModel):
    input_text: str

    class Config:
        json_schema_extra = {
            "example": {
                "input_text": "Hello, World!",
                "output_text": "HELLO, WORLD!",
            }
        } 

class TextModelOut(BaseModel):
    upper: str | None = None
    lower: str | None = None

    class Config:
        json_schema_extra = {
            "example": {
                "output_text": "HELLO, WORLD!",
            }
        }
@register_process("simple_process")
class SimpleProcess(BaseProcess):
    # Define process description as a class variable,
    # you can load it from a YAML file
    process_description = ProcessDescription.from_yaml(
            "examples/run_example.yaml"
    )

    async def execute(
        self,
        exec_body: dict[str, dict],
        job_progress_callback: JobProgressCallback | None = None
    ) -> BaseModel:

        # Report start if callback is provided

        if job_progress_callback:
            job_progress_callback(10, "Processing input")

        text_model = TextModel.model_validate(exec_body["inputs"])

        # Simulate some processing time
        if job_progress_callback:
            job_progress_callback(30, "Converting text")

        await asyncio.sleep(5)  # Simulate work

        output = {}
        if "upper" in exec_body["outputs"].keys():
            output["upper"] = text_model.input_text.upper()

        if "lower" in exec_body["outputs"].keys():
            output["lower"] = text_model.input_text.lower()

        output_model = TextModelOut.model_validate(output)

        if job_progress_callback:
            job_progress_callback(70, "Finalizing results")

        await asyncio.sleep(0.3)  # More simulated work

        if job_progress_callback:
            job_progress_callback(90, "Preparing output")

        # raise Exception("This is a test exception")

        return output_model

@register_process("simple_process_2")
class SimpleProcess_2(BaseProcess):
    # Define process description as a class variable
    process_description = ProcessDescription(
        id="simple_process_2",
        title="Simple Process",
        version="1.0.0",
        description="A simple example process",
        jobControlOptions=[
            ProcessJobControlOptions.SYNC_EXECUTE,
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "input_text": ProcessInput(
                title="Input Text",
                description="Text to process",
                schema=Schema(type="string", minLength=1, maxLength=10),
            )
        },
        outputs={
            "output_text": ProcessOutput(
                title="Output Text",
                description="Processed text",
                schema=Schema(type="string"),
            )
        },
        keywords=["text", "processing"],
        metadata={"created": "2024-02-19", "provider": "Example Organization"},
    )

    async def execute(
        self,
        exec_body: dict[str, Any],
        job_progress_callback: Callable[[int, str], None] | None = None
    ) -> BaseModel:

        # Report start if callback is provided
        if job_progress_callback:
            job_progress_callback(10, "Processing input")

        text_model = TextModel.model_validate(exec_body["inputs"])

        # Simulate some processing time
        if job_progress_callback:
            job_progress_callback(30, "Converting text")

        await asyncio.sleep(0.5)  # Simulate work
        output_text = text_model.input_text.upper()
        output_model = TextModelOut(upper=output_text)

        if job_progress_callback:
            job_progress_callback(70, "Finalizing results")

        await asyncio.sleep(0.3)  # More simulated work

        if job_progress_callback:
            job_progress_callback(90, "Preparing output")

        return output_model


# =============================================================================
# Example 3 — BaseParallelProcess (data fan-out)
#
# The same operation (upper-casing) is applied to independent chunks of the
# input list.  Each chunk is dispatched as a separate Celery task, so N workers
# process N chunks truly in parallel.  KEDA will scale the worker pool
# automatically as the chunk tasks land in the Redis queue.
#
# Pattern:  split_inputs  →  execute_single × N workers  →  merge_results
# =============================================================================


class WordBatch(BaseModel):
    words: list[str]


@register_process("batch_upper_process")
class BatchUpperProcess(BaseParallelProcess):
    """
    Accepts a list of words and returns them upper-cased.
    The list is split into chunks of 3; each chunk runs on its own worker.
    """

    process_description = ProcessDescription(
        id="batch_upper_process",
        title="Batch Upper Process",
        version="1.0.0",
        description=(
            "Upper-cases a list of words by processing fixed-size chunks in "
            "parallel.  Illustrates BaseParallelProcess (data fan-out)."
        ),
        jobControlOptions=[
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "words": ProcessInput(
                title="Words",
                description="List of words to upper-case",
                schema=Schema(type="array", items={"type": "string"}),
            )
        },
        outputs={
            "words": ProcessOutput(
                title="Upper-cased words",
                description="Every word converted to upper case",
                schema=Schema(type="array", items={"type": "string"}),
            )
        },
        keywords=["text", "parallel"],
    )

    # --- 1. Partition the input into independent chunks ---

    def split_inputs(self, exec_body: dict) -> list[dict]:
        """Split the word list into chunks of 3.  Each chunk → one worker."""
        words: list[str] = exec_body["inputs"]["words"]
        chunk_size = 3
        return [
            {"inputs": {"words": words[i : i + chunk_size]}}
            for i in range(0, len(words), chunk_size)
        ]

    # --- 2. Process one chunk (runs on a dedicated Celery worker) ---

    def execute_single(
        self,
        item: dict,
        job_progress_callback: JobProgressCallback | None = None,
    ) -> WordBatch:
        """Upper-case every word in a single chunk."""
        return WordBatch(words=[w.upper() for w in item["inputs"]["words"]])

    # --- 3. Stitch the partial results back together ---

    def merge_results(self, results: list[dict]) -> WordBatch:
        """Flatten the per-chunk results into one list, preserving order."""
        return WordBatch(words=[w for chunk in results for w in chunk["words"]])


# =============================================================================
# Example 4 — BaseScatterProcess (operation fan-out / scatter-gather)
#
# Three *different* analyses run on the same input text, each on its own
# Celery worker.  No chunking needed — the full input is broadcast to every
# @parallel_step.  KEDA scales one worker per step.
#
# Pattern:  @parallel_step × N workers (same input)  →  merge_results
# =============================================================================


class WordCountResult(BaseModel):
    count: int


class CharCountResult(BaseModel):
    count: int


class UniqueWordsResult(BaseModel):
    words: list[str]


class TextAnalysisResult(BaseModel):
    word_count: int
    char_count: int
    unique_words: list[str]


@register_process("text_analysis_process")
class TextAnalysisProcess(BaseScatterProcess):
    """
    Analyses a piece of text via three independent operations that run in
    parallel on separate workers:

      • count_words        — total word count
      • count_chars        — character count (excluding spaces)
      • extract_unique     — sorted list of unique lower-cased words

    Illustrates BaseScatterProcess (operation fan-out / scatter-gather).
    """

    process_description = ProcessDescription(
        id="text_analysis_process",
        title="Text Analysis Process",
        version="1.0.0",
        description=(
            "Runs three independent text analyses in parallel and merges the "
            "results.  Illustrates BaseScatterProcess (operation fan-out)."
        ),
        jobControlOptions=[
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "text": ProcessInput(
                title="Text",
                description="The text to analyse",
                schema=Schema(type="string", minLength=1),
            )
        },
        outputs={
            "result": ProcessOutput(
                title="Analysis result",
                description="Combined word count, char count and unique words",
                schema=Schema(type="object"),
            )
        },
        keywords=["text", "analysis", "scatter"],
    )

    # --- Each @parallel_step receives the full exec_body and runs on its
    #     own Celery worker.  The method name becomes the key in merge_results.

    @parallel_step
    def count_words(self, exec_body: dict) -> WordCountResult:
        text: str = exec_body["inputs"]["text"]
        return WordCountResult(count=len(text.split()))

    @parallel_step
    def count_chars(self, exec_body: dict) -> CharCountResult:
        text: str = exec_body["inputs"]["text"]
        return CharCountResult(count=len(text.replace(" ", "")))

    @parallel_step
    def extract_unique(self, exec_body: dict) -> UniqueWordsResult:
        text: str = exec_body["inputs"]["text"]
        return UniqueWordsResult(words=sorted({w.lower() for w in text.split()}))

    # --- merge_results receives {step_name: result_dict} and the original
    #     exec_body after all steps finish.

    def merge_results(self, results: dict[str, dict], exec_body: dict) -> TextAnalysisResult:
        return TextAnalysisResult(
            word_count=results["count_words"]["count"],
            char_count=results["count_chars"]["count"],
            unique_words=results["extract_unique"]["words"],
        )


# Create the FastAPI app
app = OGCProcessesAPI(
    contact={
        "name": "LGV Hamburg",
        "url": "https://example.com",
        "email": "support@support.com",
    },
    license={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0",
    },
    terms_of_service="https://example.com/terms",
).get_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        log_level="DEBUG",
    )
