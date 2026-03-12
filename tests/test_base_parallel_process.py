"""
Tests for BaseParallelProcess — split → parallel execute → merge pattern.

Two layers of coverage:
  1. Interface layer (no broker required) — verifies split_inputs, execute_single
     and merge_results behave correctly using the built-in serial fallback.
  2. Celery task layer (task_always_eager, no broker required) — exercises the
     execute_parallel_item task and _run_parallel orchestrator that run in
     production with real workers.
"""
import json
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from fastprocesses.common import celery_app
from fastprocesses.core.base_process import BaseParallelProcess
from fastprocesses.core.models import (
    ProcessDescription,
    ProcessInput,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.worker.celery_app import _run_parallel, execute_parallel_item

# ---------------------------------------------------------------------------
# Shared output model
# ---------------------------------------------------------------------------


class WordBatch(BaseModel):
    words: list[str]


# ---------------------------------------------------------------------------
# Concrete BaseParallelProcess used by all tests
#
# Given a list of words it:
#   split_inputs  → chunk the list into groups of CHUNK_SIZE
#   execute_single → upper-case one chunk
#   merge_results  → flatten all chunks back into a single list
# ---------------------------------------------------------------------------

CHUNK_SIZE = 3

_DESCRIPTION = ProcessDescription(
    id="batch_upper",
    title="Batch Upper",
    version="1.0.0",
    description="Upper-cases a word list by processing chunks in parallel.",
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
        "result": ProcessOutput(
            title="Result",
            description="Upper-cased words",
            scheme=Schema(type="array", items={"type": "string"}),
        )
    },
)


class BatchUpperProcess(BaseParallelProcess):
    process_description = _DESCRIPTION

    def split_inputs(self, exec_body: dict) -> list[dict]:
        words = exec_body["inputs"]["words"]
        return [
            {"inputs": {"words": words[i : i + CHUNK_SIZE]}}
            for i in range(0, len(words), CHUNK_SIZE)
        ]

    def execute_single(self, item: dict, job_progress_callback=None) -> WordBatch:
        return WordBatch(words=[w.upper() for w in item["inputs"]["words"]])

    def merge_results(self, results: list[dict]) -> WordBatch:
        return WordBatch(words=[w for r in results for w in r["words"]])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]
EXPECTED = [w.upper() for w in WORDS]  # 7 words → 3 chunks → merged back


@pytest.fixture
def process():
    return BatchUpperProcess()


@pytest.fixture
def exec_body():
    return {"inputs": {"words": WORDS}}


@pytest.fixture
def eager_celery():
    """Configure Celery to execute tasks synchronously (no broker needed)."""
    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    yield
    celery_app.conf.update(task_always_eager=False, task_eager_propagates=False)


# ---------------------------------------------------------------------------
# 1. Interface tests — no broker, no Redis
# ---------------------------------------------------------------------------


def test_split_produces_correct_chunks(process, exec_body):
    """7 words with CHUNK_SIZE=3 → 3 independent chunks."""
    chunks = process.split_inputs(exec_body)

    assert len(chunks) == 3
    assert chunks[0]["inputs"]["words"] == ["alpha", "beta", "gamma"]
    assert chunks[1]["inputs"]["words"] == ["delta", "epsilon", "zeta"]
    assert chunks[2]["inputs"]["words"] == ["eta"]


def test_execute_single_uppercases_one_chunk(process):
    """Each chunk is processed independently by execute_single."""
    item = {"inputs": {"words": ["hello", "world"]}}
    result = process.execute_single(item)

    assert isinstance(result, WordBatch)
    assert result.words == ["HELLO", "WORLD"]


def test_merge_combines_all_partial_results(process):
    """merge_results receives dicts (post-serialisation) and returns one model."""
    partials = [{"words": ["A", "B"]}, {"words": ["C", "D"]}, {"words": ["E"]}]
    merged = process.merge_results(partials)

    assert isinstance(merged, WordBatch)
    assert merged.words == ["A", "B", "C", "D", "E"]


