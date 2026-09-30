# Plan: Dismiss extension — `DELETE /jobs/{jobID}`

## TL;DR

Wire a `DELETE /jobs/{jobID}` endpoint that (a) cancels running/queued jobs and
(b) removes per-job artifacts of finished jobs. The job status record is kept as
a small **tombstone** with `status: dismissed` so repeat calls return `410 Gone`.
Cancellation is two-layered: a best-effort Celery `revoke(terminate=True)` plus a
**cooperative check** in every worker entry point, because revoke alone is not
reliable in this deployment (KEDA scale-to-zero, `task_reject_on_worker_lost`,
chords with independent subtask IDs).

## Current state (findings)

- `ProcessManager.delete_job` exists but is broken and unused:
  `AsyncResult(job_id)` is always truthy, it only calls `result.forget()`, never
  revokes, never touches `job_status_cache`, and no route calls it.
- `JobStatusCode.DISMISSED` and `ProcessJobControlOptions.DISMISS` already exist.
- `update_job_status` overwrites any status unconditionally. A worker that is still
  running after dismissal would flip `dismissed` back to `running`/`successful`.
- Chord jobs: `execute_process` returns `None` immediately; the real work runs in
  `execute_parallel_item` / `execute_scatter_step` subtasks with their own task IDs,
  then `finalize_*`. Revoking only `job_id` would not stop them.
- `sigterm_handler` (registered in `worker/celery_app.py`) only logs. If pool
  children inherit it, `revoke(terminate=True)` with the default `SIGTERM` is a no-op.
- `task_reject_on_worker_lost=True` + `task_acks_late=True`: a hard-killed task may be
  **requeued** and run again.
- Job mode (`FP_CELERY_JOB_MODE`) shuts down in `task_postrun`, which does not fire
  for a killed child → the pod would idle until `activeDeadlineSeconds`.

## Behaviour (spec mapping)

| Current status            | Action                                                 | HTTP |
|---------------------------|--------------------------------------------------------|------|
| not found                 | —                                                      | 404 `no-such-job` |
| `accepted` / `running`    | mark dismissed → revoke → cleanup per-job keys          | 200 + StatusInfo (`dismissed`) |
| `successful` / `failed`   | mark dismissed → cleanup per-job keys + result backend  | 200 + StatusInfo (`dismissed`) |
| `dismissed`               | nothing                                                 | 410 Gone |

After dismissal:
- `GET /jobs/{id}` → 200 with the tombstone (`status: dismissed`).
- `GET /jobs/{id}/results` → 410 Gone (currently would hit Celery state / cache).
- `GET /jobs` → tombstones are listed (they are regular status records).

## Decisions

1. **Order: status first, then revoke.** Writing `DISMISSED` before revoking closes the
   race where the worker finishes between the two calls.
2. **`DISMISSED` is sticky.** `update_job_status` refuses to change a job away from
   `DISMISSED` (log at debug, return). This single guard neutralises every late
   write from progress callbacks, `finalize_*`, the `SoftTimeLimitExceeded` handler,
   and the `_load_process` failure path.
3. **Cooperative check is the source of truth; revoke is an optimisation.**
   - `execute_process`: first thing after `_deserialize`, if status is `DISMISSED`
     → log + return `None` (no cache write, `CacheResultTask.on_success` already
     skips `None`). Covers: queued tasks when no worker is running (revoke broadcast
     lost), requeued tasks after a hard kill.
   - `execute_parallel_item` / `execute_scatter_step`: same check → return
     `{"__dismissed__": True}` without running user code.
   - `finalize_parallel` / `finalize_scatter`: if dismissed → cleanup claim-check
     keys + progress keys, return `None`, do not merge or cache.
4. **Revoke all task IDs of a job.** At chord dispatch, pre-assign subtask IDs
   (`sig.set(task_id=...)`) and store them under `chord:tasks:{job_id}` (same TTL as
   other chord keys). The API revokes `[job_id, *subtask_ids, finalize_id]`.
5. **Kill signal: `SIGKILL`.** Deterministic regardless of the custom SIGTERM handler.
   The resulting `WorkerLostError` requeue is harmless thanks to decision 3.
   (Alternative: `SIGUSR1` → `SoftTimeLimitExceeded` inside the task for a graceful
   exit; only worth it if processes need to release external resources. Verify
   empirically — see Step 6.)
6. **Do not delete the shared result cache entry** (`calculation_task.celery_key`).
   It is content-addressed and reused by other jobs with identical inputs
   (`__result_ref__` pointers). Only per-job keys are removed:
   `temp_result_cache[job_id]`, `job_request_cache[job_id]`, Celery result backend
   (`AsyncResult.forget()` for all task IDs), `fp:subtask_progress:*`,
   `fp:scatter_*`, `chord:*:{job_id}*`.
