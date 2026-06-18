# [DONE] Plan: Refactor `celery_app.py` — Strategy Pattern by Process Type

## TL;DR

Split the monolithic `execute_process` task into a pipeline + strategy-per-process-type architecture. Eliminates boolean flags (`chord_dispatched`, `retrying`), removes duplicated finalize logic, and makes execution paths explicit and linear.

## Decisions (settled)

- **SoftTimeLimitExceeded**: fail the job immediately (`_fail_job` + `raise`). No re-run attempt — the grace period is for process implementations to checkpoint, not for the wrapper to retry.
- **Job mode shutdown bug**: FIXED (targeted `control.shutdown(destination=[hostname])`). Already applied in `common.py`.
- **KEDA tuning**: FIXED (`listLength: 1`, `pollingInterval: 5`). Already applied in `kso-worker-scaledjob.yaml`.
- **Process types**: BaseProcess, BaseParallelProcess, BaseScatterProcess — each gets its own executor strategy.
- **Worker modes** (job mode on/off): remain in `common.py` via `task_postrun` signal — no changes needed beyond the targeted-shutdown fix already applied.

## Target Structure

```
worker/
    celery_app.py       ← thin: Celery task registrations only, imports from below
    job_status.py       ← update_job_status, _increment_and_report_progress, _cleanup_progress_counter
    executors.py        ← ExecutorStrategy ABC + StandardExecutor, ParallelExecutor, ScatterExecutor
    pipeline.py         ← _deserialize, _load_process, _run_pipeline (validate → resolve → late_validate)
    chord_tasks.py      ← execute_parallel_item, execute_scatter_step, finalize_parallel, finalize_scatter
```

## Steps

### Phase 1: Extract job status utilities → `worker/job_status.py`

1. Move `update_job_status`, `_increment_and_report_progress`, `_cleanup_progress_counter`, `_SUBTASK_PROGRESS_KEY` into `worker/job_status.py`.
   - Verify: imports resolve, `pytest tests/ -q` passes.

### Phase 2: Extract pipeline (validation+resolution) → `worker/pipeline.py`

2. Create `worker/pipeline.py` with:
   - `_deserialize(serialized_data: str | bytes) -> dict` — handles str/bytes normalization + `json.loads`
   - `_load_process(process_id: str, job_id: str) -> BaseProcess` — get from registry, fail job on error
   - `_run_pipeline(process: BaseProcess, process_id: str, data: dict, job_id: str) -> dict` — validate_inputs → resolve_remote_inputs → late_validate, updating job status at each step, raising `InputValidationError` on failure
   - Verify: unit tests pass.

### Phase 3: Extract executor strategies → `worker/executors.py`

3. Create `worker/executors.py`:
   ```
   class ExecutorStrategy(ABC):
       def execute(self, process, process_id, data, job_id, serialized_data, job_progress_callback) -> BaseModel | None

   class StandardExecutor(ExecutorStrategy):
       # calls process.run_execute(), catches SoftTimeLimitExceeded → fail + raise

   class ParallelExecutor(ExecutorStrategy):
       # calls _run_parallel(), returns None

   class ScatterExecutor(ExecutorStrategy):
       # calls _run_scatter(), returns None

   def get_executor(process: BaseProcess) -> ExecutorStrategy:
       if isinstance(process, BaseParallelProcess): return ParallelExecutor()
       if isinstance(process, BaseScatterProcess): return ScatterExecutor()
       return StandardExecutor()
   ```
   - `_run_parallel` and `_run_scatter` move here (they build and dispatch chords).
   - Verify: imports resolve, tests pass.

### Phase 4: Extract chord tasks → `worker/chord_tasks.py`

4. Move `execute_parallel_item`, `execute_scatter_step`, `finalize_parallel`, `finalize_scatter` into `worker/chord_tasks.py`.
5. Unify finalize logic into a shared `_finalize_chord(process_id, job_id, serialized_data, merge_fn) -> dict` helper. Both `finalize_parallel` and `finalize_scatter` delegate to it.
   - Verify: chord task names unchanged (`fastprocesses.finalize_parallel`, etc.), tests pass.

### Phase 5: Rewrite `celery_app.py` → thin orchestrator

6. `execute_process` becomes ~30 lines:
   ```python
   @celery_app.task(bind=True, name="fastprocesses.execute_process", base=CacheResultTask)
   def execute_process(self, process_id: str, serialized_data: str | bytes):
       job_id = self.request.id
       data = _deserialize(serialized_data)

       try:
           process = _load_process(process_id, job_id)
           data = _run_pipeline(process, process_id, data, job_id)
           callback = _make_progress_callback(job_id, process_id)
           result = get_executor(process).execute(
               process, process_id, data, job_id, serialized_data, callback
           )
       except celery.exceptions.Retry:
           raise
       except kombu.exceptions.OperationalError as e:
           raise self.retry(exc=e, countdown=10, max_retries=6)
       except (InputValidationError, ValueError, ValidationError) as e:
           _fail_job(job_id, str(e))
           raise
       except SoftTimeLimitExceeded:
           _fail_job(job_id, f"Process exceeded time limit (job_id={job_id}).")
           raise
       except Exception as e:
           _fail_job(job_id, f"Process execution failed. See logs (job_id={job_id}).")
           raise

       if result is None:
           return None  # chord dispatched

       _succeed_job(job_id)
       return result.model_dump(exclude_none=True)
   ```
   - No `finally` block, no boolean flags.
   - `CacheResultTask`, `check_cache`, `find_result_in_cache` stay here (small, task-registration-only).
   - Verify: full test suite passes, task names unchanged.

7. Update `celery_app.conf.include` in `common.py` to include new modules:
   ```python
   include=["fastprocesses.worker.celery_app", "fastprocesses.worker.chord_tasks"]
   ```

### Phase 6: Delete dead code

8. Remove commented-out `handle_execute_failure` block at the bottom of `celery_app.py`.
   - Verify: tests pass.

## Files Modified

- `src/fastprocesses/worker/celery_app.py` — gutted to thin orchestrator
- `src/fastprocesses/worker/job_status.py` — NEW
- `src/fastprocesses/worker/executors.py` — NEW
- `src/fastprocesses/worker/pipeline.py` — NEW
- `src/fastprocesses/worker/chord_tasks.py` — NEW
- `src/fastprocesses/common.py` — update `include` list

## Files NOT Modified

- `src/fastprocesses/core/base_process.py` — process ABC unchanged
- `src/fastprocesses/api/manager.py` — API-side strategy pattern unchanged
- `src/fastprocesses/common.py` — job mode / signal handlers stay (already fixed)

## Verification

1. `pytest tests/ -q` — all existing tests pass after each phase
2. Celery task names unchanged (grep for `name="fastprocesses.` in new files matches old)
3. `celery inspect registered` shows same task list as before
4. Manual smoke test: submit async request → parallel process → verify job completes
5. No new dependencies added

## Scope Boundaries

- **Included**: structural decomposition of `celery_app.py`, SoftTimeLimitExceeded fix, dead code removal
- **Excluded**: changing the Strategy pattern in `manager.py`, modifying process ABCs, changing Celery config, changing KEDA templates (already done), adding new features
