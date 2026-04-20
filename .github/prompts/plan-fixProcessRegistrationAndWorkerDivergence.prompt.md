# Plan: Fix Silent Registry & Worker Divergence

## Problem

`fastprocesses` uses Redis as the process registry. `@register_process` writes to Redis on module import. Two problems:

**Pitfall 1 — Silent empty registry (the dangerous one)**
The `@register_process` decorator writes into a global mutable singleton inside fastprocesses. This only happens when the module is imported. If the import in `app.py` is ever removed — by a developer cleaning up, by autoflake, by isort, by a pre-commit hook — the API starts normally, returns HTTP 200 on `/processes`, but the list is empty. No error, no warning, no startup failure. This is the worst kind of bug: silent and only discovered at runtime by a user.

The `# noqa: F401` protects against the linter, but not against a human.

**Pitfall 2 — API/Worker registry divergence**
The Celery worker also needs to import the same process modules to know how to execute dispatched tasks. If the worker and the API server diverge (e.g. the worker is started from a different entry point that doesn't trigger the import), the API accepts jobs that the worker silently discards.

**Pitfall 3 — Redis data loss silently empties the registry at runtime**
Redis is used as the *only* store for process registrations. In Kubernetes without persistence enabled (common for Redis-as-broker deployments), any Redis pod restart — OOMKilled, rolling update, eviction — flushes the `process_registry` hash. The already-running API pod never notices: the FastAPI app is healthy, `/health` returns 200, but `GET /processes` returns an empty list and every execution request returns 404. The `@register_process` decorator already fired at class-definition time during the API's startup; re-importing the module is a no-op (`sys.modules` caches it) so the decorator cannot fire again without a process restart.

This is a variant of Pitfall 1 that strikes at runtime, not at startup.

**Root cause:** `fastprocesses` uses the service locator anti-pattern for registration: implicit global state mutated by import side-effects, with no way to query "what is registered" at startup time without already having triggered the registration.

### Does this plan eliminate the service locator anti-pattern?

**Partially.** The plan makes the import side-effects *explicit and verified*, but the underlying mechanism — `@register_process` mutating a global Redis key on import, and `ProcessManager`/worker tasks calling `get_process_registry()` to pull the registry from a global — is **not changed**.

A fuller fix would inject `ProcessRegistry` into `ProcessManager.__init__` as a constructor parameter (the seam already exists; it just unconditionally calls `get_process_registry()` today). Celery task bodies are harder to inject into, but the `get_process_registry()` call is at least greppable and easily audited.

That refactor is considered **out of scope for this plan**: it touches the entire manager/task/worker layer and is a separate concern. The partial mitigation here (explicit module list, fail-fast on empty registry, worker validation) eliminates the *silent failure modes* that actually hurt users, which is the more urgent problem. The structural coupling to the global getter can be addressed as a follow-up.

---

## Decisions

- **Redis is a write-through cache, not the source of truth.** The in-process dict populated by `@register_process` at class-definition time is the authoritative registry. Redis is a durable mirror that can always be rebuilt from the in-memory dict via `resync()`.
- `@register_process` writes to both the in-memory dict and Redis (unchanged externally; the decorator signature is **unchanged**).
- `clear_registry_on_startup=True` is the default when `process_modules` is provided — ensures no zombie entries (deleted/renamed classes) survive restarts.
- Version changes are handled naturally: `hset` overwrites the existing Redis entry on re-registration.
- Empty registry after import → `RuntimeError` (fail fast, never reach uvicorn).
- Worker startup validation is **warn-only** — log `WARNING` for un-importable class paths, but don't crash the worker.
- The `/health/ready` probe detects an empty registry key (data loss) and auto-resyncs before returning ready — no operator action needed.
- No environment-variable-based auto-discovery, no Python entry points — out of scope.

---

## Steps

### Phase 0 — In-memory dict as source of truth in `process_registry.py`

Add a module-level dict `_in_memory_registry: dict[str, dict]` that stores the same data `register_process` currently writes only to Redis. The key is `process_id`; the value is `{"description": ..., "class_path": ...}`.

Update `register_process()` (the decorator) to write into `_in_memory_registry` *in addition to* Redis. This is the only change to the decorator — its signature and external behaviour are unchanged.

Add two methods on `ProcessRegistry`:

- `resync() -> list[str]` — writes every entry in `_in_memory_registry` into Redis (`hset`), then returns the list of process IDs. Idempotent: safe to call repeatedly. This is the recovery path for Redis data loss.
- `clear()` — deletes the `process_registry` Redis hash key (used by the startup clean-slate logic). Does **not** touch `_in_memory_registry`.

### Phase 1 — Shared utility in `process_registry.py`

Add `load_process_modules(module_paths: list[str]) -> list[str]` as a module-level function.

- Uses `importlib.import_module` for each path — raises `ImportError` loudly on a bad module path.
- After importing all modules (which populates `_in_memory_registry` via `@register_process`), calls `resync()` to write/update Redis.
- Returns the list of process IDs now in the registry.
- Reusable by both the API server and (optionally) the worker.

### Phase 2 — Explicit startup in `server.py`

Add two new parameters to `OGCProcessesAPI.__init__`:

```python
process_modules: list[str] | None = None
clear_registry_on_startup: bool = True
```

When `process_modules` is provided:

1. If `clear_registry_on_startup=True`, call `process_registry.clear()` before importing (removes stale/zombie entries from Redis; `_in_memory_registry` is unaffected because it is always empty at a fresh process start).
2. Call `load_process_modules(process_modules)` — each listed module is imported, populating `_in_memory_registry` via `@register_process`, then `resync()` writes them to Redis.
3. After import, if `get_process_ids()` returns an empty list, raise `RuntimeError` (fail fast).
4. Log the list of registered process IDs at `INFO` level for startup visibility.

The resulting `app.py` pattern (outside the project package) becomes:

```python
import uvicorn
from fastprocesses.api.server import OGCProcessesAPI

app = OGCProcessesAPI(
    process_modules=["myproject.processes.my_process"],
    contact={
        "name": "LGV Hamburg",
        "url": "https://example.com",
        "email": "support@support.com",
    },
    license={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0",
    },
    terms_of_service="https://example.com/terms",
).get_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None, log_level="DEBUG")
```

The `@register_process` decorators stay inside the project package (`myproject/processes/my_process.py`). The `app.py` entry point lists the module paths explicitly — making the dependency visible, greppable, and immune to linter cleanup.

### Phase 3 — `/health/ready` auto-resync and worker startup validation

**Router (`router.py`) — extend the `/health/ready` probe:**

1. Ping Redis (existing check — unchanged).
2. Check `process_registry.get_process_ids()`. If the list is **empty** and `_in_memory_registry` is non-empty (i.e., modules were loaded but Redis lost the data), call `process_registry.resync()` and log a `WARNING`: `"Process registry was empty in Redis; resynced from in-memory registry"`.
3. If the list is still empty after resync (genuinely no processes registered), return HTTP 503 with `reason: no_processes_registered`.
4. Otherwise return `{"status": "ready"}`.

This means a Redis restart is *self-healing* within the next readiness probe cycle, with no operator action required.

**`common.py` — extend the `worker_ready_handler` Celery signal handler:**

1. Fetch all entries from the `process_registry` Redis hash.
2. For each entry, parse the `class_path` field.
3. Call `pydoc.locate(class_path)` — if it returns `None`, the class is not importable on this worker.
4. Log a `WARNING` for every broken path: `"Process '{id}' class path '{path}' is not importable on this worker"`.
5. Worker still starts — warnings are visible in logs before any job is dispatched.

This catches API/worker divergence (Pitfall 2) before a job is accepted and silently discarded.

---

## Files to Change

| File | Change |
|------|--------|
| `src/fastprocesses/processes/process_registry.py` | Add `_in_memory_registry` dict; update `register_process` to write to it; add `resync()`, `clear()`, `load_process_modules()` |
| `src/fastprocesses/api/server.py` | Add `process_modules`, `clear_registry_on_startup` params; startup import + validation |
| `src/fastprocesses/api/router.py` | Extend `/health/ready` to detect empty Redis registry and call `resync()` |
| `src/fastprocesses/common.py` | Extend `worker_ready_handler` to validate class paths |
| `examples/run_example.py` | Update to use new `process_modules` parameter |

---

## Verification Checklist

- [ ] `pytest tests/` — existing tests pass, no regressions.
- [ ] `OGCProcessesAPI(process_modules=["nonexistent.module"])` → `ImportError` before app starts.
- [ ] `OGCProcessesAPI(process_modules=["mymod"])` where `mymod` has no `@register_process` → `RuntimeError`.
- [ ] Normal startup → INFO log lists all registered process IDs.
- [ ] Startup with stale Redis key (zombie process) + `clear_registry_on_startup=True` → zombie gone after restart.
- [ ] Redis data loss at runtime → next `/health/ready` call detects empty registry, calls `resync()`, returns `{"status": "ready"}`, WARNING in logs; `GET /processes` returns the full list again.
- [ ] Redis data loss at runtime with no `_in_memory_registry` entries (e.g., legacy mode, no `process_modules`) → `/health/ready` returns HTTP 503 with `reason: no_processes_registered`.
- [ ] Worker start with a stale/broken `class_path` in Redis → `WARNING` logged, worker still starts.
- [ ] `GET /processes` returns the correct process list.
- [ ] `OGCProcessesAPI()` with no `process_modules` (legacy mode) → behaves as before, no regression.
