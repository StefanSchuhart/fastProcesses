from datetime import datetime, timezone

from fastprocesses.common import job_status_cache
from fastprocesses.core.logging import logger
from fastprocesses.core.models import JobStatusCode, JobStatusInfo, Link

_SUBTASK_PROGRESS_KEY = "fp:subtask_progress"


def update_job_status(
    job_id: str,
    progress: int,
    message: str | None = None,
    status: str | None = None,
    started: datetime | None = None,
    process_id: str | None = None,
) -> None:
    job_key = f"job:{job_id}"
    raw_job_info = job_status_cache.get(job_key)

    if not raw_job_info:
        logger.warning(
            "Job status for job_id={} not found in cache when updating; "
            "initializing a fallback job status entry.",
            job_id,
        )
        fallback_status = status or JobStatusCode.RUNNING
        now = datetime.now(timezone.utc)
        job_info = JobStatusInfo.model_validate(
            {
                "jobID": job_id,
                "status": fallback_status,
                "type": "process",
                "processID": process_id,
                "created": now,
                "updated": now,
                "progress": progress,
                "message": message,
                "links": [
                    Link.model_validate(
                        {
                            "href": f"/jobs/{job_id}",
                            "rel": "self",
                            "type": "application/json",
                        }
                    )
                ],
            }
        )
        if fallback_status == JobStatusCode.SUCCESSFUL:
            job_info.finished = now
            job_info.links.append(
                Link.model_validate(
                    {
                        "href": f"/jobs/{job_id}/results",
                        "rel": "results",
                        "type": "application/json",
                    }
                )
            )
        job_status_cache.put(job_key, job_info)
        logger.debug(
            "Initialized fallback job status for job {}: {} at {}%",
            job_id,
            fallback_status,
            progress,
        )
        return

    try:
        job_info = JobStatusInfo.model_validate(raw_job_info)
    except Exception as exc:
        logger.error(
            "Failed to validate cached job status for job_id={}: {!r}", job_id, exc
        )
        return

    job_info.status = status or job_info.status
    job_info.progress = progress
    job_info.started = started or job_info.started
    job_info.updated = datetime.now(timezone.utc)

    if status == JobStatusCode.SUCCESSFUL:
        job_info.finished = datetime.now(timezone.utc)
        job_info.links.append(
            Link.model_validate(
                {
                    "href": f"/jobs/{job_info.jobID}/results",
                    "rel": "results",
                    "type": "application/json",
                }
            )
        )

    if message:
        job_info.message = message
    if process_id and not job_info.processID:
        job_info.processID = process_id

    job_status_cache.put(job_key, job_info)
    logger.debug(f"Updated progress for job {job_id}: {progress}%, {message}")


def _increment_and_report_progress(job_id: str, total: int) -> None:
    """
    Atomically increments the completed-subtask counter for *job_id* and
    reports proportional progress (0-90 %) to the job status cache.

    The remaining 10 % (90-100 %) is reserved for the merge step executed
    by ``finalize_parallel`` / ``finalize_scatter``.

    All exceptions are swallowed so that a Redis hiccup never causes a
    subtask to fail.
    """
    counter_key = f"{_SUBTASK_PROGRESS_KEY}:{job_id}"
    try:
        completed = job_status_cache.redis_connection._execute_redis_command(
            "incr", counter_key
        )
        if completed == 1:
            # Safety TTL so orphaned keys don't accumulate.
            job_status_cache.redis_connection._execute_redis_command(
                "expire", counter_key, 86400
            )
        progress = min(int(completed / total * 90), 90)
        update_job_status(
            job_id,
            progress,
            f"Completed {completed}/{total} subtasks.",
            JobStatusCode.RUNNING,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not update subtask progress for job {}: {!r}", job_id, exc
        )


def _cleanup_progress_counter(job_id: str) -> None:
    """Deletes the subtask progress counter key for *job_id*."""
    counter_key = f"{_SUBTASK_PROGRESS_KEY}:{job_id}"
    try:
        job_status_cache.redis_connection._execute_redis_command("delete", counter_key)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not clean up progress counter for job {}: {!r}", job_id, exc
        )
