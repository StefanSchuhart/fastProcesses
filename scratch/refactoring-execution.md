
## Refactor: Execution Strategy Cleanup (manager.py)

### Problems

1. **`AsyncExecutionStrategy.execute` is dead code.** `execute_process` always
   routes async calls to `execute_raw`. The `@abstractmethod` implementation on
   the class is never called, misleading future developers.

2. **Async path lost its API-side cache check.** `execute_raw` always submits
   to Celery. The old `execute()` short-circuited with a Redis GET at the API
   level. Now every async call, even cache hits, incurs:
   broker round-trip → queue → worker pick-up → Redis GET → job-status write.

3. **Strategy pattern adds indirection without benefit.** There is only one
   call site (`execute_process`), strategies are never swapped at runtime, and
   the raw/non-raw split already broke the abstraction.

4. **Sync path is asymmetric without explanation.** No raw/fast path exists for
   sync; comments say it's acceptable for small jobs, but this is not enforced
   or documented at the decision point.

### Plan

#### Step 1 — Replace strategy classes with two flat private methods

Remove `ExecutionStrategy`, `AsyncExecutionStrategy`, `SyncExecutionStrategy`.
Add to `ProcessManager`:

```python
def _execute_async(self, process_id: str, data: ProcessExecRequestBody, raw_body: bytes) -> ProcessExecResponse
def _execute_sync(self, process_id: str, data: ProcessExecRequestBody) -> ProcessExecResponse | Any
```

`execute_process` calls the appropriate one after validation (unchanged).

#### Step 2 — Restore API-side cache check in `_execute_async`

`data` (the parsed `ProcessExecRequestBody`) is already available at the call
site in `execute_process`, so derive the cache key without re-parsing:

```python
calculation_task = CalculationTask(inputs=data.inputs, outputs=data.outputs, response=data.response)
cached = temp_result_cache.get(key=calculation_task.celery_key)
if cached:
    task = self.celery_app.send_task("fastprocesses.find_result_in_cache", ...)
    # write job_status, return ProcessExecResponse(status="accepted", ...)
```

On miss: send `raw_body` bytes to Celery directly (same as old `execute_raw`).

#### Step 3 — Keep worker-side cache check unchanged

The worker check in `execute_process` (celery task) stays. It guards against:
- race condition (two identical requests arriving simultaneously)
- cache hits that arrive after the API-side miss window

#### Step 4 — Remove `find_result_in_cache` celery task if it becomes unused

After step 2 the `find_result_in_cache` task is still used (for async cache
hits). Keep it. Re-evaluate after full refactor.

### Files affected

- `src/fastprocesses/api/manager.py` — primary change
- `src/fastprocesses/worker/celery_app.py` — no change expected
- `src/fastprocesses/api/router.py` — no change expected
- Tests referencing strategy classes directly — update mocks/patches

---

## Refactor: Sync Execution Mode for Chord-Based Processes

### Problem

`BaseParallelProcess` and `BaseScatterProcess` dispatch a Celery chord whose
minimum latency is: broker round-trip + queue wait + N subtasks + finalize
callback.  That chain reliably exceeds `FP_SYNC_EXECUTION_TIMEOUT_SECONDS`
(default 10 s) for any non-trivial workload.

When the timeout is hit, `SyncExecutionStrategy` *silently* returns a
`ProcessExecResponse(status="running")` — the client requested sync execution
but receives async behavior with no indication it should poll `/jobs/{id}`.
This is a silent protocol violation, not a user error.

### Decision

Do **not** reject sync requests for chord processes (that removes user choice).
Instead, force-override the execution mode to async at the `execute_process`
call site, where the process instance is already available, and make the
override transparent via a response header.

### Plan

#### Step 1 — Add mode override in `ProcessManager.execute_process`

After validation and before the `execution_mode` branch, insert:

```python
from fastprocesses.core.base_process import BaseParallelProcess, BaseScatterProcess

if isinstance(process, (BaseParallelProcess, BaseScatterProcess)):
    execution_mode = ExecutionMode.ASYNC
    _async_overridden = True
else:
    _async_overridden = False
```

#### Step 2 — Surface the override to the caller

`execute_process` currently returns `ProcessExecResponse | Any`.  Add an
optional out-param or return a thin wrapper so the router can detect that an
override occurred.  Simplest option: add `execution_mode_overridden: bool` to
`ProcessExecResponse` as a non-serialised field (excluded from the OGC JSON
body) and set it when the override fires.

Alternatively — and with zero model changes — the router can detect the
override by checking `isinstance(result, ProcessExecResponse)` with
`result.status == "accepted"` when the original `Prefer` header was
`respond-sync`.  That requires no new fields but is implicit.

#### Step 3 — Emit a response header from the router