# ---------------------------------------------------------------------------
# 2. Serial fallback tests (execute() — no broker required)
#
# In production this code path is bypassed by the Celery worker which
# dispatches the chunks in parallel.  It is kept so that subclasses work
# correctly in unit tests without a running broker.
# ---------------------------------------------------------------------------


def test_serial_fallback_produces_correct_output(process, exec_body):
    """execute() runs split → execute_single x N → merge in sequence."""
    result = process.execute(exec_body)

    assert isinstance(result, WordBatch)
    assert result.words == EXPECTED


def test_serial_fallback_calls_execute_single_once_per_chunk(process, exec_body):
    """
    With 7 words and CHUNK_SIZE=3, execute_single must be called exactly 3 times
    — once per chunk.  In production each call becomes a separate Celery task.
    """
    with patch.object(
        process, "execute_single", wraps=process.execute_single
    ) as mock:
        process.execute(exec_body)

    assert mock.call_count == 3
    # Verify each call received the correct chunk
    call_words = [call.args[0]["inputs"]["words"] for call in mock.call_args_list]
    assert call_words == [
        ["alpha", "beta", "gamma"],
        ["delta", "epsilon", "zeta"],
        ["eta"],
    ]


# ---------------------------------------------------------------------------
# 3. Celery task tests (task_always_eager=True — no broker required)
#
# These tests exercise the same code paths that run in production with real
# workers.  task_always_eager makes Celery execute tasks in-process so the
# test needs no running broker or Redis.
# ---------------------------------------------------------------------------


def test_execute_parallel_item_task(eager_celery, process):
    """
    execute_parallel_item is the subtask dispatched per chunk.
    It calls execute_single on the process and returns a serialisation-safe dict
    (the form merge_results receives in production).
    """
    item = {"inputs": {"words": ["foo", "bar", "baz"]}}

    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry:
        mock_registry.return_value.get_process.return_value = process
        async_result = execute_parallel_item.delay(
            "batch_upper", json.dumps(item)
        )

    assert async_result.get() == {"words": ["FOO", "BAR", "BAZ"]}


def test_run_parallel_fans_out_and_merges(eager_celery, process, exec_body):
    """
    _run_parallel splits the input, dispatches one execute_parallel_item
    subtask per chunk via a Celery group, then pipes all partial results
    through merge_results.

    With task_always_eager=True the subtasks run synchronously in-process.
    With real Celery workers (task_always_eager=False) they run concurrently
    across as many workers as are available.
    """
    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry, patch(
        "fastprocesses.worker.celery_app.update_job_status"
    ):
        mock_registry.return_value.get_process.return_value = process
        result = _run_parallel(
            service=process,
            process_id="batch_upper",
            data=exec_body,
            job_id="test-job-42",
            job_status="running",
        )

    assert isinstance(result, WordBatch)
    assert result.words == EXPECTED


def test_run_parallel_dispatches_one_subtask_per_chunk(eager_celery, process, exec_body):
    """
    The number of subtasks dispatched equals the number of chunks from
    split_inputs — confirming that every chunk gets its own worker slot.
    """
    subtask_args: list[tuple] = []
    original_s = execute_parallel_item.s

    def recording_s(*args, **kwargs):
        subtask_args.append(args)
        return original_s(*args, **kwargs)

    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry, patch(
        "fastprocesses.worker.celery_app.update_job_status"
    ), patch.object(execute_parallel_item, "s", side_effect=recording_s):
        mock_registry.return_value.get_process.return_value = process
        _run_parallel(
            service=process,
            process_id="batch_upper",
            data=exec_body,
            job_id="test-job-43",
            job_status="running",
        )

    expected_chunks = process.split_inputs(exec_body)
    assert len(subtask_args) == len(expected_chunks)  # 3 chunks → 3 subtasks
