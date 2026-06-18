"""
End-to-end tests for format-aware result serialization and caching.

These tests exercise the full worker → cache → router path without a running
broker or Redis by using Celery's task_always_eager mode and an in-memory dict
as the cache backend.

Coverage:
  1. BaseProcess with BaseProcessResult — raw/document response modes
  2. Cache hit: same inputs requested in a different format reuses the cached
     model-dump and re-serializes to the new format (no re-execution).
  3. Cache hit: raw vs document response mode reuses the same cache entry.
  4. BaseParallelProcess with BaseProcessResult — merge_results path.
"""
import json
from contextlib import ExitStack
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from typing import Any

from fastprocesses.common import celery_app
from fastprocesses.core.base_process import BaseParallelProcess, BaseProcess
from fastprocesses.core.models import (
    CalculationTask,
    ProcessDescription,
    ProcessInput,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    ResponseType,
    Schema,
)
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.core.outputs_handler import serialize_result
from fastprocesses.worker.celery_app import execute_process as _execute_process

# Cast to Any so Pyright can see the Celery Task attributes (.delay, .apply, …)
execute_process: Any = _execute_process


# ---------------------------------------------------------------------------
# Shared result class
# ---------------------------------------------------------------------------


class FrequencyResult(BaseProcessResult):
    """Word-frequency result that supports JSON and CSV serialization."""

    counts: dict[str, int]
    output_serializers: ClassVar = {
        "frequencies": {
            "application/json": "_to_json",
            "text/csv": "_to_csv",
        }
    }

    def _to_json(self) -> bytes:
        return json.dumps(self.counts, sort_keys=True, ensure_ascii=False).encode()

    def _to_csv(self) -> bytes:
        lines = ["word,count"] + [
            f"{w},{c}" for w, c in sorted(self.counts.items())
        ]
        return "\n".join(lines).encode()


_FREQ_DESC = ProcessDescription(
    id="word_freq",
    title="Word Frequency",
    version="1.0.0",
    description="Counts word frequencies.",
    jobControlOptions=[
        ProcessJobControlOptions.SYNC_EXECUTE,
        ProcessJobControlOptions.ASYNC_EXECUTE,
    ],
    outputTransmission=[ProcessOutputTransmission.VALUE],
    inputs={
        "text": ProcessInput(
            title="Text",
            description="Input text",
            scheme=Schema(type="string"),
        )
    },
    outputs={
        "frequencies": ProcessOutput(
            title="Frequencies",
            description="Word frequency table",
            scheme=Schema(
                oneOf=[
                    Schema(contentMediaType="application/json"),
                    Schema(type="string", contentMediaType="text/csv"),
                ]
            ),
        )
    },
)


class WordFreqProcess(BaseProcess):
    process_description = _FREQ_DESC

    def execute(self, exec_body: dict, job_progress_callback=None) -> FrequencyResult:
        words = exec_body["inputs"]["text"].lower().split()
        counts: dict[str, int] = {}
        for w in words:
            counts[w] = counts.get(w, 0) + 1
        return FrequencyResult(counts=counts)


# ---------------------------------------------------------------------------
# Parallel variant (merge_results → FrequencyResult)
# ---------------------------------------------------------------------------


class WordFreqParallelProcess(BaseParallelProcess):
    process_description = _FREQ_DESC

    def split_inputs(self, exec_body: dict) -> list[dict]:
        words = exec_body["inputs"]["text"].lower().split()
        mid = len(words) // 2
        return [
            {"inputs": {"text": " ".join(words[:mid])}},
            {"inputs": {"text": " ".join(words[mid:])}},
        ]

    def execute_single(self, item: dict, job_progress_callback=None) -> FrequencyResult:
        words = item["inputs"]["text"].lower().split()
        counts: dict[str, int] = {}
        for w in words:
            counts[w] = counts.get(w, 0) + 1
        return FrequencyResult(counts=counts)

    def merge_results(self, results: list[dict]) -> FrequencyResult:
        merged: dict[str, int] = {}
        for chunk in results:
            for w, c in chunk["counts"].items():
                merged[w] = merged.get(w, 0) + c
        return FrequencyResult(counts=merged)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEXT = "the cat sat on the mat the cat"
