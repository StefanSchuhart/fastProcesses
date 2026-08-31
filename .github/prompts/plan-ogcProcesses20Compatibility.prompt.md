# Plan: OGC API Processes 2.0 Compatibility

## Goal

Add OGC API Processes 2.0 support without breaking existing 1.0 clients or existing process implementations.

The compatibility rule is:

- The **core** stores canonical, version-neutral process/job/result state.
- The **adapters** translate between OGC API Processes 1.0 and 2.0 wire contracts.
- Existing process classes and YAML descriptions remain valid until an explicit major-version cleanup removes 1.0 compatibility.

The first useful feature to implement is **2.0 output/result semantics**: selected outputs, omitted-vs-empty outputs, individual result resources, multi-valued result indexing, and profile-aware `results.yaml` / `values.yaml` responses. This is the smallest feature that touches the real compatibility problem in fastprocesses today: the code is currently centered on 1.0 `response=raw|document`, `transmissionMode`, and `outputTransmission`.

---

## Current 1.0 Anchors

The existing implementation is 1.0-shaped in these places:

- `src/fastprocesses/core/models.py`
  - `ProcessSummary.outputTransmission`
  - `ProcessDescription.outputTransmission`
  - `OutputControl.transmissionMode`
  - `ProcessExecRequestBody.response`
  - `JobStatusInfo.jobID` / `type` fields
- `src/fastprocesses/core/outputs_handler.py`
  - serializes based on `response_mode` (`raw` vs `document`)
  - serializes by reference based on per-output `transmissionMode`
- `src/fastprocesses/core/output_schema_resolver.py`
  - treats missing and empty `outputs` too similarly
  - forwards `transmissionMode` as a resolved output concern
- `src/fastprocesses/api/router.py`
  - advertises 1.0 conformance URIs
  - defaults execution to async unless `Prefer: respond-sync` is present
  - has no `/jobs/{jobID}/results/{outputID}` or `/jobs/{jobID}/results/{outputID}/{N}` routes
- `src/fastprocesses/api/manager.py` and `src/fastprocesses/worker/job_status.py`
  - create and persist 1.0-style job status documents

---

## Compatibility Architecture

### 1. Keep a canonical core model that is not the public wire schema

Do not make the internal execution pipeline switch between 1.0 and 2.0 models. Instead, introduce canonical internal structures that express the behavior fastprocesses actually needs:

```python
class CanonicalExecuteRequest(BaseModel):
    inputs: dict[str, Any]
    outputs: OutputSelection
    process_id: str
    process_version: str | None = None
    requested_protocol: Literal["ogcapi-processes-1.0", "ogcapi-processes-2.0"]
    legacy_response_mode: Literal["raw", "document"] | None = None
```

```python
class OutputSelection(BaseModel):
    state: Literal["omitted", "empty", "selected"]
    outputs: dict[str, CanonicalOutputRequest]
```

```python
class CanonicalOutputRequest(BaseModel):
    output_id: str
    format: dict[str, Any] | None = None
    legacy_transmission_mode: Literal["value", "reference"] | None = None
```

Rules:

- `outputs` omitted by the client becomes `state="omitted"` and means all declared outputs.
- `outputs: {}` becomes `state="empty"` and means execute without generated response/retrievable outputs.
- `outputs` with keys becomes `state="selected"` and means only those outputs.
- 1.0-only fields such as `response` and `transmissionMode` are preserved as compatibility hints, not as the core representation.

### 2. Use protocol adapters at the boundary

Add a small adapter layer, for example:

```text
src/fastprocesses/api/adapters/
  __init__.py
  processes_v1.py
  processes_v2.py
```

Each adapter owns request parsing and response rendering for one protocol version.

`processes_v1.py` responsibilities:

- Accept current 1.0 execute bodies with `response` and `transmissionMode`.
- Convert them into `CanonicalExecuteRequest`.
- Render current 1.0-compatible responses so existing clients do not break.
- Emit 1.0 conformance URIs where a 1.0 representation is requested.

`processes_v2.py` responsibilities:

- Accept 2.0 execute bodies without requiring `response` / `transmissionMode`.
- Preserve omitted-vs-empty `outputs` exactly.
- Render 2.0 result documents and profile links.
- Emit 2.0 conformance URIs only for implemented classes.

The router should select an adapter, then call shared manager/core code.

### 3. Select protocol version explicitly, with a conservative default

Use a simple negotiation rule first; do not infer too much.

Supported selectors, in priority order:

