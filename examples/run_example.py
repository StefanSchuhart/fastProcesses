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
         -H "Content-Type: application/json" \
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

    curl -s -X POST http://localhost:8000/processes/text_analysis_process/execution \
         -H "Content-Type: application/json" \
         -d '{
               "inputs": {"text": "the quick brown fox jumps over the lazy dog"},
               "outputs": {"result": {}},
               "mode": "async"
             }' | python3 -m json.tool

    # → 3 tasks land in the Redis queue (one per step)
    # Poll and fetch result the same way as above.

─────────────────────────────────────────────────────────────────────────────
4.  word_frequency_process — BaseProcessResult + multi-format output
─────────────────────────────────────────────────────────────────────────────

The process advertises two output formats for the same data.  The client
requests the desired format in the execute body.  If the format is omitted,
the resolver picks application/json (highest priority in MEDIA_TYPE_PRIORITY).

JSON output (default):

    curl -s -X POST http://localhost:8000/processes/word_frequency_process/execution \
         -H "Content-Type: application/json" \
         -H "Prefer: respond-sync" \
         -d '{
               "inputs": {"text": "the cat sat on the mat the cat"},
               "outputs": {"frequencies": {}},
               "response": "raw"
             }'

CSV output (client-requested format):

    curl -s -X POST http://localhost:8000/processes/word_frequency_process/execution \
         -H "Content-Type: application/json" \
         -H "Prefer: respond-sync" \
         -d '{
               "inputs": {"text": "the cat sat on the mat the cat"},
               "outputs": {
                 "frequencies": {
                   "format": {"mediaType": "text/csv"}
                 }
               },
               "response": "raw"
             }'

─────────────────────────────────────────────────────────────────────────────
5.  parallel_word_frequency_process — BaseParallelProcess + multi-format
─────────────────────────────────────────────────────────────────────────────

Counts combined word frequencies across a batch of texts using parallel
chunked execution.  merge_results() returns dict[str, BaseProcessResult]
to demonstrate multi-format output from a BaseParallelProcess.

JSON output (default):

    curl -s -X POST http://localhost:8000/processes/parallel_word_frequency_process/execution \
         -H "Content-Type: application/json" \
         -H "Prefer: respond-sync" \
         -d '{
               "inputs": {"texts": ["the cat sat", "the mat the cat"]},
               "outputs": {"frequencies": {}},
               "response": "raw"
             }'

CSV output:

    curl -s -X POST http://localhost:8000/processes/parallel_word_frequency_process/execution \
         -H "Content-Type: application/json" \
         -H "Prefer: respond-sync" \
         -d '{
               "inputs": {"texts": ["the cat sat", "the mat the cat"]},
               "outputs": {
                 "frequencies": {
                   "format": {"mediaType": "text/csv"}
                 }
               },
               "response": "raw"
             }'

─────────────────────────────────────────────────────────────────────────────
6.  scatter_text_summary_process — BaseScatterProcess + multi-format
─────────────────────────────────────────────────────────────────────────────

Runs three analyses (word count, char count, unique-word count) in parallel
on separate workers, then merges them into a TextSummaryResult that can be
served as JSON or CSV.  merge_results() returns dict[str, BaseProcessResult].

JSON output (default):

    curl -s -X POST http://localhost:8000/processes/scatter_text_summary_process/execution \
         -H "Content-Type: application/json" \
         -H "Prefer: respond-sync" \
         -d '{
               "inputs": {"text": "the quick brown fox jumps over the lazy dog"},
               "outputs": {"summary": {}},
               "response": "raw"
             }'

CSV output:

    curl -s -X POST http://localhost:8000/processes/scatter_text_summary_process/execution \
         -H "Content-Type: application/json" \
         -H "Prefer: respond-sync" \
         -d '{
               "inputs": {"text": "the quick brown fox jumps over the lazy dog"},
               "outputs": {
                 "summary": {
                   "format": {"mediaType": "text/csv"}
                 }
               },
               "response": "raw"
             }'
