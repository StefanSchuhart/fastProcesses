# [REJECTED] Output Format Resolution — Implementation Plan
## fastprocesses library

> **Purpose**: Guide for implementing OGC API Processes–compliant output format
> resolution, serialization delegation, and response assembly.
> Written for a downstream implementor. Knowledge gaps are marked **⚠ VERIFY**.

---

## Background & design decisions

The OGC API Processes spec (18-062r2) allows a client to request each output
in a specific media type via the execute request body:

```json
{
  "outputs": {
    "risk_features": { "format": { "mediaType": "application/geo+json" } },
    "healpix_aggregation": { "format": { "mediaType": "application/flatgeobuf" } }
  },
  "response": "document"
}
```

The process description advertises supported formats per output via `oneOf`
branches in the output schema — either with `contentMediaType` (for binary /
string-encoded values) or with an OGC semantic `format` hint (for native JSON
objects). The library must bridge these two sides without knowing anything
about third-party data types (GeoDataFrame, rasterio datasets, etc.).

**Core principle**: the library owns OGC spec mechanics; the process author
owns domain serialization. The boundary is a `ProcessResult` protocol.

The revised minimal surface for process authors is just two things:

```python
# 1. Declare what your output supports — in the ProcessDescription (already done)

# 2. Return values that know how to serialize themselves
class MyResult(BaseProcessResult):
    def __init__(self, value):
        super().__init__(value)
        self.register("application/geo+json", ...)

async def execute(self, exec_body, ...):
    return {"my_output": MyResult(computed_value)}

```
---

## New files to create

### `src/fastprocesses/core/output_protocol.py`

Defines the `ProcessResult` protocol and `BaseProcessResult` helper.
**No third-party imports** (no geopandas, rasterio, etc.) — domain-agnostic.

```python
from __future__ import annotations
from typing import Any, Callable, Protocol, runtime_checkable


class SerializationError(Exception):
    pass


@runtime_checkable
class ProcessResult(Protocol):
    """
    Protocol for wrapping process output values with their serializers.
    Process authors return instances of this from execute().
    The library calls .serialize(media_type) -> bytes.
    """
    def serialize(self, media_type: str) -> bytes: ...
    def supported_media_types(self) -> list[str]: ...


class BaseProcessResult:
    """
    Optional convenience base. Process authors may subclass this
    or implement ProcessResult directly.
    """
    def __init__(self, value: Any) -> None:
        self._value = value
        self._serializers: dict[str, Callable[[Any], bytes]] = {}

    def register(
        self, media_type: str, fn: Callable[[Any], bytes]
    ) -> "BaseProcessResult":
        self._serializers[media_type] = fn
        return self  # fluent API

    def serialize(self, media_type: str) -> bytes:
        if media_type not in self._serializers:
            raise SerializationError(
                f"{type(self).__name__} has no serializer for '{media_type}'. "
                f"Supported: {self.supported_media_types()}"
            )
        return self._serializers[media_type](self._value)

    def supported_media_types(self) -> list[str]:
        return list(self._serializers.keys())
```

---

### `src/fastprocesses/core/output_schema_resolver.py`

Walks the process description output schemas to map a requested `mediaType`
to the correct `oneOf` branch, handling both `contentMediaType` and OGC
semantic `format` hints.

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

# ⚠ VERIFY: import path for Schema and ProcessDescription
from fastprocesses.core.models import Schema, ProcessDescription


# Normative OGC semantic hint → IANA media type mapping (18-062r2)
OGC_FORMAT_HINTS: dict[str, str] = {
    "geojson-geometry":           "application/geo+json",
    "geojson-feature":            "application/geo+json",
    "geojson-feature-collection": "application/geo+json",
    "ogc-bbox":                   "application/json",
}

# Default preference order when client omits format
MEDIA_TYPE_PRIORITY = [
    "application/geo+json",
    "application/flatgeobuf",
    "application/json",
    "image/png",
]


