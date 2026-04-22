# Plan: API Federation & Process Versioning

> **Prerequisite:** This plan builds on the completed work from *"Fix Silent Registry & Worker Divergence"* (`plan-fixProcessRegistrationAndWorkerDivergence.prompt.md`). It assumes `_in_memory_registry`, `resync()`, `load_process_modules()`, and the `/health/ready` auto-resync are already in place.

---

## Goal

Enable a **federated deployment** where:

1. A **centralized API gateway** (no process code installed) lists all processes, validates inputs, accepts execution requests, and serves job status/results.
2. **Multiple independent worker Helm charts** each register their own processes and subscribe to their own Celery queues.
3. **Concurrent process versions** can coexist — deploying v2 does not force retirement of v1.
4. Adding a new project or process version requires deploying a new Helm chart only — zero changes to the API gateway chart.

---

## Current Blockers

### Blocker 1 — API-side `pydoc.locate` requires the class to be installed locally

`ProcessRegistry.get_process()` calls `pydoc.locate(class_path)` to instantiate the class. This is used in two API-side code paths:

- **`ProcessManager.get_process_description()`** — serves `GET /processes/{process_id}`. Instantiates the class just to call `.get_description()`.
- **`ProcessManager.execute_process()`** — validates inputs/outputs by instantiating the class, calling `service.quick_validate_inputs()` and `service.validate_outputs()`.

If the API gateway pod doesn't have the class installed, both paths raise `ProcessClassNotFoundError`. The process description and JSON schema are already serialised in the Redis registry entry — they just aren't used on the API side today.

### Blocker 2 — Registry key is `process_id` only (no version dimension)

The Redis hash field is `process_id` (e.g., `simple_process`). Registering v2 with the same ID overwrites v1. Two versions of the same process cannot coexist.

### Blocker 3 — `send_task` dispatches to the default queue

All `send_task` calls use the default Celery queue. There is no mechanism to route execution to a specific worker group. In a federated deployment, the wrong worker could pick up a task it cannot execute.

### Blocker 4 — `clear()` deletes the entire registry hash

The current `clear()` approach (`redis.delete(self.registry_key)`) wipes all pods' entries. In a federated deployment, Pod A restarting wipes Pod B's registrations.

---

## Decisions

- **Registry key becomes `process_id:version`** — composite key enables version coexistence.
- **Description and schema served from Redis** — the API gateway never needs to import process classes. `pydoc.locate` is only used on worker pods.
- **Each process version gets its own Celery queue** — named `fp:{process_id}:{version}`. Workers subscribe only to their queues.
- **`clear()` scoped to own keys** — only `hdel` the entries in `_in_memory_registry`, not the entire hash.
- **`GET /processes/{process_id}` returns the latest version** by semver. A `version` query param allows requesting a specific version. `GET /processes` lists all versions.
- **`POST /processes/{process_id}/execution` targets latest by default** — a `version` field in the request body (or query param) overrides.
- **`@register_process` decorator gains an optional `queue` parameter** — defaults to `fp:{process_id}:{version}`.
- **Backwards-compatible** — single-deployment users (no federation) see no behavior change. The version is always present (already required by `ProcessSummary`), the queue defaults are invisible.

---

## Steps

### Phase 1 — Versioned registry key

**`process_registry.py`**

1. Change the Redis hash field from `process_id` to `process_id:version`. The version is read from `process.get_description().version`.

2. Update `_in_memory_registry` key to match: `process_id:version`.

3. Add a `queue` field to the stored registry entry alongside `description` and `class_path`:
   ```python
   process_data = {
       "description": description_dict,
       "class_path": f"{process.__module__}.{process.__class__.__name__}",
       "queue": queue or f"fp:{process_id}:{description.version}",
   }
   ```

4. Update `register_process` decorator to accept optional `queue`:
   ```python
   @register_process("simple_process", queue="custom-queue")  # optional
   class SimpleProcess(BaseProcess): ...
   ```

5. Update `get_process_ids()`:
   - Returns composite keys (`process_id:version`) from Redis.
   - Add `get_unique_process_ids() -> list[str]` that returns deduplicated process IDs (without version suffix).

6. Update `has_process(process_id)`:
   - Check both `process_id:*` patterns. Accept `process_id` alone (latest) or `process_id:version` (exact).

7. Update `get_process(process_id, version=None)`:
   - If `version` is given, look up `process_id:version` exactly.
   - If `version` is `None`, find the latest semver entry for `process_id`.

8. Scoped `clear()`:
   ```python
   def clear(self):
       if _in_memory_registry:
           self.redis_connection._execute_redis_command(
               'hdel', self.registry_key, *_in_memory_registry.keys()
           )
   ```

