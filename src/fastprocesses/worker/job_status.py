from datetime import datetime, timezone
import json
from fastprocesses.common import job_status_cache
from fastprocesses.core.logging import logger
from fastprocesses.core.models import JobStatusCode, JobStatusInfo, Link

_SUBTASK_PROGRESS_KEY = "fp:subtask_progress"
_SCATTER_ALL_KEY = "fp:scatter_all"
_SCATTER_DONE_KEY = "fp:scatter_done"


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


def _init_scatter_roster(job_id: str, step_names: list[str]) -> None:
    """
    Stores the full list of scatter step names for *job_id* so that any
    subtask can later read it back to build the step-status message.
    Called once by ``_run_scatter`` before the chord is dispatched.
    """
    try:
        redis = job_status_cache.redis_connection
        all_key = f"{_SCATTER_ALL_KEY}:{job_id}"
        
        redis._execute_redis_command("set", all_key, json.dumps(step_names))
        redis._execute_redis_command("expire", all_key, 86400)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not initialise scatter roster for job {}: {!r}", job_id, exc
        )


def _record_scatter_step_done(job_id: str, step_name: str, total: int) -> None:
    """
    Marks *step_name* as completed for *job_id* and updates the job status
    message with a human-readable roster:

        Steps: vulnerability_pipeline ✓, exposure_pipeline ✓,
               hazard_wb_pipeline …, hazard_ma_pipeline … (2/4 done)

    Uses an atomic INCR for the progress percentage (same 0-90 % band as
    ``_increment_and_report_progress``) and a Redis set for the done-step
    roster so concurrent workers never race.

    All exceptions are swallowed so a Redis hiccup never causes a subtask
    to fail.
    """
    import json as json
    counter_key = f"{_SUBTASK_PROGRESS_KEY}:{job_id}"
    done_key = f"{_SCATTER_DONE_KEY}:{job_id}"
    all_key = f"{_SCATTER_ALL_KEY}:{job_id}"
    try:
        redis = job_status_cache.redis_connection
        completed = redis._execute_redis_command("incr", counter_key)
        if completed == 1:
            redis._execute_redis_command("expire", counter_key, 86400)
        redis._execute_redis_command("sadd", done_key, step_name)
        redis._execute_redis_command("expire", done_key, 86400)

        # Read back all_steps and done_steps to build the message.
        raw_all = redis._execute_redis_command("get", all_key)
        all_steps: list[str] = json.loads(raw_all) if raw_all else []
        done_raw = redis._execute_redis_command("smembers", done_key)
        done_steps: set[str] = {
            m.decode() if isinstance(m, bytes) else m for m in (done_raw or [])
        }

        parts = [
            f"{s} \u2713" if s in done_steps else f"{s} \u2026"
            for s in (all_steps or [step_name])
        ]
        message = ", ".join(parts) + f" ({len(done_steps)}/{total} done)"

        progress = min(int(completed / total * 90), 90)
        update_job_status(job_id, progress, message, JobStatusCode.RUNNING)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not record scatter step progress for job {}: {!r}", job_id, exc
        )


def _cleanup_scatter_roster(job_id: str) -> None:
    """Deletes scatter roster keys for *job_id* after the job completes."""
    try:
        redis = job_status_cache.redis_connection
        redis._execute_redis_command("delete", f"{_SCATTER_ALL_KEY}:{job_id}")
        redis._execute_redis_command("delete", f"{_SCATTER_DONE_KEY}:{job_id}")
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Could not clean up scatter roster for job {}: {!r}", job_id, exc
        )