@dataclass(frozen=True)
class ResolvedOutputFormat:
    output_id: str
    media_type: str        # canonical IANA media type string
    transmission_mode: str # "value" | "reference"
    is_binary: bool        # True → bytes; False → JSON-native object
    schema_branch: Schema  # matched oneOf branch (or root schema)


def _media_type_from_schema(schema: Schema) -> str | None:
    """
    Extract the implied media type from a Schema node.
    Handles contentMediaType (explicit) and OGC format hints (implicit).

    ⚠ VERIFY: field names on Schema match what is defined in core/models.py.
    Schema may use aliases or extra-fields storage. Relevant fields:
      schema.contentMediaType  (str | None)
      schema.format            (str | None)  — OGC semantic hint OR JSON Schema format
      schema.allOf             (list[Schema] | None)
      schema.oneOf             (list[Schema] | None)
      schema.type              (str | None)
    """
    if getattr(schema, "contentMediaType", None):
        return schema.contentMediaType

    fmt = getattr(schema, "format", None)
    if fmt and fmt in OGC_FORMAT_HINTS:
        return OGC_FORMAT_HINTS[fmt]

    # Recurse into allOf — OGC format hints often sit inside allOf branches
    for sub in getattr(schema, "allOf", None) or []:
        mt = _media_type_from_schema(sub)
        if mt:
            return mt

    return None


def _find_branch(schema: Schema, requested_mt: str) -> Schema | None:
    for branch in getattr(schema, "oneOf", None) or []:
        if _media_type_from_schema(branch) == requested_mt:
            return branch
    if _media_type_from_schema(schema) == requested_mt:
        return schema
    return None


def _default_media_type(schema: Schema) -> str | None:
    candidates = [
        _media_type_from_schema(b)
        for b in (getattr(schema, "oneOf", None) or [schema])
    ]
    for preferred in MEDIA_TYPE_PRIORITY:
        if preferred in candidates:
            return preferred
    return next((c for c in candidates if c), None)


def _is_binary(schema: Schema) -> bool:
    """
    True when the wire value is bytes rather than a JSON-native object.
    GeoJSON is a JSON object (not binary). FlatGeobuf, GeoTIFF, PNG are binary.
    """
    return (
        getattr(schema, "type", None) == "string"
        and getattr(schema, "contentMediaType", None)
        not in (None, "application/json", "application/geo+json")
    )


class OutputSchemaResolver:
    def __init__(self, process_description: ProcessDescription) -> None:
        self._desc = process_description

    def resolve(
        self, requested_outputs: dict[str, Any]
    ) -> dict[str, ResolvedOutputFormat]:
        """
        Resolves the execute request outputs dict against the process description.
        Returns one ResolvedOutputFormat per requested output.

        If requested_outputs is empty, all described outputs are resolved
        with their default formats (OGC API Processes req. 27).

        ⚠ VERIFY: ProcessDescription.outputs is a dict[str, ProcessOutput]
        and ProcessOutput.schema returns a Schema instance.
        """
        if not requested_outputs:
            requested_outputs = {oid: {} for oid in self._desc.outputs}

        resolved: dict[str, ResolvedOutputFormat] = {}

        for output_id, spec in requested_outputs.items():
            if output_id not in self._desc.outputs:
                raise ValueError(
                    f"Output '{output_id}' not declared in process description."
                )

            schema = self._desc.outputs[output_id].schema
            spec = spec if isinstance(spec, dict) else {}

            fmt_obj = spec.get("format") or {}
            requested_mt = fmt_obj.get("mediaType") if isinstance(fmt_obj, dict) else None

            if requested_mt:
                branch = _find_branch(schema, requested_mt)
                if branch is None:
                    advertised = [
                        _media_type_from_schema(b)
                        for b in (getattr(schema, "oneOf", None) or [schema])
                    ]
                    raise ValueError(
                        f"Output '{output_id}': mediaType '{requested_mt}' not supported. "
                        f"Advertised: {[m for m in advertised if m]}"
                    )
                media_type = requested_mt
            else:
                media_type = _default_media_type(schema)
                if not media_type:
                    raise ValueError(
                        f"Output '{output_id}': no format requested and "
                        f"no default derivable from process description."
                    )
                branch = _find_branch(schema, media_type) or schema

            resolved[output_id] = ResolvedOutputFormat(
                output_id=output_id,
                media_type=media_type,
                transmission_mode=spec.get("transmissionMode", "value"),
                is_binary=_is_binary(branch),
                schema_branch=branch,
            )

        return resolved