1. URL query parameter: `?spec=2.0` or `?spec=1.0`
2. Profile query/header when later implemented
3. Default server setting, initially `1.0` for backward compatibility

Example route pattern:

```python
adapter = get_processes_adapter(spec or settings.FP_OGC_PROCESSES_DEFAULT_SPEC)
canonical_request = adapter.parse_execute_request(raw_body, process_description)
result = process_manager.execute_process(process_id, canonical_request, execution_mode)
return adapter.render_execute_response(result, ...)
```

Do not add duplicate `/v1` and `/v2` route trees unless a deployment needs hard URL separation. Query/profile negotiation keeps the public OGC paths stable.

### 4. Keep process descriptions backward-compatible

Existing descriptions can keep 1.0 fields:

```yaml
outputTransmission:
  - value
  - reference
```

Internal parsing should accept those fields but the 2.0 adapter should not emit them by default.

Add 2.0 optional fields gradually:

- `dataClasses`
- `dataAccessAPIs`
- `valuePassing`
- `executionUnitRequirements`

Compatibility policy:

- `ProcessDescription` accepts both 1.0 and 2.0 description shapes.
- `ProcessDescriptionV1Adapter.render(...)` emits 1.0 shape, including `outputTransmission`.
- `ProcessDescriptionV2Adapter.render(...)` emits 2.0 shape, excluding `outputTransmission` and including 2.0 fields when present.
- Process authors are not forced to rewrite YAML before the API can advertise a 2.0 Core subset.

### 5. Store job status canonically, render per version

Introduce an internal job status that contains both old and new names conceptually:

```python
class CanonicalJobStatus(BaseModel):
    id: str
    process_id: str | None = None
    process_version: str | None = None
    status: JobStatusCode
    created: datetime | None = None
    started: datetime | None = None
    finished: datetime | None = None
    updated: datetime | None = None
    progress: int | None = None
    message: str | None = None
    links: list[Link] = []
```

Rendering rules:

- 1.0 response uses `jobID` and `type: "process"`.
- 2.0 response uses `id` and `processingEntityType: "ogc-api-processes"`.
- During transition, the parser accepts cached statuses with either `jobID` or `id`.

This prevents Redis cache contents from becoming protocol-specific.

### 6. Store canonical results once, render many ways

The worker should continue storing result model dumps, not already-rendered HTTP responses.

Then adapters can render the same stored result as:

- 1.0 raw single-output response
- 1.0 document response
- 2.0 direct single-output response
- 2.0 `results.yaml` response
- 2.0 `values.yaml` response
- 2.0 individual output response
- 2.0 Nth multi-valued output response

Avoid storing version-specific result documents in Redis. Store only:

- canonical result payload
- canonical output selection metadata for the job
- process ID/version used to resolve output schemas

---

## Phases

### Phase 1 — Add compatibility constants and adapter skeletons

Files:

- `src/fastprocesses/core/models.py`
- `src/fastprocesses/api/router.py`
- `src/fastprocesses/api/adapters/processes_v1.py`
- `src/fastprocesses/api/adapters/processes_v2.py`

Steps:

1. Add constants for 1.0 and 2.0 conformance/profile URIs.
2. Add `FP_OGC_PROCESSES_DEFAULT_SPEC="1.0"` setting.
3. Add adapter selection by `spec` query parameter.
4. Keep 1.0 as the default behavior.
5. Add tests proving `spec=1.0` returns existing conformance identifiers and `spec=2.0` returns only implemented 2.0 identifiers.

Verify:

- Existing API tests still pass without passing `spec`.
- `GET /conformance?spec=2.0` does not advertise unimplemented collection/KVP classes.

### Phase 2 — Canonical execute request and output selection

Files:

- `src/fastprocesses/core/models.py`
- `src/fastprocesses/core/output_schema_resolver.py`
- `src/fastprocesses/api/adapters/processes_v1.py`
- `src/fastprocesses/api/adapters/processes_v2.py`

Steps:

1. Add `CanonicalExecuteRequest`, `OutputSelection`, and `CanonicalOutputRequest`.
2. Change adapter parsing so omitted `outputs`, empty `outputs`, and selected outputs are distinct.
3. Keep `ProcessExecRequestBody` for 1.0 request validation.
4. Add a 2.0 request model that omits `response` and `transmissionMode` from the public contract.
5. Update resolver input from `dict | None` to `OutputSelection`.

Verify:

- `outputs` omitted resolves all outputs.
- `outputs: {}` resolves no outputs and marks the job/request as no-output.
- `outputs: {"x": {}}` resolves only `x`.
- 1.0 clients using `response` and `transmissionMode` still get the old behavior.