9. Update `resync()` to use composite keys.

### Phase 2 — Description-from-Redis on the API side

**`process_registry.py`**

1. Add `get_process_description(process_id, version=None) -> ProcessDescription`:
   - Reads the `description` field from the Redis hash entry directly.
   - Parses it with `ProcessDescription.model_validate(...)`.
   - Does **not** call `pydoc.locate`. Does **not** require the class.

2. Add `get_process_schema(process_id, version=None) -> dict`:
   - Returns the input/output schema from the stored description.
   - Used for input validation without class instantiation.

**`manager.py`**

3. Refactor `get_process_description()`:
   - Replace `self.process_registry.get_process(process_id)` → `self.process_registry.get_process_description(process_id)`.
   - No more class instantiation on the API side for description serving.

4. Refactor input/output validation in `execute_process()`:
   - Replace `service.quick_validate_inputs(data.inputs)` with schema-based validation using the stored JSON schema.
   - Replace `service.validate_outputs(data.outputs)` similarly.
   - The validation logic can be extracted to a `ProcessRegistry` method or a standalone utility that works from the schema dict.

5. **`get_process()` (pydoc.locate path) is retained** but only called on **worker pods**. The API gateway never calls it.

### Phase 3 — Queue-routed task dispatch

**`manager.py`**

1. In `AsyncExecutionStrategy.execute()` and `SyncExecutionStrategy.execute()`, read the target queue from the registry entry:
   ```python
   queue = self.process_manager.process_registry.get_process_queue(process_id, version)
   task = self.process_manager.celery_app.send_task(
       "fastprocesses.execute_process",
       args=[process_id, serialized_data],
       queue=queue,
   )
   ```

2. Same for cache retrieval tasks (`find_result_in_cache`) — these can stay on the default queue since they don't need process classes.

**`common.py`**

3. Worker subscribes to queues matching its `_in_memory_registry`:
   ```python
   @worker_ready.connect
   def worker_ready_handler(sender, **kwargs):
       queues = [entry["queue"] for entry in _in_memory_registry.values()]
       # Log subscribed queues
       logger.info(f"Worker subscribed to queues: {queues}")
   ```

4. Celery config update — the worker command uses `-Q` to subscribe:
   ```
   celery -A myproject.celery_worker worker -Q fp:simple_process:1.0.0,fp:simple_process:2.0.0
   ```
   Or programmatically via `celery_app.conf.task_queues`.

### Phase 4 — Version selection in the API

**`router.py`**

1. `GET /processes` — returns all versions of all processes. The response already uses `ProcessSummary` which includes `id` and `version`.

2. `GET /processes/{process_id}` — add optional `version` query param:
   ```python
   @router.get("/processes/{process_id}")
   async def describe_process(process_id: str, version: str | None = None):
   ```
   - `version=None` → return latest by semver.
   - `version="1.0.0"` → return exact version.

3. `POST /processes/{process_id}/execution` — add optional `version` query param (or read from request body):
   ```python
   @router.post("/processes/{process_id}/execution")
   async def execute_process(process_id: str, ..., version: str | None = None):
   ```
   - `version=None` → dispatch to latest.
   - `version="1.0.0"` → dispatch to v1 queue.

**`models.py`**

4. Add `version` field to `ProcessExecRequestBody` (optional, `None` → latest):
   ```python
   class ProcessExecRequestBody(BaseModel):
       inputs: Dict[str, Any]
       outputs: dict[str, dict[str, OutputControl]] | None = None
       mode: Optional[ExecutionMode] = ExecutionMode.ASYNC
       response: ResponseType = ResponseType.RAW
       version: str | None = None
   ```

5. Add `version` field to `JobStatusInfo` so job results can be traced back to the exact process version:
   ```python
   class JobStatusInfo(BaseModel):
       ...
       processVersion: Optional[str] = None
   ```

### Phase 5 — Worker self-registration entry point pattern

**Documentation / examples**

Each project's worker entry point follows this pattern:

```python
# myproject/celery_worker.py
import myproject.processes.my_process  # triggers @register_process

from fastprocesses.common import celery_app  # re-export for celery -A
```

```bash
celery -A myproject.celery_worker worker \
    -Q fp:my_process:1.0.0 \
    --loglevel=info
```

The Helm chart's worker Deployment uses this command. The centralized API chart uses:
```python
app = OGCProcessesAPI(
    # No process_modules — purely a gateway
    contact={...},
).get_app()
```

---

## Helm Chart Topology