7. **Job mode:** add a `task_revoked` signal handler in `common.py` that performs the
   same targeted shutdown as `task_postrun` when `FP_CELERY_JOB_MODE` is on.

## Steps

### 1. Sticky DISMISSED in `worker/job_status.py`
- In `update_job_status`, after validating `job_info`: if
  `job_info.status == DISMISSED` → return without writing.
- Add `is_job_dismissed(job_id) -> bool` helper (single `job_status_cache.get`).
- Verify: unit test — update after dismissal leaves status `dismissed`.

### 2. Cooperative checks in worker tasks
- `celery_app.execute_process`: early-return `None` when dismissed (before cache
  lookup and `_load_process`).
- `chord_tasks.execute_parallel_item` / `execute_scatter_step`: early-return
  `{"__dismissed__": True}`.
- `chord_tasks.finalize_parallel` / `finalize_scatter`: early-return `None` after
  cleaning up claim-check + progress keys (reuse existing cleanup helpers).
- Verify: eager-mode tests for each entry point with a pre-dismissed status →
  process methods (`execute`, `execute_single`, step, `merge_results`) are never called.

### 3. Track chord task IDs
- `_run_parallel` / `_run_scatter`: build signatures with explicit `task_id`
  (`uuid4()`), also for the callback; `temp_result_cache.put(f"chord:tasks:{job_id}", ids)`.
- Clean the key up in `finalize_*` (success, error-sentinel and dismissed paths).
- Verify: test asserts the key contains N+1 IDs after dispatch.

### 4. `ProcessManager.dismiss_job` in `api/manager.py`
Replace `delete_job` with:
```python
def dismiss_job(self, job_id: str) -> JobStatusInfo:
    # 404 → JobNotFoundError, 410 → new JobDismissedError
    # 1. write DISMISSED (+ message, updated, finished) to job_status_cache
    # 2. if previous status in (accepted, running): revoke all task IDs, SIGKILL
    # 3. forget() Celery results for all task IDs; delete per-job cache keys
    # 4. return updated JobStatusInfo (drop the "results" link)
```
- Broker errors during revoke are logged, not raised — the tombstone + cooperative
  check still guarantee the job will not complete.
- `get_job_result`: raise `JobDismissedError` when status is `DISMISSED`
  (check before `AsyncResult`).
- Add `JobDismissedError` to `core/exceptions.py`.
- Verify: unit tests for each row of the behaviour table (mock `celery_app.control`).

### 5. Router + conformance in `api/router.py`
- `@router.delete("/jobs/{job_id}")` → 200 `JobStatusInfo`; map
  `JobNotFoundError` → 404 `no-such-job`, `JobDismissedError` → 410 (OGC exception body).
- `GET /jobs/{job_id}/results`: map `JobDismissedError` → 410.
- Add `http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/dismiss` to `/conformance`.
- Advertise `dismiss` in `jobControlOptions` of process descriptions (see open question 2).
- Verify: `TestClient` tests for 200 / 404 / 410 and the conformance list.

### 6. Job mode + integration check
- `common.py`: `@task_revoked.connect` → targeted shutdown when job mode is on.
- Manual/integration test against docker-compose Redis + a real worker:
  1. long-running standard process → DELETE → worker child killed, status stays
     `dismissed`, no result cached, requeued message (if any) exits immediately.
  2. parallel process mid-chord → DELETE → remaining subtasks skip, finalize skips.
  3. DELETE on a successful job → 200, results → 410, second DELETE → 410.
  4. job mode worker exits after revoke.

### 7. Docs
- README / `docs/content/user_guide`: DELETE endpoint, curl example, note that
  shared result cache entries are not purged and that access control must be
  enforced upstream (reverse proxy / auth middleware).

## Open questions

1. **Access control.** fastprocesses has no auth layer. Options: (a) document that it
   must be enforced upstream, (b) add `FP_ENABLE_DISMISS: bool = True` to allow
   switching the endpoint off (route returns 405 / not registered, conformance
   class omitted). Recommendation: (a) + (b).
2. **Per-process opt-in?** Should `dismiss` be advertised for every process, or only
   when the process description lists it in `jobControlOptions`? Recommendation:
   global — cancellation is implemented by the library, not by the process author.
3. **Published references.** `OutputReferencePublisher` has no delete operation, so
   outputs published by reference (e.g. `LocalFileReferencePublisher`) would survive
   dismissal. Extend the protocol with an optional `unpublish(job_id)` now, or defer?
   Recommendation: defer; note it in docs.
4. **Tombstone TTL.** Keep the dismissed record for `FP_JOB_STATUS_TTL_DAYS` (current
   behaviour of the key) or shorten it? Recommendation: keep — it is tiny and needed
   for the 410 guarantee.