### Phase 3 — Correct execution-mode negotiation

Files:

- `src/fastprocesses/api/router.py`
- `src/fastprocesses/api/manager.py`
- `src/fastprocesses/core/models.py`

Steps:

1. Add a pure `choose_execution_mode(process_description, prefer_header)` helper.
2. Implement 2.0 rules:
   - async-only process -> async
   - sync-only process -> sync
   - both modes and no `Prefer` -> sync
   - both modes and `Prefer: respond-async` -> async preferred
3. Preserve legacy behavior for `spec=1.0` until tests are updated and consumers are ready.
4. Add `Preference-Applied` when a preference is honored.

Verify:

- Unit tests for the helper cover all job-control combinations.
- Existing async default behavior remains for `spec=1.0` if that is required by deployed clients.
- `spec=2.0` follows the draft default of sync when both modes are available and no `Prefer` is sent.

### Phase 4 — 2.0 result rendering and profile links

Files:

- `src/fastprocesses/core/outputs_handler.py`
- `src/fastprocesses/core/output_schema_resolver.py`
- `src/fastprocesses/api/adapters/processes_v2.py`

Steps:

1. Add `serialize_result_v2(...)` beside the existing 1.0 serializer.
2. Support direct single-output responses when exactly one single-valued output is requested.
3. Support `results.yaml` JSON when multiple outputs are requested.
4. Support `values.yaml` JSON for multi-valued single outputs.
5. Add `Link: rel="profile"` response headers for `ogc-results` and `ogc-values` where applicable.
6. Keep `serialize_result(...)` as the 1.0 serializer until a later cleanup.

Verify:

- Existing `test_output_format_resolution.py` 1.0 tests still pass.
- New 2.0 tests assert response bodies and profile links.

### Phase 5 — Persist output-selection metadata per job

Files:

- `src/fastprocesses/api/manager.py`
- `src/fastprocesses/worker/celery_app.py`
- `src/fastprocesses/worker/chord_tasks.py`
- `src/fastprocesses/core/cache.py`

Steps:

1. Store canonical output selection with the job request metadata.
2. Store the process ID and version with the job.
3. Ensure cache hits still mint a distinct job ID but point to the canonical cached result.
4. For `outputs: {}`, mark the job as successful but with no retrievable outputs.

Verify:

- Re-fetching job results uses the original output selection, not the caller's current request body.
- Cache hits can still be rendered as 1.0 or 2.0 depending on the retrieval request.

### Phase 6 — Add 2.0 individual result endpoints

Files:

- `src/fastprocesses/api/router.py`
- `src/fastprocesses/api/manager.py`
- `src/fastprocesses/api/adapters/processes_v2.py`

Steps:

1. Add `GET /jobs/{job_id}/results/{output_id}`.
2. Add `GET /jobs/{job_id}/results/{output_id}/{index}`.
3. Include `OGC-Output-Values-Count` on individual result responses.
4. Return 404 for unrequested outputs.
5. Return 404 for out-of-bounds index values.
6. Return 404 `result-not-available` for successful no-output jobs.

Verify:

- Async job with selected output lets that output be fetched individually.
- Unrequested output returns 404.
- Multi-valued output index 0 works.
- Out-of-range index returns 404.

### Phase 7 — Job status compatibility rendering

Files:

- `src/fastprocesses/core/models.py`
- `src/fastprocesses/api/adapters/processes_v1.py`
- `src/fastprocesses/api/adapters/processes_v2.py`
- `src/fastprocesses/worker/job_status.py`

Steps:

1. Add canonical job status parsing that accepts old cached `jobID` fields.
2. Render 1.0 status with `jobID` and `type`.
3. Render 2.0 status with `id` and `processingEntityType`.
4. Add `processingEntityType="ogc-api-processes"` for 2.0 responses.
5. Preserve existing Redis entries across deployment.

Verify:

- Old cached job status can still be read after deploy.
- `GET /jobs/{id}?spec=1.0` emits 1.0 shape.
- `GET /jobs/{id}?spec=2.0` emits 2.0 shape.

### Phase 8 — 2.0 process-description rendering

Files:

- `src/fastprocesses/core/models.py`
- `src/fastprocesses/api/adapters/processes_v1.py`
- `src/fastprocesses/api/adapters/processes_v2.py`

Steps:

1. Accept current YAML descriptions unchanged.
2. Add optional 2.0 fields to input/output descriptions.
3. Render `outputTransmission` only in 1.0 responses.
4. Add process-description profile links in 2.0 responses.
5. Keep process authors on the same `BaseProcess.process_description` API.