_EXPECTED_COUNTS = {"cat": 2, "mat": 1, "on": 1, "sat": 1, "the": 3}


@pytest.fixture
def eager_celery():
    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    yield
    celery_app.conf.update(task_always_eager=False, task_eager_propagates=False)


@pytest.fixture
def process():
    return WordFreqProcess()


@pytest.fixture
def mem_cache():
    """In-memory dict backing a mock temp_result_cache."""
    return {}


def _make_serialized(outputs: dict | None = None, response: str = "raw") -> str:
    return json.dumps(
        {"inputs": {"text": _TEXT}, "outputs": outputs, "response": response}
    )


def _patched_stack(stack, process_instance, mem_cache_dict):
    """Enter all standard patches and return (mock_cache, mock_registry)."""
    # get_process_registry is used via pipeline._load_process, not imported
    # directly into celery_app, so patch it at the pipeline module level.
    mock_registry = stack.enter_context(
        patch("fastprocesses.worker.pipeline.get_process_registry")
    )
    stack.enter_context(patch("fastprocesses.worker.celery_app.update_job_status"))
    stack.enter_context(patch("fastprocesses.worker.pipeline.update_job_status"))
    mock_cache = stack.enter_context(
        patch("fastprocesses.worker.celery_app.temp_result_cache")
    )
    mock_cache.get.side_effect = lambda key: mem_cache_dict.get(key)
    mock_cache.put.side_effect = lambda key, value: mem_cache_dict.update(
        {key: value}
    ) or json.dumps(value)
    mock_registry.return_value.get_process.return_value = process_instance
    return mock_cache, mock_registry


# ---------------------------------------------------------------------------
# 1. Worker produces the correct model_dump in the cache
# ---------------------------------------------------------------------------


def test_execute_process_stores_model_dump(eager_celery, process, mem_cache):
    """execute_process stores result.model_dump() in the cache."""
    serialized = _make_serialized()

    with ExitStack() as stack:
        _patched_stack(stack, process, mem_cache)
        execute_process.delay("word_freq", serialized).get()

    assert len(mem_cache) == 1
    cached = next(iter(mem_cache.values()))
    assert cached == {"counts": _EXPECTED_COUNTS}


# ---------------------------------------------------------------------------
# 2. serialize_result produces correct raw bytes for JSON and CSV
# ---------------------------------------------------------------------------


def test_serialize_result_raw_json():
    """serialize_result with response='raw' + JSON media type → correct bytes."""
    result = FrequencyResult(counts=_EXPECTED_COUNTS)
    requested_outputs = {"frequencies": {}}
    response = serialize_result(result, requested_outputs, "raw", _FREQ_DESC)

    assert response.media_type == "application/json"
    assert json.loads(response.body) == _EXPECTED_COUNTS


def test_serialize_result_raw_csv():
    """serialize_result with response='raw' + CSV media type → correct bytes."""
    result = FrequencyResult(counts=_EXPECTED_COUNTS)
    requested_outputs = {"frequencies": {"format": {"mediaType": "text/csv"}}}
    response = serialize_result(result, requested_outputs, "raw", _FREQ_DESC)

    assert response.media_type == "text/csv"
    lines = bytes(response.body).decode().splitlines()
    assert lines[0] == "word,count"
    data_lines = set(lines[1:])
    assert data_lines == {"cat,2", "mat,1", "on,1", "sat,1", "the,3"}


def test_serialize_result_document_json():
    """response='document' wraps output as OGC qualified value."""
    result = FrequencyResult(counts=_EXPECTED_COUNTS)
    response = serialize_result(result, {}, "document", _FREQ_DESC)

    body = json.loads(response.body)
    assert "frequencies" in body
    # JSON-native output: value is the object itself (not base64)
    assert body["frequencies"]["value"] == _EXPECTED_COUNTS
    assert body["frequencies"]["mediaType"] == "application/json"
    assert "encoding" not in body["frequencies"]


