"""
Tests for BaseScatterProcess — scatter/gather pattern.

The same two layers as the parallel process tests:
  1. Interface layer (serial fallback, no broker required).
  2. Celery task layer (task_always_eager, no broker required).

Scenario: a geo-enrichment process receives a single set of coordinates and
runs three independent lookups (elevation, land-use, temperature) concurrently.
Each step is a @parallel_step method; merge_results stitches them together.
"""
import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from fastprocesses.common import celery_app
from fastprocesses.core.base_process import (
    BaseScatterProcess,
    get_parallel_steps,
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
from fastprocesses.worker.chord_tasks import (
    _run_scatter,
    execute_scatter_step,
    finalize_scatter,
)

# ---------------------------------------------------------------------------
# Partial result models (one per step)
# ---------------------------------------------------------------------------


class ElevationResult(BaseModel):
    value_m: float


class LandUseResult(BaseModel):
    category: str


class TemperatureResult(BaseModel):
    celsius: float


class GeoEnrichResult(BaseModel):
    elevation_m: float
    land_use: str
    temperature_c: float


# ---------------------------------------------------------------------------
# Concrete BaseScatterProcess
# ---------------------------------------------------------------------------

_DESCRIPTION = ProcessDescription(
    id="geo_enrich",
    title="Geo Enrich",
    version="1.0.0",
    description="Enriches coordinates with elevation, land-use and temperature.",
    jobControlOptions=[ProcessJobControlOptions.ASYNC_EXECUTE],
    outputTransmission=[ProcessOutputTransmission.VALUE],
    inputs={
        "lat": ProcessInput(
            title="Latitude",
            description="WGS-84 latitude",
            scheme=Schema(type="number"),
        ),
        "lon": ProcessInput(
            title="Longitude",
            description="WGS-84 longitude",
            scheme=Schema(type="number"),
        ),
    },
    outputs={
        "result": ProcessOutput(
            title="Result",
            description="Enriched data",
            scheme=Schema(type="object"),
        )
    },
)


class GeoEnrichProcess(BaseScatterProcess):
    process_description = _DESCRIPTION

    @parallel_step
    def get_elevation(self, exec_body: dict) -> ElevationResult:
        # Stub — a real implementation would call an external service
        return ElevationResult(value_m=342.5)

    @parallel_step
    def get_land_use(self, exec_body: dict) -> LandUseResult:
        return LandUseResult(category="forest")

    @parallel_step
    def get_temperature(self, exec_body: dict) -> TemperatureResult:
        return TemperatureResult(celsius=12.3)

    def merge_results(self, results: dict[str, dict], exec_body: dict) -> GeoEnrichResult:
        return GeoEnrichResult(
            elevation_m=results["get_elevation"]["value_m"],
            land_use=results["get_land_use"]["category"],
            temperature_c=results["get_temperature"]["celsius"],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EXEC_BODY = {"inputs": {"lat": 53.55, "lon": 10.0}}


@pytest.fixture
def process() -> BaseScatterProcess:
    return GeoEnrichProcess()


@pytest.fixture
def serialized_data():
    """Minimal CalculationTask JSON — the same shape execute_process receives."""
    return json.dumps(
        {"inputs": EXEC_BODY["inputs"], "outputs": None, "response": "raw"}
    )


@pytest.fixture
def eager_celery():
    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    yield
    celery_app.conf.update(task_always_eager=False, task_eager_propagates=False)


# ---------------------------------------------------------------------------
# 1. @parallel_step decorator tests
# ---------------------------------------------------------------------------


def test_parallel_step_sets_flag():
    """The decorator sets _is_parallel_step=True on the function."""
    assert getattr(GeoEnrichProcess.get_elevation, "_is_parallel_step", False) is True
    assert getattr(GeoEnrichProcess.get_land_use, "_is_parallel_step", False) is True
    assert getattr(GeoEnrichProcess.get_temperature, "_is_parallel_step", False) is True


def test_undecorated_method_has_no_flag(process: BaseScatterProcess):
    """merge_results is NOT a parallel step."""
    assert getattr(process.merge_results, "_is_parallel_step", False) is False


def test_get_parallel_steps_returns_all_decorated_methods(process):
    steps = get_parallel_steps(process)

    assert set(steps) == {"get_elevation", "get_land_use", "get_temperature"}
    # Values are bound methods — callable
    assert all(callable(m) for m in steps.values())


# ---------------------------------------------------------------------------
# 2. Serial fallback tests (no broker)
# ---------------------------------------------------------------------------


def test_serial_fallback_produces_correct_output(process):
    """execute() runs all steps serially and merges them correctly."""
    result = process.execute(EXEC_BODY)

    assert isinstance(result, GeoEnrichResult)
    assert result.elevation_m == 342.5
    assert result.land_use == "forest"
    assert result.temperature_c == 12.3


def test_serial_fallback_calls_every_step_exactly_once(process: BaseScatterProcess):
    """Each @parallel_step is called once — once per worker in production."""
    with (
        patch.object(process, "get_elevation", wraps=process.get_elevation) as e_mock,
        patch.object(process, "get_land_use", wraps=process.get_land_use) as l_mock,
        patch.object(
            process, "get_temperature", wraps=process.get_temperature
        ) as t_mock,
    ):
        process.execute(EXEC_BODY)

    assert e_mock.call_count == 1
    assert l_mock.call_count == 1
    assert t_mock.call_count == 1


def test_no_parallel_steps_raises(process: BaseScatterProcess):
    """A subclass with no @parallel_step methods raises NotImplementedError."""

    class EmptyScatter(BaseScatterProcess):
        process_description = _DESCRIPTION

        def merge_results(self, results):
            return GeoEnrichResult(elevation_m=0, land_use="", temperature_c=0)

    with pytest.raises(NotImplementedError, match="no @parallel_step"):
        EmptyScatter().execute(EXEC_BODY)


# ---------------------------------------------------------------------------
# 3. Celery task tests (task_always_eager=True — no broker required)
# ---------------------------------------------------------------------------


def test_execute_scatter_step_task(eager_celery, process):
    """
    execute_scatter_step loads its input from temp_result_cache (claim-check),
    runs the step, stores the result back in the cache, and returns a tiny
    marker dict instead of the full result.
    """
    store = {
        "chord:payload:test-job": {
            "step_input": EXEC_BODY,
            "original_input": EXEC_BODY,
        }
    }

    with ExitStack() as stack:
        mock_registry = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.get_process_registry")
        )
        stack.enter_context(
            patch("fastprocesses.worker.chord_tasks._increment_and_report_progress")
        )
        mock_cache = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.temp_result_cache")
        )
        mock_cache.get.side_effect = lambda key: store.get(key)
        mock_cache.put.side_effect = lambda key, value: store.update({key: value})
        mock_registry.return_value.get_process.return_value = process

        result = execute_scatter_step.delay(
            "geo_enrich", "test-job", 3, "get_elevation", "chord:payload:test-job"
        ).get()

    assert result == {"__claim_check__": "chord:result:test-job:get_elevation"}
    assert store["chord:result:test-job:get_elevation"] == {"value_m": 342.5}