```
shared-redis/                     # One Redis, shared by all
  └── Celery broker + result backend + process registry + job/result cache

fastprocesses-api/                # Centralized API gateway Helm chart
  └── process_modules=None
  └── clear_registry_on_startup=False
  └── Reads registry from Redis, serves descriptions from Redis
  └── Dispatches to versioned queues
  └── /health/ready returns 503 until workers populate Redis

myproject-v1/                     # Project A Helm chart
  └── celery -A myproject.celery_worker worker -Q fp:my_process:1.0.0
  └── On startup: imports modules → registers to Redis
  └── KEDA scales on queue depth for fp:my_process:1.0.0

myproject-v2/                     # Updated version Helm chart (parallel deployment)
  └── celery -A myproject.celery_worker worker -Q fp:my_process:2.0.0
  └── On startup: registers to Redis alongside v1
  └── Clients using version=2.0.0 get routed here

other-project/                    # Separate project Helm chart
  └── celery -A otherproject.celery_worker worker -Q fp:geo_analysis:1.0.0
  └── Completely independent, registers its own processes
```

Adding a new project: `helm install other-project ./chart` → worker starts → registers to Redis → API immediately sees new processes. No API chart changes.

Retiring v1: `helm uninstall myproject-v1` → worker stops → v1 entries remain in Redis until TTL or explicit deregistration. Optionally, worker shutdown signal handler calls `hdel` for its own entries.

---

## Files to Change

| File | Change |
|------|--------|
| `src/fastprocesses/processes/process_registry.py` | Composite key `process_id:version`; `queue` field in registry entry; `get_process_description()` from Redis; `get_process_schema()`; scoped `clear()`; version-aware `get_process()` and `has_process()` |
| `src/fastprocesses/api/manager.py` | Description from registry (not class instantiation); schema-based validation; queue-routed `send_task`; version param threading |
| `src/fastprocesses/api/router.py` | `version` query param on `GET /processes/{id}` and `POST .../execution` |
| `src/fastprocesses/core/models.py` | `version` field on `ProcessExecRequestBody` and `JobStatusInfo` |
| `src/fastprocesses/common.py` | Worker queue subscription from `_in_memory_registry` |
| `src/fastprocesses/worker/celery_app.py` | Worker tasks pass version through; `get_process` calls use version |

---

## Migration & Backwards Compatibility

- **Existing single-deployment users** — no breaking changes. Version is already required in `ProcessSummary`. The composite key is transparent; `GET /processes/simple_process` still returns the (only) version. Queue defaults to `fp:{id}:{version}` but the default queue also works if `-Q` is not specified.
- **Existing Redis data** — a migration utility reads old `process_id` keys, extracts the version from the stored description, and re-writes them as `process_id:version`. Run once at upgrade time.
- **Old workers + new API** — old workers ignore the `queue` kwarg (tasks still land on default queue). Not ideal but functional. Log a deprecation warning.

---

## Out of Scope (Future Work)

- **Process deregistration on shutdown** — worker shutdown handler calling `hdel` for its own entries. Not required for correctness (stale entries don't break anything; they just return `ProcessClassNotFoundError` if someone tries to execute them).
- **TTL on registry entries** — auto-expiring entries for workers that disappeared without deregistering. Could use Redis key expiry on a per-entry basis.
- **OGC API version negotiation** — the OGC API Processes 1.0 spec has no standard version negotiation in URLs. The `version` query param is an extension. A future OGC spec revision may standardize this.
- **Dynamic code delivery** — hot-loading new process code onto running pods without restart. Requires shared volumes or plugin installation mechanisms.
- **Per-version result caching** — currently the cache key is based on inputs/outputs only. Two versions of the same process with identical inputs would share a cache entry. This may or may not be desirable.

---

## Verification Checklist

- [ ] `pytest tests/` — existing tests pass, no regressions.
- [ ] Registry entry uses `process_id:version` composite key.
- [ ] `GET /processes` returns all versions of all processes.
- [ ] `GET /processes/{id}` returns latest version by default.
- [ ] `GET /processes/{id}?version=1.0.0` returns exact version.
- [ ] `POST /processes/{id}/execution` dispatches to latest version queue.
- [ ] `POST /processes/{id}/execution` with `version=1.0.0` dispatches to v1 queue.
- [ ] API gateway pod with no process code: `GET /processes` works, execution works via queue routing.
- [ ] Worker pod registers its own processes on startup.
- [ ] Worker pod subscribes only to its own queues.
- [ ] Pod A restart does not wipe Pod B's registry entries (scoped `clear()`).
- [ ] Two versions of the same process coexist in the registry.
- [ ] `GET /jobs/{id}` works from any pod (shared Redis).
- [ ] Retiring a Helm chart removes workers; registry entries become stale but don't break the API.
- [ ] Backwards compatibility: single-deployment user sees no behavior change.