```

---

### `src/fastprocesses/core/outputs_handler.py`

Orchestrates: parse execute request → resolve formats → serialize → build
FastAPI response. This is the surface process authors interact with.

```python
from __future__ import annotations
import base64
import json
from typing import Any

from fastapi.responses import JSONResponse, Response

# ⚠ VERIFY: import paths
from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.output_protocol import (
    BaseProcessResult, ProcessResult, SerializationError
)
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver, ResolvedOutputFormat
)


class OutputsHandler:
    """
    Bridges process execute() return values and the OGC API Processes
    wire format. Process authors call build_response() with their results.

    Usage:

        return OutputsHandler(
            process_description=self.process_description,
            execute_request=exec_body,
        ).build_response({
            "risk_features":        GeoDataFrameResult(risk_gdf),
            "validation_report":    report_dict,   # plain dict → JSON fallback
            "classification_breaks_wb": [0.1, 0.4, 0.7],  # list → JSON fallback
        })
    """

    def __init__(
        self,
        process_description: ProcessDescription,
        execute_request: dict[str, Any],
    ) -> None:
        self._desc = process_description
        self._request = execute_request
        self._resolver = OutputSchemaResolver(process_description)

    def build_response(self, results: dict[str, Any]) -> Response:
        response_mode = self._request.get("response", "raw")
        requested_outputs = self._request.get("outputs") or {}
        resolved = self._resolver.resolve(requested_outputs)

        missing = set(resolved) - set(results)
        if missing:
            raise ValueError(
                f"execute() returned no value for requested output(s): {missing}"
            )

        if response_mode == "raw":
            return self._raw_response(results, resolved)
        return self._document_response(results, resolved)

    # ------------------------------------------------------------------ #

    def _raw_response(
        self, results: dict, resolved: dict[str, ResolvedOutputFormat]
    ) -> Response:
        if len(resolved) != 1:
            raise ValueError(
                "response='raw' requires exactly one output; "
                f"got {list(resolved)}"
            )
        output_id, fmt = next(iter(resolved.items()))
        payload = self._serialize(output_id, results[output_id], fmt.media_type)
        return Response(content=payload, media_type=fmt.media_type)

    def _document_response(
        self, results: dict, resolved: dict[str, ResolvedOutputFormat]
    ) -> JSONResponse:
        document: dict[str, Any] = {}
        for output_id, fmt in resolved.items():
            if fmt.transmission_mode == "reference":
                # ⚠ VERIFY / implement: store result externally, return href
                href = self._store_and_get_href(output_id, results[output_id], fmt)
                document[output_id] = {"href": href, "type": fmt.media_type}
                continue

            payload = self._serialize(output_id, results[output_id], fmt.media_type)

            if fmt.is_binary:
                document[output_id] = {
                    "value": base64.b64encode(payload).decode("ascii"),
                    "mediaType": fmt.media_type,
                    "encoding": "base64",
                }
            else:
                document[output_id] = json.loads(payload)

        return JSONResponse(content=document)

    # ------------------------------------------------------------------ #

    def _serialize(self, output_id: str, value: Any, media_type: str) -> bytes:
        # 1. Value implements ProcessResult protocol → delegate entirely
        if isinstance(value, ProcessResult):
            return value.serialize(media_type)

        # 2. Already bytes → pass through (process did the serialization)
        if isinstance(value, bytes):
            return value

        # 3. JSON-compatible primitives → built-in fallback
        if media_type in ("application/json", "application/geo+json"):
            if isinstance(value, (dict, list, str, int, float, bool, type(None))):
                return json.dumps(value, ensure_ascii=False).encode()

        if media_type in ("text/plain", "text/html"):
            if isinstance(value, str):
                return value.encode()

        # 4. No path worked → clear actionable error
        raise SerializationError(
            f"Output '{output_id}': cannot serialize {type(value).__name__!r} "
            f"as '{media_type}'. "
            f"Wrap the value in a ProcessResult implementation that registers "
            f"a serializer for this media type."
        )

    def _store_and_get_href(self, output_id: str, value: Any, fmt: ResolvedOutputFormat) -> str:
        raise NotImplementedError(
            f"transmissionMode='reference' requested for output '{output_id}' "
            f"but _store_and_get_href is not implemented. "
            f"Subclass OutputsHandler and override this method."
        )