def test_execute_scatter_step_unknown_step_raises(eager_celery, process):
    """Requesting a non-existent step name raises ValueError."""
    store = {
        "chord:payload:test-job": {
            "step_input": EXEC_BODY,
            "original_input": EXEC_BODY,
        }
    }

    with ExitStack() as stack:
        mock_registry = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.get_process_registry")
        )
        stack.enter_context(patch("fastprocesses.worker.chord_tasks.update_job_status"))
        stack.enter_context(
            patch("fastprocesses.worker.chord_tasks._increment_and_report_progress")
        )
        mock_cache = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.temp_result_cache")
        )
        mock_cache.get.side_effect = lambda key: store.get(key)
        mock_registry.return_value.get_process.return_value = process
        with pytest.raises(ValueError, match="not found"):
            execute_scatter_step.delay(
                "geo_enrich", "test-job", 1, "nonexistent_step", "chord:payload:test-job"
            ).get()


def test_run_scatter_fans_out_and_merges(
    eager_celery, process, serialized_data
):
    """
    _run_scatter stores a single payload in temp_result_cache and dispatches a
    chord of N steps + finalize_scatter.  With task_always_eager=True the
    whole chord runs synchronously using the dict-based cache mock, so
    finalize_scatter has already merged and stored the result by the time
    _run_scatter returns.
    """
    store: dict = {}

    def cache_put(key, value):
        store[key] = value

    with ExitStack() as stack:
        mock_registry = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.get_process_registry")
        )
        stack.enter_context(patch("fastprocesses.worker.chord_tasks.update_job_status"))
        stack.enter_context(
            patch("fastprocesses.worker.chord_tasks._increment_and_report_progress")
        )
        mock_cache = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.temp_result_cache")
        )
        mock_cache.get.side_effect = lambda key: store.get(key)
        mock_cache.put.side_effect = cache_put
        mock_cache.delete.side_effect = lambda key: store.pop(key, None)
        mock_registry.return_value.get_process.return_value = process
        _run_scatter(
            process=process,
            process_id="geo_enrich",
            data=EXEC_BODY,
            job_id="test-job-scatter-1",
            serialized_data=serialized_data,
        )

    # finalize_scatter stored the merged result under the job_id key
    cached = store["test-job-scatter-1"]
    assert cached["elevation_m"] == 342.5
    assert cached["land_use"] == "forest"
    assert cached["temperature_c"] == 12.3


