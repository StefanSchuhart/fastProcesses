# Plan: Result Cache Hardening & Duplicate-Write Elimination

## Background

Two production incidents surfaced problems in how `fastprocesses` uses Redis/DragonflyDB for job results:

1. **Crash loop at startup** (resolved separately, see `plan-fixProcessRegistrationAndWorkerDivergence.prompt.md`): `register_process` re-raised `ResponseError: Out of memory`, killing uvicorn workers in a loop.
2. **Large-result read crash**: DragonflyDB neared OOM writing a large `BaseParallelProcess` result, then a worker/API process crashed while reading that same result back via `/jobs/{id}/results`, causing an upstream 504 at the federating "UMP" proxy.

Root cause of #2: `finalize_parallel`/`finalize_scatter` write the **same large payload twice** to the same Redis instance (once keyed by `celery_key` for dedup, once keyed by `job_id` for result retrieval), and `TempResultCache.get()` amplifies memory ~3-6x during deserialization (bytes → str → dict) with no size guard.

This plan consolidates all fixes discussed in response to these incidents into one sequenced plan. No code has been written yet — this is planning only.

---

## Decisions

- **`job_id` and `celery_key` remain distinct identifiers.** Confirmed via `_check_cache`/`_get_cached_result` in `manager.py`: every request — even a cache hit — mints a fresh Celery task id via `send_task("fastprocesses.find_result_in_cache", ...)`. `job_id` is per-request identity (OGC API Processes semantics: dismissal, status, polling); `celery_key` is per-computation identity (content hash, shared across identical requests). Collapsing them would let one user's `DELETE /jobs/{id}` or status polling affect another user's unrelated request, and would race two concurrent identical submissions onto the same Celery task id. **Do not unify them.**
- Instead, eliminate the duplicate **payload** write via a pointer/indirection, reusing the claim-check pattern already used in `chord_tasks.py` for subtask payloads: store the actual result once under `celery_key`, and store only a tiny pointer under `job_id`.
- `CalculationTask._hash_dict()` must include `process_id` in the hash. Today it hashes only `inputs` + normalized `outputs`, so two *different* processes with structurally identical inputs/outputs shapes can collide on the same `celery_key` — a real cross-process cache-poisoning bug, independent of the pointer work but must land before/alongside it (a collision would otherwise corrupt the canonical single-copy result).
- `FP_MAX_RESULT_SIZE_BYTES` is an opt-in safeguard (default: disabled / `None`) — existing deployments should not have a new failure mode appear without opt-in.
- `TempResultCache.get()` must pre-check size (`STRLEN`) before decode+parse, to avoid ever materializing an oversized value in memory.
- `ProcessRegistry`'s default Redis connection (`settings.results_cache.connection` vs. `settings.celery_broker.connection`) — proposed switch agreed as architecturally cleaner in discussion, but **no go-ahead given yet**. Tracked here as an open decision, not committed to this plan's scope.

---

## Steps

### Phase 1 — Fix `celery_key` hash to include `process_id`

In `src/fastprocesses/core/models.py`, update `CalculationTask._hash_dict()` to include `process_id` alongside `inputs`/normalized `outputs` in the hashed payload. This is a pure bugfix with no external signature change (`celery_key` is still a `computed_field` property). Must land before/with Phase 2, since Phase 2 makes `celery_key` the sole canonical storage location for large payloads — a hash collision there is worse than in today's dedup-only usage.

**Verify:** existing tests in `tests/test_output_format_resolution.py` / cache-related tests still pass; add a test asserting two different `process_id`s with identical inputs/outputs produce different `celery_key`s.

### Phase 2 — Eliminate duplicate large-payload writes via pointer indirection

Reuse the existing claim-check marker convention (`{"__claim_check__": rkey}` in `chord_tasks.py`) with a new, distinctly-named marker `{"__result_ref__": celery_key}` to avoid ambiguity with the chord claim-check keyspace.

