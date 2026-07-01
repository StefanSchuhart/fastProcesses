import json

from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.config import OGCProcessesSettings
from fastprocesses.core.exceptions import (
    InputValidationError,
    ProcessClassNotFoundError,
    SSRFBlockedError,
)
from fastprocesses.core.logging import logger
from fastprocesses.core.models import JobStatusCode
from fastprocesses.core.types import JobProgressCallback
from fastprocesses.processes.process_registry import get_process_registry
from fastprocesses.worker.job_status import update_job_status


def _deserialize(serialized_data: str | bytes) -> tuple[dict, str]:
    """Normalise bytes/str and parse JSON. Returns (data dict, normalised string)."""
    if isinstance(serialized_data, str):
        s = serialized_data
    else:
        s = bytes(serialized_data).decode("utf-8")
    return json.loads(s), s


def _load_process(process_id: str, job_id: str) -> BaseProcess:
    """Fetch the process from the registry, marking the job failed on error."""
    try:
        return get_process_registry().get_process(process_id)
    except ValueError as e:
        update_job_status(
            job_id,
            0,
            f"Process '{process_id}' not found.",
            JobStatusCode.FAILED,
        )
        raise e
    except ProcessClassNotFoundError:
        raise
    except Exception as e:
        # Covers pydoc.ErrorDuringImport, ImportError, FileNotFoundError, and
        # any other unexpected error raised while importing the process module
        # (e.g. a missing data file opened at import time).  These are
        # server-side errors, not caused by the user's inputs.
        logger.error(
            "Failed to load process '{}' for job {}: {}",
            process_id,
            job_id,
            e,
            exc_info=True,
        )
        update_job_status(
            job_id,
            0,
            (
                "Worker error: the process could not be loaded. "
                "This is not caused by your input — please contact the administrator."
            ),
            JobStatusCode.FAILED,
        )
        # Re-raise as a plain RuntimeError so Celery can pickle and store the
        # failure in the result backend.  The original exception (e.g.
        # pydoc.ErrorDuringImport) is not pickleable and would crash the
        # broker serialisation step, producing a misleading
        # UnpickleableExceptionWrapper instead of a clear FAILED task entry.
        raise RuntimeError(
            f"Process '{process_id}' could not be loaded: {type(e).__name__}: {e}"
        ) from None


def _run_pipeline(
    process: BaseProcess, process_id: str, data: dict, job_id: str,
    job_progress_callback: JobProgressCallback | None = None,
) -> dict:
    """
    Runs the pre-execution pipeline: validate → resolve remote inputs → late_validate.

    Updates the job status at each step and raises InputValidationError on failure.
    Returns the (possibly modified) data dict after remote input resolution.
    """
    # Step 1: validate wire-format inputs against the process description schema
    settings = OGCProcessesSettings()
    if settings.FP_SKIP_INPUT_VALIDATION:
        logger.warning(
            "FP_SKIP_INPUT_VALIDATION is enabled — skipping input validation "
            "for process {} (job {}). Do not use this in production.",
            process_id,
            job_id,
        )
    else:
        update_job_status(
            job_id, 0,
            "Pre-process step 1: Validating inputs against process description.",
            JobStatusCode.RUNNING
        )
        try:
            process.validate_inputs(data["inputs"])
        except ValueError as e:
            logger.error(f"Input validation failed for process {process_id}: {e}")
            update_job_status(job_id, 0, str(e), JobStatusCode.FAILED)
            raise InputValidationError(process_id, repr(e))

    # Step 2: resolve remote inputs (URI strings → downloaded data)
    update_job_status(
        job_id, 0,
        "Pre-process step 2: Resolving remote inputs.",
        JobStatusCode.RUNNING
    )
    try:
        data = process.resolve_remote_inputs(data, job_progress_callback)
    except (SSRFBlockedError, ValueError) as e:
        logger.error(
            "Remote input resolution failed for process {}: {}", process_id, e
        )
        update_job_status(job_id, 0, str(e), JobStatusCode.FAILED)
        raise InputValidationError(process_id, repr(e))

    # Step 3: late validation of resolved inputs (no-op by default)
    update_job_status(
        job_id, 0,
        "Pre-process step 3: Validating remotely resolved inputs.",
        JobStatusCode.RUNNING
    )
    try:
        process.late_validate(data["inputs"])
    except ValueError as e:
        logger.error("Late validation failed for process {}: {}", process_id, e)
        update_job_status(job_id, 0, str(e), JobStatusCode.FAILED)
        raise InputValidationError(process_id, repr(e))

    return data