```

---

## Changes to existing files

### `src/fastprocesses/core/models.py`

**⚠ VERIFY** that the `Schema` Pydantic model has the following fields
(or equivalent via `model_config` / `extra="allow"`). The resolver reads:

| Field | Type | Notes |
|---|---|---|
| `type` | `str \| None` | JSON Schema `type` |
| `format` | `str \| None` | OGC semantic hint or JSON Schema format |
| `oneOf` | `list[Schema] \| None` | Format variant branches |
| `allOf` | `list[Schema] \| None` | Composition (OGC uses this for format hints) |
| `contentMediaType` | `str \| None` | IANA media type for string-encoded values |
| `contentEncoding` | `str \| None` | e.g. `"base64"` for binary-in-document mode |

If `Schema` uses `extra="allow"` (Pydantic v2 `model_config`), these fields
may need to be accessed via `schema.model_extra.get("contentMediaType")`
rather than `schema.contentMediaType`. **Adjust `_media_type_from_schema`
and `_is_binary` in the resolver accordingly.**

---

### `src/fastprocesses/api/router.py`

**⚠ VERIFY** how the router currently:
1. Receives the raw execute request body
2. Calls `process.execute(exec_body, ...)`
3. Builds and returns the HTTP response

The new design requires that `exec_body` (the full, unparsed execute request
dict including `outputs` and `response`) is passed through to `execute()` and
then to `OutputsHandler.build_response()`. If the router currently strips or
transforms the body before passing it, that needs to be preserved.

**Likely change**: where the router currently calls something like
`return JSONResponse(result.model_dump())`, it should instead return the
`Response` object that `OutputsHandler.build_response()` produces directly —
since that response already has the correct `Content-Type` and body encoding.

---

### `src/fastprocesses/worker/pipeline.py` and `worker/executors.py`

**⚠ VERIFY** how Celery task results are stored and retrieved. Currently
`execute()` returns a `BaseModel`. With the new design it returns
`dict[str, ProcessResult | primitive]`.

Key questions:
- Are results serialized to Redis? If so, `ProcessResult` objects are not
  directly JSON-serializable. The result dict may need to be stored as raw
  bytes per output key, or the `OutputsHandler` must be called **before**
  the result is written to Redis (i.e. inside the Celery task).
- If results pass through Redis, consider whether to serialize at task
  completion time (store bytes keyed by output_id + media_type) or defer
  serialization to result fetch time (store the raw Python objects, which
  requires pickling or a custom Redis serializer).

**Recommended approach**: call `OutputsHandler` inside the executor, store
the fully-serialized output dict to Redis, and have the router read and
return it. This avoids needing to round-trip complex Python objects through
Redis.

---

### `src/fastprocesses/core/exceptions.py`

Add `SerializationError` if not already present, OR re-export it from
`output_protocol.py`. **⚠ VERIFY** existing exception hierarchy to avoid
duplicates.

---

### `src/fastprocesses/__init__.py`

Export the new public surface from the top-level package:

```python
from fastprocesses.core.output_protocol import (
    ProcessResult,
    BaseProcessResult,
    SerializationError,
)
from fastprocesses.core.outputs_handler import OutputsHandler
```

**⚠ VERIFY** what is currently exported and whether adding these creates
circular import issues given the existing `__init__.py` structure.

---

## Responsibility map (summary)

```
fastprocesses (library — owns spec mechanics)
├── core/output_protocol.py        ProcessResult protocol, BaseProcessResult
├── core/output_schema_resolver.py OGC format hint → media type, oneOf branch resolution
├── core/outputs_handler.py        resolve → serialize → build FastAPI Response
└── OGC_FORMAT_HINTS               finite normative mapping, all that the library knows