def test_run_scatter_dispatches_one_subtask_per_step(
    process, serialized_data
):
    """
    The number of chord-header subtasks equals the number of @parallel_step
    methods.  Each subtask receives a claim-check key string (not serialised
    data) and all steps share the same key.
    """
    subtask_args: list[tuple] = []
    original_s = execute_scatter_step.s

    def recording_s(*args, **kwargs):
        subtask_args.append(args)
        return original_s(*args, **kwargs)

    mock_chord = MagicMock()
    with ExitStack() as stack:
        # Patch chord itself so no tasks actually run — we only verify dispatch.
        stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.chord", return_value=mock_chord)
        )
        stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.temp_result_cache")
        )
        stack.enter_context(
            patch.object(execute_scatter_step, "s", side_effect=recording_s)
        )
        _run_scatter(
            process=process,
            process_id="geo_enrich",
            data=EXEC_BODY,
            job_id="test-job-scatter-2",
            serialized_data=serialized_data,
        )

    expected_steps = get_parallel_steps(process)
    assert len(subtask_args) == len(expected_steps)  # 3 steps → 3 subtasks

    # All steps receive the same payload key (claim-check), not raw data
    dispatched_keys = [args[4] for args in subtask_args]
    assert len(set(dispatched_keys)) == 1
    assert dispatched_keys[0].startswith("chord:payload:")


def test_finalize_scatter_merges_and_caches(
    eager_celery, process, serialized_data
):
    """
    finalize_scatter resolves claim-check keys, calls merge_results, caches
    the output, and marks the job as SUCCESSFUL.
    """
    job_id = "test-job-scatter-3"
    step_names = ["get_elevation", "get_land_use", "get_temperature"]
    store = {
        f"chord:result:{job_id}:get_elevation": {"value_m": 342.5},
        f"chord:result:{job_id}:get_land_use": {"category": "forest"},
        f"chord:result:{job_id}:get_temperature": {"celsius": 12.3},
        f"chord:payload:{job_id}": {
            "step_input": EXEC_BODY,
            "original_input": json.loads(serialized_data),
        },
    }
    step_results = [
        {"__claim_check__": f"chord:result:{job_id}:{n}"} for n in step_names
    ]

    with ExitStack() as stack:
        mock_registry = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.get_process_registry")
        )
        mock_update = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.update_job_status")
        )
        mock_cache = stack.enter_context(
            patch("fastprocesses.worker.chord_tasks.temp_result_cache")
        )
        mock_cache.get.side_effect = lambda key: store.get(key)
        mock_cache.put.side_effect = lambda key, value: store.update({key: value})
        mock_cache.delete.side_effect = lambda key: store.pop(key, None)
        mock_registry.return_value.get_process.return_value = process
        merged = finalize_scatter(
            step_results,
            step_names,
            "geo_enrich",
            job_id,
            f"chord:payload:{job_id}",
        )

    assert merged["elevation_m"] == 342.5
    assert merged["land_use"] == "forest"
    assert merged["temperature_c"] == 12.3
    assert store.get(job_id) == merged  # stored under job_id
    last_status = mock_update.call_args_list[-1][0][3]
    assert last_status == "successful"