"""
import asyncio
import csv
import io
import json
import logging
from typing import Any, Callable, ClassVar

import uvicorn
from pydantic import BaseModel

from fastprocesses import BaseProcessResult
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

class TextModelOut(BaseProcessResult):
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
    ) -> TextModelOut:

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
                scheme=Schema(type="string", minLength=1, maxLength=10),
            )
        },
        outputs={
            "upper": ProcessOutput(
                title="Upper-cased Text",
                description="Processed text",
                scheme=Schema(type="string"),
            )
        },
        keywords=["text", "processing"],
        metadata={"created": "2024-02-19", "provider": "Example Organization"},
    )

    async def execute(
        self,
        exec_body: dict[str, Any],
        job_progress_callback: Callable[[int, str], None] | None = None
    ) -> TextModelOut:

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


class WordBatch(BaseProcessResult):
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
                scheme=Schema(
                    type="array", items=Schema.model_validate({"type": "string"})
                ),
            )
        },
        outputs={
            "words": ProcessOutput(
                title="Upper-cased words",
                description="Every word converted to upper case",
                scheme=Schema(
                    type="array", items=Schema.model_validate({"type": "string"})
                ),
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


class TextAnalysisResult(BaseProcessResult):
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
                scheme=Schema(type="string", minLength=1),
            )
        },
        outputs={
            "result": ProcessOutput(
                title="Analysis result",
                description="Combined word count, char count and unique words",
                scheme=Schema(type="object"),
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

    def merge_results(
        self, results: dict[str, dict], exec_body: dict
    ) -> TextAnalysisResult:
        return TextAnalysisResult(
            word_count=results["count_words"]["count"],
            char_count=results["count_chars"]["count"],
            unique_words=results["extract_unique"]["words"],
        )


# =============================================================================
# Example 4 — BaseProcessResult + OutputsHandler (multi-format output)
#
# The same word-frequency dict is served in two formats depending on what
# the client requests:
#   • application/json  →  {"word": count, ...}  (JSON object, default)
#   • text/csv          →  "word,count\n..."      (plain CSV)
#
# The process description advertises both via oneOf on the output schema.
# The library resolves which format to use, calls the right serializer on
# WordFrequencyResult, and builds the HTTP response automatically.
#
# Pattern:  execute() → dict[str, BaseProcessResult]  (new-style return)
# =============================================================================


class WordFrequencyResult(BaseProcessResult):
    """Word-frequency data that can be serialized to JSON or CSV.

    Fields are Pydantic model fields (stored in Redis as plain dict).
    output_serializers maps each output ID + media type to the method
    name that produces the corresponding bytes.
    """

    frequencies: dict[str, int]
    output_serializers: ClassVar = {
        "frequencies": {
            "application/json": "_to_json",
            "text/csv": "_to_csv",
        }
    }

    def _to_json(self) -> bytes:
        return json.dumps(self.frequencies, ensure_ascii=False, indent=2).encode()

    def _to_csv(self) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["word", "count"])
        for word, count in sorted(self.frequencies.items()):
            writer.writerow([word, count])
        return buffer.getvalue().encode()


@register_process("word_frequency_process")
class WordFrequencyProcess(BaseProcess):
    """Counts word frequencies and returns them in JSON or CSV format.

    The output format is chosen by the client via the execute request body:
        "outputs": {"frequencies": {"format": {"mediaType": "text/csv"}}}

    When no format is specified, application/json is used (it ranks highest
    in MEDIA_TYPE_PRIORITY among the two advertised types).
    """

    process_description = ProcessDescription(
        id="word_frequency_process",
        title="Word Frequency Process",
        version="1.0.0",
        description=(
            "Counts how often each word appears in the input text. "
            "Supports application/json and text/csv output formats. "
            "Demonstrates BaseProcessResult and multi-format output resolution."
        ),
        jobControlOptions=[
            ProcessJobControlOptions.SYNC_EXECUTE,
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "text": ProcessInput(
                title="Input Text",
                description="The text whose word frequencies to count",
                scheme=Schema(type="string", minLength=1),
            )
        },
        outputs={
            "frequencies": ProcessOutput(
                title="Word Frequencies",
                description=(
                    "Word frequency table. "
                    "Request application/json for a JSON object or "
                    "text/csv for a CSV file."
                ),
                scheme=Schema(
                    oneOf=[
                        # Branch 1: JSON object — not binary, nests inside
                        # a document response without base64 encoding
                        Schema(contentMediaType="application/json"),
                        # Branch 2: CSV string — type=string signals to the
                        # library that the wire value is an encoded string,
                        # not a JSON-native object
                        Schema(
                            type="string",
                            contentMediaType="text/csv",
                        ),
                    ]
                ),
            )
        },
        keywords=["text", "frequency", "multi-format"],
    )

    def execute(
        self,
        exec_body: dict[str, Any],
        job_progress_callback: JobProgressCallback | None = None,
    ) -> WordFrequencyResult:
        text: str = exec_body["inputs"]["text"]
        words = text.lower().split()

        frequencies: dict[str, int] = {}
        for word in words:
            frequencies[word] = frequencies.get(word, 0) + 1

        return WordFrequencyResult(frequencies=frequencies)


# =============================================================================
# Example 5 — BaseParallelProcess + multi-format output
#
# A list of texts is split into chunks of 2 and dispatched to separate Celery
# workers.  Each worker counts word frequencies for its chunk.  The finalize
# callback merges all per-chunk dicts and returns a WordFrequencyResult — the
# same ProcessResult used in example 4 — demonstrating that merge_results() can
# return a new-style dict[str, BaseProcessResult] just like execute() can.
# =============================================================================


class FrequencyChunk(BaseModel):  # intermediate, worker-internal
    """Partial word frequencies produced by a single parallel chunk."""

    frequencies: dict[str, int]


@register_process("parallel_word_frequency_process")
class ParallelWordFrequencyProcess(BaseParallelProcess):
    """Counts combined word frequencies across a list of texts in parallel.

    Splits the input list into chunks of two texts, dispatches each chunk as
    an independent Celery task, and merges the per-chunk frequency dicts in
    the finalize callback.  merge_results() returns a new-style
    dict[str, BaseProcessResult] so the output format (JSON or CSV) is chosen
    by the client in the same way as WordFrequencyProcess.
    """

    process_description = ProcessDescription(
        id="parallel_word_frequency_process",
        title="Parallel Word Frequency Process",
        version="1.0.0",
        description=(
            "Counts combined word frequencies across a list of texts. "
            "Work is split into chunks of two processed in parallel. "
            "Supports application/json and text/csv output formats. "
            "Demonstrates BaseParallelProcess with multi-format output."
        ),
        jobControlOptions=[
            ProcessJobControlOptions.SYNC_EXECUTE,
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "texts": ProcessInput(
                title="Input Texts",
                description="List of texts to count word frequencies across",
                scheme=Schema(
                    type="array",
                    items=Schema.model_validate({"type": "string"}),
                    minItems=1,
                ),
            )
        },
        outputs={
            "frequencies": ProcessOutput(
                title="Word Frequencies",
                description=(
                    "Combined word frequency table from all input texts. "
                    "Request application/json for a JSON object or "
                    "text/csv for a CSV file."
                ),
                scheme=Schema(
                    oneOf=[
                        Schema(contentMediaType="application/json"),
                        Schema(type="string", contentMediaType="text/csv"),
                    ]
                ),
            )
        },
        keywords=["text", "frequency", "parallel", "multi-format"],
    )

    def split_inputs(self, exec_body: dict) -> list[dict]:
        """Split the text list into chunks of two."""
        texts: list[str] = exec_body["inputs"]["texts"]
        chunk_size = 2
        return [
            {"inputs": {"texts": texts[i : i + chunk_size]}}
            for i in range(0, len(texts), chunk_size)
        ]

    def execute_single(
        self,
        item: dict,
        job_progress_callback: JobProgressCallback | None = None,
    ) -> FrequencyChunk:
        """Count word frequencies for the texts in one chunk."""
        frequencies: dict[str, int] = {}
        for text in item["inputs"]["texts"]:
            for word in text.lower().split():
                frequencies[word] = frequencies.get(word, 0) + 1
        return FrequencyChunk(frequencies=frequencies)

    def merge_results(
        self, results: list[dict]
    ) -> WordFrequencyResult:
        """Merge per-chunk frequency dicts and wrap in a WordFrequencyResult."""
        merged: dict[str, int] = {}
        for chunk in results:
            for word, count in chunk["frequencies"].items():
                merged[word] = merged.get(word, 0) + count
        return WordFrequencyResult(frequencies=merged)


# =============================================================================
# Example 6 — BaseScatterProcess + multi-format output
#
# Three different analyses run on the same text concurrently (scatter/gather).
# merge_results() assembles a TextSummaryResult that the library serializes
# to JSON or CSV depending on what the client requested — demonstrating
# multi-format output from a BaseScatterProcess.
# =============================================================================


class TextSummaryResult(BaseProcessResult):
    """Text analysis metrics that can be serialized to JSON or CSV."""

    word_count: int
    char_count: int
    unique_word_count: int
    output_serializers: ClassVar = {
        "summary": {
            "application/json": "_to_json",
            "text/csv": "_to_csv",
        }
    }

    def _to_json(self) -> bytes:
        return json.dumps(
            {
                "word_count": self.word_count,
                "char_count": self.char_count,
                "unique_word_count": self.unique_word_count,
            },
            ensure_ascii=False,
            indent=2,
        ).encode()

    def _to_csv(self) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric", "value"])
        for metric, val in [
            ("word_count", self.word_count),
            ("char_count", self.char_count),
            ("unique_word_count", self.unique_word_count),
        ]:
            writer.writerow([metric, val])
        return buffer.getvalue().encode()


@register_process("scatter_text_summary_process")
class ScatterTextSummaryProcess(BaseScatterProcess):
    """Runs three text analyses in parallel and merges them as JSON or CSV.

    Three @parallel_step methods (count_words, count_chars, count_unique)
    each receive the full input text and run on separate Celery workers.
    merge_results() returns a TextSummaryResult wrapped in a new-style
    dict[str, BaseProcessResult] so the wire format is chosen by the client.
    """

    process_description = ProcessDescription(
        id="scatter_text_summary_process",
        title="Scatter Text Summary Process",
        version="1.0.0",
        description=(
            "Runs word count, char count, and unique-word count in parallel "
            "then merges the results. "
            "Supports application/json and text/csv output formats. "
            "Demonstrates BaseScatterProcess with multi-format output."
        ),
        jobControlOptions=[
            ProcessJobControlOptions.SYNC_EXECUTE,
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "text": ProcessInput(
                title="Text",
                description="The text to analyse",
                scheme=Schema(type="string", minLength=1),
            )
        },
        outputs={
            "summary": ProcessOutput(
                title="Text Summary",
                description=(
                    "Word count, char count and unique-word count. "
                    "Request application/json for a JSON object or "
                    "text/csv for a CSV file."
                ),
                scheme=Schema(
                    oneOf=[
                        Schema(contentMediaType="application/json"),
                        Schema(type="string", contentMediaType="text/csv"),
                    ]
                ),
            )
        },
        keywords=["text", "analysis", "scatter", "multi-format"],
    )

    @parallel_step
    def count_words(self, exec_body: dict) -> WordCountResult:
        text: str = exec_body["inputs"]["text"]
        return WordCountResult(count=len(text.split()))

    @parallel_step
    def count_chars(self, exec_body: dict) -> CharCountResult:
        text: str = exec_body["inputs"]["text"]
        return CharCountResult(count=len(text.replace(" ", "")))

    @parallel_step
    def count_unique(self, exec_body: dict) -> UniqueWordsResult:
        text: str = exec_body["inputs"]["text"]
        return UniqueWordsResult(words=sorted({w.lower() for w in text.split()}))

    def merge_results(
        self, results: dict[str, dict], exec_body: dict
    ) -> TextSummaryResult:
        """Assemble a TextSummaryResult from the three parallel step outputs."""
        return TextSummaryResult(
            word_count=results["count_words"]["count"],
            char_count=results["count_chars"]["count"],
            unique_word_count=len(results["count_unique"]["words"]),
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
        log_level=logging.DEBUG,
    )