- `finalize_parallel` / `finalize_scatter` (`chord_tasks.py`): keep `temp_result_cache.put(key=calculation_task.celery_key, value=merged)` (canonical, single copy). Replace the second `temp_result_cache.put(key=job_id, value=merged)` with `temp_result_cache.put(key=job_id, value={"__result_ref__": calculation_task.celery_key})`.
- `CacheResultTask.on_success` (`celery_app.py`): standard `BaseProcess` results already write only to `temp_result_cache` keyed by `celery_key`; Celery's own result backend separately stores `retval` keyed by `job_id` (a different Redis instance — `celery_result`, not `results_cache`). Confirm whether this cross-instance duplication is in scope here or acceptable as-is (different backend, smaller blast radius) — **needs explicit decision**, see Open Questions.
- `manager.py::get_job_result` (and any other reader of `temp_result_cache` by `job_id`): add one-hop pointer resolution — if the fetched value is a dict with `"__result_ref__"`, re-fetch using that key to get the actual payload.
- Ensure the current non-re-raising `try/except Exception as cache_err: logger.error(...)` around these `put()` calls in `finalize_parallel`/`finalize_scatter` still allows `ResultTooLargeError` (Phase 3) to propagate and mark the job `FAILED`, rather than being swallowed into a false `SUCCESSFUL`.

**Verify:** a parallel/scatter process execution stores the result exactly once in `results_cache` Redis (spot-check via `redis-cli` key count / `MEMORY USAGE`); `GET /jobs/{id}/results` still returns the correct payload via the pointer.

### Phase 3 — `FP_MAX_RESULT_SIZE_BYTES` safeguard

- Add `FP_MAX_RESULT_SIZE_BYTES: int | None = None` to `OGCProcessesSettings` (`config.py`), default `None` (disabled).
- Add `ResultTooLargeError` to `core/exceptions.py`.
- `TempResultCache.put()` (`cache.py`): if configured, check serialized size before writing; raise `ResultTooLargeError` if exceeded.
- `TempResultCache.get()` (`cache.py`): perform `STRLEN` (or equivalent) before `GET`, to avoid decoding+parsing a value that's already known to be oversized — this closes the specific memory-amplification path that caused incident #2, independent of whether the limit is configured (a hardcoded sane ceiling, e.g. protect against multi-GB reads, may be worth applying unconditionally — see Open Questions).
- `execute_process` (`celery_app.py`) and `finalize_parallel`/`finalize_scatter` (`chord_tasks.py`): let `ResultTooLargeError` propagate to the outer exception handler so the job is marked `JobStatusCode.FAILED` with a clear message, instead of silently "succeeding" with no cached result.

**Concrete root cause (confirmed via DragonflyDB log)**: a `Pipeline buffer over limit` log with `parsed commands queue size: 1` means a *single* `SETEX` command carried a ~300 MB value — this trips Dragonfly's `--pipeline_buffer_limit` (default 128 MiB, shared per IO thread across all connections on that thread) on its own, before any pipelining/batching is involved. `--request_cache_limit` (default 64 MiB/thread) is at similar risk. Combined with the ~3-6x bytes→str→dict amplification during `TempResultCache.get()`, a ~50 MB stored value can transiently balloon past 128-300 MB while being read back.

- **Default `FP_MAX_RESULT_SIZE_BYTES = 10_485_760` (10 MiB)**, not disabled — a result this large is never reasonable for a synchronous cache entry; treat it as catching a real bug (unbounded/unpaginated process output), not just a tunable. Deployments with legitimately larger results can raise it, but should stay well under ~20-25 MiB to leave headroom below Dragonfly's 128 MiB per-thread pipeline budget when multiple large results could land on the same IO thread concurrently.
- The unconditional `STRLEN`-before-`GET` guard should use a hardcoded ceiling (e.g. ~50 MiB) independent of `FP_MAX_RESULT_SIZE_BYTES`, since it protects the read-side amplification path regardless of configuration.

**Storage/serialization efficiency (reduces both the odds of hitting the above limits and the cost when near them)**:
- `TempResultCache.put()`/`get()` (`cache.py`): switch from `json.dumps/loads` (text) to `zlib.compress(orjson.dumps(...))` / `orjson.loads(zlib.decompress(...))` (binary, compressed) — done. JSON/geodata payloads typically compress 5-10x, shrinking both the Dragonfly wire size (directly reducing `pipeline_buffer_limit` pressure) and reducing the number of intermediate copies made during encode/decode (`orjson.dumps` returns `bytes` directly, skipping the extra `str` allocation `json.dumps` requires; decompressing straight to `bytes` then `orjson.loads(bytes)` skips the redundant `bytes→str` hop before parsing).
- **Celery's own result backend needs the same treatment** — it uses the `custom_json` kombu serializer registered in `common.py` (shared by `task_serializer`/`result_serializer`), not `TempResultCache`. Updated `custom_json_serializer`/`custom_json_deserializer` (`common.py`) to the same `zlib.compress(orjson.dumps(...))` / `orjson.loads(zlib.decompress(...))` scheme, and changed `content_encoding` from `"utf-8"` to `"binary"` since the payload is now compressed bytes, not text. This benefits both the broker (task args) and the result backend uniformly, since both use the same registered serializer — done.
- `msgpack`/binary numeric encoding (deferred) — a larger, separate change (versioned wire format, touches `outputs_handler.py`/serializers) only worth pursuing if numeric-array-heavy results turn out to dominate payload size; not pursued now.

