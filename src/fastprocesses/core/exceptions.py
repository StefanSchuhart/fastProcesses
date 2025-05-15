class FastProcessesError(Exception):
    """Base class for all custom exceptions in FastProcesses."""

    pass


class JobNotFoundError(FastProcessesError):
    """Raised when a job is not found in the cache."""

    def __init__(self, job_id: str):
        super().__init__(f"Job {job_id} not found")


class JobNotReadyError(FastProcessesError):
    """Raised when a job result is not ready."""

    def __init__(self, job_id: str):
        super().__init__(f"Result for job ID {job_id} is not ready")


class JobFailedError(FastProcessesError):
    """Raised when a job has failed."""

    def __init__(self, job_id: str, error: str):
        super().__init__(f"Job failed: {error}")


class ProcessNotFoundError(FastProcessesError):
    """Raised when a process is not found in the registry."""

    def __init__(self, process_id: str):
        super().__init__(f"Process {process_id} not found")


class InputValidationError(FastProcessesError):
    """Raised when process input validation fails."""

    def __init__(self, process_id: str, error: str):
        super().__init__(f"Input validation failed for process {process_id}: {error}")


class OutputValidationError(FastProcessesError):
    """Raised when process output validation fails."""

    def __init__(self, process_id: str, error: str):
        super().__init__(f"Output validation failed for process {process_id}: {error}")