In `router.py`, after `execute_process` returns:

```python
if overridden:
    response.headers["X-Execution-Mode"] = "async"
    response.headers["X-Execution-Mode-Override-Reason"] = "parallel-process"
```

This keeps the HTTP contract honest without changing the OGC response body.

#### Step 4 — Remove the chord polling loop from `SyncExecutionStrategy`

Once chord processes never reach the sync path, the `while time.monotonic() <
deadline` busy-wait in `SyncExecutionStrategy` that polls `temp_result_cache`
for `result is None` can be removed.  The sync path becomes: submit task →
`AsyncResult.get(timeout)` → return result.  The `None` branch disappears from
`SyncExecutionStrategy` entirely.

### Files affected

- `src/fastprocesses/api/manager.py` — mode override + polling loop removal
- `src/fastprocesses/api/router.py` — header emission
- Tests for sync execution with parallel/scatter processes — update expectations

---

## Refactor: Eliminate the `None` Sentinel from the Execution Stack

### Problem

`ParallelExecutor` and `ScatterExecutor` return `None` to signal "chord
dispatched; result comes later via finalize callback."  This leaks into every
layer that handles task results:

| Site | `None` handling |
|------|----------------|
| `ExecutorStrategy.execute` docstring | Documents `None` as a valid return |
| `execute_process` Celery task | `if result is None: return None` |
| `CacheResultTask.on_success` | `if retval is None: return` (skips caching) |
| `SyncExecutionStrategy` | Polling loop waiting for finalize to populate cache |
| `manager.py` `_execute_sync` | `deadline` / `time.sleep(0.1)` busy-wait |

`CacheResultTask` is now a no-op for chord processes — caching happens inside
`finalize_parallel` / `finalize_scatter` instead.  Two separate caching paths
must be kept consistent.

### Concept

Replace the implicit `None` signal with an explicit type.  Introduce a small
sentinel object `ChordDispatched` that the executors return instead of `None`.
All `None`-check sites are replaced with `isinstance(result, ChordDispatched)`
checks, making intent explicit and allowing the type checker to catch missing
handling in new code paths.

After the sync-mode override refactor (above) is in place, the only remaining
`ChordDispatched` check site is the Celery `execute_process` task itself —
the API layer no longer needs to know about it.

### Step-by-step plan

#### Step 1 — Define `ChordDispatched` sentinel in `executors.py`

```python
class ChordDispatched:
    """Returned by chord-based executors to signal that a Celery chord was
    dispatched and the result will be stored by the finalize callback."""
    __slots__ = ()

CHORD_DISPATCHED = ChordDispatched()
```

#### Step 2 — Update `ParallelExecutor` and `ScatterExecutor`

```python
class ParallelExecutor(ExecutorStrategy):
    def execute(self, ...) -> BaseModel | ChordDispatched:
        _run_parallel(...)
        return CHORD_DISPATCHED

class ScatterExecutor(ExecutorStrategy):
    def execute(self, ...) -> BaseModel | ChordDispatched:
        _run_scatter(...)
        return CHORD_DISPATCHED
```

Update `ExecutorStrategy.execute` return type annotation accordingly.

#### Step 3 — Update `execute_process` Celery task

Replace:

```python
if result is None:
    return None
```

With:

```python
if isinstance(result, ChordDispatched):
    return None   # still returns None to Celery — chord callback handles the rest
```

This is the only site where `None` continues to flow outward, because the
Celery result backend (used by `AsyncResult.get`) does not know about
`ChordDispatched`.  The `None` is confined to the Celery layer boundary.

#### Step 4 — Update `CacheResultTask.on_success`

Replace:

```python
if retval is None:
    return
```

With:

```python
if retval is None:
    # Chord was dispatched; finalize_* handles caching.
    return
```

No functional change, but add a comment that makes the reason explicit.
Long-term, if Celery ever supports typed return values, this can be tightened.

#### Step 5 — Remove `None` handling from `_execute_sync` / `SyncExecutionStrategy`

After Step 3 of the sync-mode override refactor, chord processes never reach
the sync path.  The `if result is None` polling block in
`SyncExecutionStrategy.execute` is dead code and can be deleted.

#### Step 6 — Audit remaining `None` checks in manager and router

Search for any remaining `if result is None` or `result is not None` patterns
that were load-bearing for the chord path.  Each should either be deleted or
replaced with an `isinstance(result, ChordDispatched)` guard at the
worker-layer boundary.

### Files affected

- `src/fastprocesses/worker/executors.py` — define sentinel, update executors
- `src/fastprocesses/worker/celery_app.py` — update task return + `CacheResultTask`
- `src/fastprocesses/api/manager.py` — remove polling loop (after sync override)
- Tests for parallel/scatter execution — update `None` assertions to `ChordDispatched`
