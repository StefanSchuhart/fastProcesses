from abc import ABC, abstractmethod

from pydantic import BaseModel

from fastprocesses.core.base_process import (
    BaseParallelProcess,
    BaseProcess,
    BaseScatterProcess,
)
from fastprocesses.core.types import JobProgressCallback
from fastprocesses.worker.chord_tasks import _run_parallel, _run_scatter


class ExecutorStrategy(ABC):
    @abstractmethod
    def execute(
        self,
        process: BaseProcess,
        process_id: str,
        data: dict,
        job_id: str,
        serialized_data: str,
        job_progress_callback: JobProgressCallback | None,
    ) -> BaseModel | None:
        """Returns the result for standard execution, None when a chord was dispatched."""
        ...


class StandardExecutor(ExecutorStrategy):
    def execute(self, process, process_id, data, job_id, serialized_data, job_progress_callback):
        return process.run_execute(data, job_progress_callback=job_progress_callback)


class ParallelExecutor(ExecutorStrategy):
    def execute(self, process, process_id, data, job_id, serialized_data, job_progress_callback):
        _run_parallel(process, process_id, data, job_id, serialized_data)
        return None


class ScatterExecutor(ExecutorStrategy):
    def execute(self, process, process_id, data, job_id, serialized_data, job_progress_callback):
        _run_scatter(process, process_id, data, job_id, serialized_data)
        return None


def get_executor(process: BaseProcess) -> ExecutorStrategy:
    if isinstance(process, BaseParallelProcess):
        return ParallelExecutor()
    if isinstance(process, BaseScatterProcess):
        return ScatterExecutor()
    return StandardExecutor()
