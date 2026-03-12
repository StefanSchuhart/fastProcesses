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
from unittest.mock import patch

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
from fastprocesses.worker.celery_app import (
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

    def merge_results(self, results: dict[str, dict]) -> GeoEnrichResult:
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
    execute_scatter_step is the per-step subtask.
    It looks up the step by name, calls it, and returns a serialisation-safe dict.
    """
    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry:
        mock_registry.return_value.get_process.return_value = process
        result = execute_scatter_step.delay(
            "geo_enrich", "get_elevation", json.dumps(EXEC_BODY)
        ).get()

    assert result == {"value_m": 342.5}


def test_execute_scatter_step_unknown_step_raises(eager_celery, process):
    """Requesting a non-existent step name raises ValueError."""
    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry:
        mock_registry.return_value.get_process.return_value = process
        with pytest.raises(ValueError, match="not found"):
            execute_scatter_step.delay(
                "geo_enrich", "nonexistent_step", json.dumps(EXEC_BODY)
            ).get()


def test_run_scatter_fans_out_and_merges(
    eager_celery, process, serialized_data
):
    """
    _run_scatter dispatches a chord: one execute_scatter_step per @parallel_step
    + a finalize_scatter callback, all with the same input.  With
    task_always_eager=True the chord runs synchronously, so finalize_scatter
    has already stored the merged result in the cache by the time _run_scatter
    returns.
    """
    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry, patch(
        "fastprocesses.worker.celery_app.update_job_status"
    ), patch(
        "fastprocesses.worker.celery_app.temp_result_cache"
    ) as mock_cache:
        mock_registry.return_value.get_process.return_value = process
        _run_scatter(
            service=process,
            process_id="geo_enrich",
            data=EXEC_BODY,
            job_id="test-job-scatter-1",
            serialized_data=serialized_data,
        )

    assert mock_cache.put.called
    cached = mock_cache.put.call_args[1]["value"]
    assert cached["elevation_m"] == 342.5
    assert cached["land_use"] == "forest"
    assert cached["temperature_c"] == 12.3


def test_run_scatter_dispatches_one_subtask_per_step(
    eager_celery, process, serialized_data
):
    """
    The number of chord-header subtasks equals the number of @parallel_step
    methods — confirming each step gets its own worker slot in production.
    """
    subtask_args: list[tuple] = []
    original_s = execute_scatter_step.s

    def recording_s(*args, **kwargs):
        subtask_args.append(args)
        return original_s(*args, **kwargs)

    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry, patch(
        "fastprocesses.worker.celery_app.update_job_status"
    ), patch(
        "fastprocesses.worker.celery_app.temp_result_cache"
    ), patch.object(execute_scatter_step, "s", side_effect=recording_s):
        mock_registry.return_value.get_process.return_value = process
        _run_scatter(
            service=process,
            process_id="geo_enrich",
            data=EXEC_BODY,
            job_id="test-job-scatter-2",
            serialized_data=serialized_data,
        )

    expected_steps = get_parallel_steps(process)
    assert len(subtask_args) == len(expected_steps)  # 3 steps → 3 subtasks

    # Every step receives the same serialised input
    dispatched_data = [json.loads(args[2]) for args in subtask_args]
    assert all(d == EXEC_BODY for d in dispatched_data)


def test_finalize_scatter_merges_and_caches(
    eager_celery, process, serialized_data
):
    """
    finalize_scatter is the chord callback.  Given the per-step results it
    reassociates them with step names, calls merge_results, caches the output,
    and marks the job as SUCCESSFUL.
    """
    step_results = [
        {"value_m": 342.5},   # get_elevation
        {"category": "forest"},  # get_land_use
        {"celsius": 12.3},    # get_temperature
    ]
    step_names = ["get_elevation", "get_land_use", "get_temperature"]

    with patch(
        "fastprocesses.worker.celery_app.get_process_registry"
    ) as mock_registry, patch(
        "fastprocesses.worker.celery_app.update_job_status"
    ) as mock_update, patch(
        "fastprocesses.worker.celery_app.temp_result_cache"
    ) as mock_cache:
        mock_registry.return_value.get_process.return_value = process
        merged = finalize_scatter(
            step_results,
            step_names,
            "geo_enrich",
            "test-job-scatter-3",
            serialized_data,
        )

    assert merged["elevation_m"] == 342.5
    assert merged["land_use"] == "forest"
    assert merged["temperature_c"] == 12.3
    assert mock_cache.put.called
    last_status = mock_update.call_args_list[-1][0][3]
    assert last_status == "successful"