**Verify:** a synthetic oversized result raises `ResultTooLargeError` and the job status ends as `FAILED` with a descriptive message, both for `BaseProcess` and `BaseParallelProcess` paths. Confirm `orjson`/`zlib` round-trip correctly for `TempResultCache` and for Celery's `custom_json` serializer (task args and result backend).


### Phase 4 — Small bugfixes (independent, low-risk) (DONE)

- `config.py`: verify current `FP_CELERY_RESULT_DB` default against the incident-era assumption; document the resolution (already fixed vs. needs fixing) in this plan or inline comment.

### Phase 5 (deferred / not yet decided) — Storage architecture

Already documented in `plan-apiFederationAndVersioning.prompt.md`'s "Storage Architecture" section (DragonflyDB tiered storage vs. object-storage claim-check extension). Not duplicated here; cross-referenced only.

### Phase 6 — `ProcessRegistry` Redis connection switch (DONE)

Switched `ProcessRegistry`'s default connection from `settings.results_cache.connection` to `settings.celery_broker.connection` so registry lookups aren't coupled to results-cache memory pressure. Approved and implemented in `src/fastprocesses/processes/process_registry.py`.


---

## Open Questions (need answers before implementing Phases 2 & 3)

1. **Phase 2 scope**: should the `BaseProcess` / `CacheResultTask.on_success` cross-instance duplication (Celery `celery_result` backend vs. `temp_result_cache`) be addressed in this pass, or is it acceptable as-is since it spans two different, independently-sized Redis instances (smaller blast radius than the same-instance `finalize_parallel` duplication)?
2. **Phase 3 scope**: should `FP_MAX_RESULT_SIZE_BYTES` apply only to `temp_result_cache`, or also to `job_status_cache` (which stores smaller, bounded metadata today but could theoretically grow via `message` fields)?
3. **Phase 3 unconditional guard**: should the `STRLEN`-before-`GET` check in `TempResultCache.get()` be applied unconditionally (independent of whether `FP_MAX_RESULT_SIZE_BYTES` is configured), acting as a hardcoded sanity ceiling, given that this is the exact mechanism that caused incident #2?
4. Should Phase 1 (hash fix) be shipped as a standalone patch release before Phase 2, given it silently invalidates all currently-cached entries (existing `celery_key`s change) — is a cache-flush-on-deploy acceptable, or does this need a migration note in `CHANGELOG.md`?

---

## Files to Change

- `src/fastprocesses/core/models.py` — Phase 1 (`_hash_dict`)
- `src/fastprocesses/worker/chord_tasks.py` — Phase 2 (`finalize_parallel`, `finalize_scatter`)
- `src/fastprocesses/worker/celery_app.py` — Phase 2 (decision-dependent), Phase 3 (propagate `ResultTooLargeError`)
- `src/fastprocesses/api/manager.py` — Phase 2 (pointer resolution in `get_job_result`)
- `src/fastprocesses/core/cache.py` — Phase 3 (`TempResultCache.put`/`get`, size guard + orjson/zlib)
- `src/fastprocesses/core/exceptions.py` — Phase 3 (`ResultTooLargeError`)
- `src/fastprocesses/core/config.py` — Phase 3 (`FP_MAX_RESULT_SIZE_BYTES`), Phase 4 (`FP_CELERY_RESULT_DB` verification)
- `src/fastprocesses/common.py` — Phase 3 (`custom_json` kombu serializer: orjson/zlib), Phase 4 (`result_expires` typo)
- `pyproject.toml` — Phase 3 (`orjson` dependency)
- `src/fastprocesses/processes/process_registry.py` — Phase 6 (default Redis connection)
- `CHANGELOG.md` — note the `celery_key` hash change (cache invalidation) if Phase 1 ships to a published version