Verify:

- Existing sample process descriptions validate.
- 1.0 description snapshots remain unchanged.
- 2.0 descriptions omit 1.0-only output transmission fields.

### Phase 9 — Advertise 2.0 Core subset

Only after Phases 1-8 pass, update `/conformance?spec=2.0` to advertise:

- Core
- JSON
- Job list
- OGC Process Description, if Phase 8 is complete
- Profile Query Parameter, if profile negotiation is implemented

Do not advertise these yet:

- Collection Input
- Remote Collections
- Collection Output
- Local Filtering
- KVP-encoded Execute

Those are separate feature projects.

### Phase 10 — Collection Input as the first large 2.0 extension

After the Core subset is real, implement Collection Input.

Start with local collections only:

1. Add collection input request model.
2. Require process inputs that accept collections to declare `dataAccessAPIs`.
3. Support `collection`, `bbox`, `datetime`, and `limit` first.
4. Fetch from local OGC API collection endpoints.
5. Feed retrieved values into the existing process execution path.

Then add:

- `filter`, `filterCrs`, `filterLang`
- `properties` / `aliases`
- `passThroughParameters`
- Remote Collections with SSRF protections
- Local Filtering only after remote/local source filtering behavior is proven

### Phase 11 — Collection Output

After result resources are stable, implement Collection Output:

1. Support `POST /processes/{id}/execution?response=collection`.
2. For one output, return `303 Location` to a collection description.
3. For multiple outputs, return `results.yaml` with links to collection descriptions.
4. Start with materialized output collections.
5. Add virtual/on-demand output collections later.

### Phase 12 — KVP Execute

Implement KVP execute last as a translation adapter:

```text
GET query parameters -> CanonicalExecuteRequest -> existing execution path
```

Do not duplicate execution logic. Do not support collection inputs in KVP unless the 2.0 spec later defines them.

---

## Files To Change First

- `src/fastprocesses/core/models.py` — canonical execute/output/job models, optional 2.0 description fields
- `src/fastprocesses/api/router.py` — adapter selection, execution-mode negotiation, new result routes
- `src/fastprocesses/api/manager.py` — canonical request handling and per-job output-selection metadata
- `src/fastprocesses/core/output_schema_resolver.py` — omitted-vs-empty output semantics
- `src/fastprocesses/core/outputs_handler.py` — split 1.0 and 2.0 serializers
- `src/fastprocesses/worker/job_status.py` — canonical job status updates
- `tests/test_output_format_resolution.py` — resolver and serializer compatibility tests
- `tests/test_e2e_format_caching.py` — cache/result rendering compatibility tests

---

## Compatibility Checklist

- [ ] Existing 1.0 clients can omit `spec` and keep current behavior.
- [ ] `spec=2.0` responses never include unimplemented conformance classes.
- [ ] Existing process YAML with `outputTransmission` still validates.
- [ ] 2.0 process descriptions do not emit `outputTransmission` by default.
- [ ] Existing `response=raw|document` requests still work for 1.0.
- [ ] 2.0 requests do not require `response=raw|document`.
- [ ] Existing `transmissionMode` requests still work for 1.0.
- [ ] 2.0 rendering does not rely on `transmissionMode`.
- [ ] Old cached job statuses using `jobID` are still readable.
- [ ] 2.0 job status emits `id` and `processingEntityType`.
- [ ] Results are stored once in canonical form and rendered per adapter.
- [ ] Omitted `outputs` and empty `outputs` are tested separately.
- [ ] Individual result endpoints are covered by async tests.
- [ ] Conformance output is truthful for both `spec=1.0` and `spec=2.0`.

---

## First Milestone Definition Of Done

The first milestone is complete when fastprocesses can truthfully expose a 2.0 Core subset for result handling while preserving 1.0 behavior by default:

1. `GET /conformance` returns 1.0 by default.
2. `GET /conformance?spec=2.0` returns only implemented 2.0 classes.
3. `POST /processes/{id}/execution?spec=2.0` distinguishes omitted, empty, and selected outputs.
4. `GET /jobs/{jobID}/results?spec=2.0` returns 2.0-shaped results.
5. `GET /jobs/{jobID}/results/{outputID}?spec=2.0` works.
6. `GET /jobs/{jobID}/results/{outputID}/{N}?spec=2.0` works for multi-valued outputs.
7. Existing 1.0 tests pass unchanged.
*** End Patch