def test_serialize_result_document_csv():
    """response='document' with CSV media type → base64-encoded value."""
    result = FrequencyResult(counts=_EXPECTED_COUNTS)
    requested_outputs = {"frequencies": {"format": {"mediaType": "text/csv"}}}
    response = serialize_result(result, requested_outputs, "document", _FREQ_DESC)

    body = json.loads(response.body)
    assert body["frequencies"]["mediaType"] == "text/csv"
    assert body["frequencies"]["encoding"] == "base64"
    import base64

    decoded = base64.b64decode(body["frequencies"]["value"]).decode("utf-8")
    lines = decoded.splitlines()
    assert lines[0] == "word,count"


# ---------------------------------------------------------------------------
# 3. Cache hit: same inputs, different format → no re-execution
# ---------------------------------------------------------------------------


def test_cache_hit_different_format_skips_execution(eager_celery, process, mem_cache):
    """
    Two requests for the same inputs and the same output IDs but different
    media types must resolve to the same canonical cache key.  The second
    call must not re-run the process — it re-serializes the cached model_dump.

    Both `outputs={"frequencies": {}}` (default JSON) and
    `outputs={"frequencies": {"format": {"mediaType": "text/csv"}}}` normalise
    to the same sorted key list `["frequencies"]`, so they share one cache entry.
    """
    execute_spy = MagicMock(wraps=process.execute)
    process.execute = execute_spy

    serialized_json = _make_serialized(outputs={"frequencies": {}}, response="raw")
    serialized_csv = _make_serialized(
        outputs={"frequencies": {"format": {"mediaType": "text/csv"}}},
        response="raw",
    )

    # First call (JSON) → populates cache
    with ExitStack() as stack:
        _patched_stack(stack, process, mem_cache)
        execute_process.delay("word_freq", serialized_json).get()

    assert execute_spy.call_count == 1

    # Second call (CSV, same output IDs) → must hit cache, no re-execution
    with ExitStack() as stack:
        _patched_stack(stack, process, mem_cache)
        execute_process.delay("word_freq", serialized_csv).get()

    assert execute_spy.call_count == 1  # still 1 — not called again


# ---------------------------------------------------------------------------
# 4. Cache hit: raw vs document mode → same cache entry
# ---------------------------------------------------------------------------


def test_cache_hit_raw_and_document_share_key(eager_celery, process, mem_cache):
    """
    Requests that differ only in response mode ('raw' vs 'document') must
    resolve to the same canonical cache key and reuse the same cached result.
    """
    task_raw = CalculationTask(
        inputs={"text": _TEXT}, outputs=None, response=ResponseType.RAW
    )
    task_doc = CalculationTask(
        inputs={"text": _TEXT}, outputs=None, response=ResponseType.DOCUMENT
    )

    assert task_raw.celery_key == task_doc.celery_key


# ---------------------------------------------------------------------------
# 5. Full round-trip: worker stores, router reconstructs and serves
# ---------------------------------------------------------------------------


def test_full_round_trip_json(eager_celery, process, mem_cache):
    """Worker stores model_dump; router reconstructs FrequencyResult and serves JSON."""
    serialized = _make_serialized()

    with ExitStack() as stack:
        _patched_stack(stack, process, mem_cache)
        execute_process.delay("word_freq", serialized).get()

    cached = next(iter(mem_cache.values()))
    reconstructed = FrequencyResult.model_validate(cached)
    response = serialize_result(reconstructed, {}, "raw", _FREQ_DESC)

    assert response.media_type == "application/json"
    assert json.loads(response.body) == _EXPECTED_COUNTS


def test_full_round_trip_csv(eager_celery, process, mem_cache):
    """Worker stores model_dump; router reconstructs and serves CSV."""
    serialized = _make_serialized()

    with ExitStack() as stack:
        _patched_stack(stack, process, mem_cache)
        execute_process.delay("word_freq", serialized).get()

    cached = next(iter(mem_cache.values()))
    reconstructed = FrequencyResult.model_validate(cached)
    requested_outputs = {"frequencies": {"format": {"mediaType": "text/csv"}}}
    response = serialize_result(reconstructed, requested_outputs, "raw", _FREQ_DESC)

    assert response.media_type == "text/csv"
    lines = bytes(response.body).decode().splitlines()
    assert lines[0] == "word,count"
    assert len(lines) == 6  # header + 5 unique words