process author's project (owns domain knowledge)
├── GeoDataFrameResult(BaseProcessResult)    registers GeoJSON / FlatGeobuf / KML
├── RasterResult(BaseProcessResult)          registers GeoTIFF / PNG / raw bytes
└── execute() → dict[str, ProcessResult | primitive]
```

---

## ⚠ Knowledge gap summary

| # | Gap | Where to check |
|---|---|---|
| 1 | `Schema` field names and access pattern (direct attr vs `model_extra`) | `core/models.py` |
| 2 | Whether `exec_body` passed to `execute()` is the raw request dict or already transformed | `api/router.py` |
| 3 | How Celery task results currently flow from worker → Redis → router response | `worker/pipeline.py`, `worker/executors.py`, `api/router.py` |
| 4 | Whether `OutputsHandler` should be called inside the Celery task or in the router at result-fetch time | `worker/executors.py`, `api/router.py` |
| 5 | Existing exception types in `core/exceptions.py` — avoid duplicating `SerializationError` | `core/exceptions.py` |
| 6 | Current `__init__.py` exports — check for circular imports before adding new exports | `__init__.py` |
| 7 | Whether `ProcessDescription.outputs` is `dict[str, ProcessOutput]` and `ProcessOutput.schema` is a `Schema` instance | `core/models.py` |

---

## Caching design — discussion and decisions

### Context (problems observed during implementation)

Two bugs surfaced while testing the `word_frequency_process` example:

1. **Format mismatch on cache hit** — a client requested `text/csv` after a
   prior `application/json` run with the same inputs. The cached (JSON)
   serialized envelope was returned unchanged, yielding the wrong format.
2. **Missing outer output key** — a `document`-mode response lacked the
   expected `{ "frequencies": ... }` wrapper because the cached `raw`
   envelope (a bare payload) was returned for a `document`-mode request.

Root cause: the initial cache key was derived only from `inputs + outputs`
(without `response`), so `raw` and `document` modes, or two different
`mediaType` values, all mapped to the same cache key.

### Option A — per-format caching (simplest, implemented as interim fix)

**Cache key = hash(inputs + outputs descriptors + response mode)**

- Each distinct combination of inputs / requested output formats / response
  mode gets its own Redis entry holding a pre-serialized envelope.
- Implemented in `CalculationTask._hash_dict` (added `response` to the
  hashed dict inside `core/models.py`).
- **Downside**: multiplies cache storage when the same inputs are requested
  in many format combinations; the process runs once per unique combination.

### Option B — canonical-result caching (recommended, not yet implemented)

**Agreed design goal**: the cache should be hit whenever the same inputs and
the same *logical* outputs (same output IDs, same format hints) are
requested, regardless of the wire-format details (`raw` vs `document`).
The process runs once; serialization and response assembly happen on
retrieval without re-running the process.

**Key insight**: `raw` vs `document` is a presentation choice, not a
computational one.  Both modes need the same serialized bytes per output;
only the envelope shape differs.

Concrete design:

1. **Canonical key** — derived from `inputs` + canonical output descriptors
   (output IDs + their `format.mediaType` if specified, but NOT `response`
   and NOT transmission-mode details).

2. **Stored value** — a canonical JSON-safe envelope per invocation:

   ```json
   {
     "outputs": {
       "frequencies": {
         "serialized": { "text/csv": "<base64-encoded-bytes>" },
         "is_binary": false,
         "media_type": "text/csv"
       }
     }
   }
   ```

   For each output the envelope stores a `serialized` map keyed by
   `mediaType` → base64-encoded bytes.  The worker populates the entry for
   the mediaType that was actually produced.

3. **Cache write** (inside `execute_process` in `worker/celery_app.py`) —
   serialize each output via `OutputsHandler._serialize`, store the
   `serialized` map under the canonical key.

4. **Cache read** (in `api/router.py`) — for each requested output, look up
   `serialized[requested_mediaType]`.
   - **Hit** → decode and assemble `raw` or `document` response.
   - **Miss for this mediaType** (a previously unseen format is requested
     for the same logical inputs) → re-run serialization from the canonical
     envelope using `OutputsHandler` and store the result for future reuse.

5. **Cache invalidation / migration** — flush dev Redis after deploying
   because existing entries have a different structure.  Add a
   `CACHE_SCHEMA_VERSION` prefix to all keys to allow rolling upgrades.

### Option C — per-output caching (additive, future work)

**Goal**: allow partial cache hits when a client requests a subset of the
outputs that a previous run already produced.

Example — first call:
```json
{ "outputs": { "output1": {}, "output2": {} } }
```
Second call (subset):
```json
{ "outputs": { "output1": {} } }
```
Under per-output caching, `output1` would be served from cache while
`output2` is skipped entirely (not computed, not returned).

**Design**:
- Canonical key per *individual output* = hash(inputs + output_id +
  output_format_hint).
- On write: store each output's serialized bytes under its own key.
- On read: for every requested output, attempt a cache lookup.  If all
  outputs hit, assemble the response.  If any miss, run the full process
  and repopulate all output keys.
- **Constraint**: only valid when outputs are independent (the process does
  not need to compute all outputs jointly).  Process authors who rely on
  shared intermediate computation must declare outputs as a group or accept
  re-runs.
- **Complexity**: moderate.  Requires atomic multi-key writes (Redis MULTI /
  pipeline), a per-output lookup loop in the router/manager, and
  documentation for process authors about the independence assumption.
- **Decision**: defer until canonical-result caching (Option B) is stable.

---

## Implementation steps (current status)

| # | Step | Status | File(s) |
|---|---|---|---|
| 1 | `ProcessResult` protocol + `BaseProcessResult` | ✅ Done | `core/output_protocol.py` |
| 2 | `OutputSchemaResolver` — `oneOf` + OGC hints | ✅ Done | `core/output_schema_resolver.py` |
| 3 | `OutputsHandler` — resolve → serialize → Response | ✅ Done | `core/outputs_handler.py` |
| 4 | `SerializationError` in `exceptions.py` | ✅ Done | `core/exceptions.py` |
| 5 | `models.py` — fix `ProcessOutput.scheme` aliases, `outputs` typing | ✅ Done | `core/models.py` |
| 6 | `base_process.py` — `execute()` return type, `validate_outputs()` signature | ✅ Done | `core/base_process.py` |
| 7 | Worker-side serialization — call `OutputsHandler` in `execute_process` | ✅ Done | `worker/celery_app.py` |
| 8 | Router unwrap — `_unwrap_fp_result` rebuilds `Response` from envelope | ✅ Done | `api/router.py` |
| 9 | Public exports in `__init__.py` | ✅ Done | `__init__.py` |
| 10 | Unit tests for resolver + handler | ✅ Done | `tests/test_output_format_resolution.py` |
| 11 | `word_frequency_process` example (JSON + CSV) | ✅ Done | `examples/run_example.py` |
| 12 | Interim cache-key fix — include `response` in hash | ✅ Done | `core/models.py` — `CalculationTask._hash_dict` |
| 13 | Canonical-result caching (Option B) — format-agnostic key + serialized-by-mediaType envelope | 🔲 Pending | `worker/celery_app.py`, `api/router.py`, `core/models.py` |
| 14 | End-to-end test — same inputs, different formats, cache correctness | 🔲 Pending | `tests/` |
| 15 | Per-output caching (Option C) | 🔲 Deferred | `worker/celery_app.py`, `api/router.py`, `api/manager.py` |
| 16 | `_store_and_get_href` — `transmissionMode="reference"` storage | 🔲 Deferred | `core/outputs_handler.py` |
| 17 | Cache key schema versioning / migration guide | 🔲 Pending | docs / README |
