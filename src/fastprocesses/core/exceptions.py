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

class ProcessClassNotFoundError(FastProcessesError):
    """Raised when a process class is not found in the registry."""

    def __init__(self, process_class: str):
        super().__init__(f"Process class {process_class} not found")


class BrokerUnavailableError(FastProcessesError):
    """Raised when the Celery broker (Redis) cannot be reached."""

    def __init__(self, detail: str):
        super().__init__(f"Broker unavailable: {detail}")


class ProcessRegistrationError(FastProcessesError):
    """Raised when a process class fails registration-time validation."""

    def __init__(self, process_id: str, reason: str):
        super().__init__(f"Cannot register process '{process_id}': {reason}")


class SerializationError(FastProcessesError):
    """Raised when a ProcessResult cannot serialize to the requested media type."""


class SSRFBlockedError(FastProcessesError):
    """Raised when a remote URL is blocked by SSRF protection."""

    def __init__(self, reason: str):
        super().__init__(f"Remote input blocked by SSRF protection: {reason}")


class ResultTooLargeError(FastProcessesError):
    """Raised when a cached result exceeds the configured/hardcoded size limit."""

    def __init__(self, key: str, size: int, limit: int):
        super().__init__(
            f"Result for key '{key}' is {size} bytes, exceeding the limit of "
            f"{limit} bytes"
        )