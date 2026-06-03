# Output Helpers v2 — Refactoring Plan

## Goal

Replace the current `ProcessResult` protocol + envelope/unwrap machinery with a
simpler, BaseModel-based `BaseProcessResult` that:

1. Gives process authors a Pydantic model they subclass — fields **are** output IDs.
2. Declares serializers declaratively via a `ClassVar`.
3. Caches format-agnostic canonical JSON (`model_dump(mode="json")`).
4. Serializes only at the response boundary (API router), never in the worker.
5. Removes all envelope/unwrap branching.

---

## Agreed Design

### `BaseProcessResult`

```python
from pydantic import BaseModel
from typing import Any, ClassVar

class BaseProcessResult(BaseModel):
    """
    Base class for process results.
    Subclass this, add fields for each output_id.
    """

    # Declarative serializer registry:
    # {output_id: {media_type: method_name_or_callable}}
    output_serializers: ClassVar[dict[str, dict[str, str]]] = {}

    def serialize(self, output_id: str, media_type: str) -> bytes:
        """Serialize a single output to the requested media type."""
        serializers = self.output_serializers.get(output_id, {})
        method_name = serializers.get(media_type)
        if method_name is None:
            raise ValueError(
                f"No serializer for output '{output_id}' / media type '{media_type}'. "
                f"Available: {list(serializers.keys())}"
            )
        method = getattr(self, method_name)
        return method()
```

### Process author usage

```python
class WordFrequencyResult(BaseProcessResult):
    frequencies: dict[str, int]

    output_serializers: ClassVar = {
        "frequencies": {
            "application/json": "frequencies_to_json",
            "text/csv": "frequencies_to_csv",
        }
    }

    def frequencies_to_json(self) -> bytes:
        return self.model_dump_json().encode()

    def frequencies_to_csv(self) -> bytes:
        rows = "\n".join(f"{k},{v}" for k, v in self.frequencies.items())
        return f"word,count\n{rows}".encode()
```

### Process execute return

```python
class WordFrequencyProcess(BaseProcess):
    async def execute(self, exec_body, ...) -> WordFrequencyResult:
        freqs = count_words(text)
        return WordFrequencyResult(frequencies=freqs)
```

---

## Data Flow (new)

```
Client request
    │
    ▼
Router (execute_process / get_job_result)
    │
    ├─ sync: call worker directly
    │   └─ worker returns BaseProcessResult.model_dump(mode="json")
    │
    ├─ async: retrieve from cache
    │   └─ cache stores model_dump(mode="json") (format-agnostic)
    │
    ▼
serialize_result(result_dict, result_class, requested_outputs)
    │
    ├─ reconstruct: result_class.model_validate(result_dict)
    ├─ for each requested output_id:
    │     resolve media_type via OutputSchemaResolver
    │     call result.serialize(output_id, media_type)
    │
    ▼
FastAPI Response (JSONResponse / Response with bytes)
```

---

## What Changes

### REMOVE

| File | What | Why |
|------|------|-----|
| `core/output_protocol.py` | `ProcessResult` protocol, current `BaseProcessResult` class | Replaced by Pydantic-based `BaseProcessResult` |
| `worker/celery_app.py` | `_build_fp_envelope()`, `_contains_process_result()` | No more envelope pattern |
| `api/router.py` | `_unwrap_fp_result()` | No more unwrapping |
| `worker/chord_tasks.py` | Envelope handling in `finalize_parallel` / `finalize_scatter` | Plain `model_dump()` flows through |

### KEEP (unchanged or minor adjustments)

| File | What | Notes |
|------|------|-------|
| `core/output_schema_resolver.py` | `OutputSchemaResolver` | Still resolves requested media types from process descriptions |
| `core/models.py` | `CalculationTask`, `ProcessOutput`, etc. | Remove `response` from cache key hash (no longer needed) |

### SIMPLIFY

| File | Current | New |
|------|---------|-----|
| `core/outputs_handler.py` | `OutputsHandler` class with `build_response()` | Single function: `serialize_result(result_dict, result_class, requested_outputs, process_desc) -> Response` |
| `worker/celery_app.py` | Envelope wrapping | Just return `result.model_dump(mode="json")` |
| `worker/chord_tasks.py` | Envelope handling in finalize | Just return merged `model_dump(mode="json")` dict |
| `core/base_process.py` | Return type `BaseModel \| Dict[str, Any]` | Return type `BaseProcessResult` |

### ADD

| File | What |
|------|------|
| `core/output_protocol.py` | New `BaseProcessResult(BaseModel)` with `output_serializers` ClassVar and `serialize()` method |
| `core/outputs_handler.py` (or rename) | `serialize_result()` function at response boundary |

---

## Cache Design

**Key derivation**: hash of `inputs` + `output_ids` only (no `response` or format info).

```python
def _hash_dict(self) -> str:
    data = self.model_dump(mode="json", include={"inputs", "outputs"})
    # outputs here = which output_ids were requested, NOT their format
    # strip format info before hashing
    if "outputs" in data and data["outputs"]:
        data["outputs"] = {k: {} for k in data["outputs"]}
    return hashlib.md5(
        json.dumps(data, sort_keys=True).encode()
    ).hexdigest()
```

**Stored value**: `result.model_dump(mode="json")` — a plain dict of `{output_id: value}`.

**On cache hit**: reconstruct `result_class.model_validate(cached_dict)` → call
`serialize(output_id, media_type)` at the response boundary.

---

## Registration-Time Validation

When `@register_process("id")` is called, validate:

1. Process class has a type-annotated return for `execute()` that is a `BaseProcessResult` subclass.
2. Each `output_id` advertised in the process description exists as a field on the result model.
3. Each `media_type` advertised in `oneOf` branches has a corresponding entry in `output_serializers`.

Raise `ProcessRegistrationError` at import time if validation fails — fast feedback.

---

## Migration Steps (ordered)

| # | Step | Verify |
|---|------|--------|
| 1 | Rewrite `core/output_protocol.py` with new `BaseProcessResult(BaseModel)` | Unit test: subclass, instantiate, `serialize()` works |
| 2 | Update `core/base_process.py` return type annotations | No runtime change, type check passes |
| 3 | Simplify `worker/celery_app.py`: remove `_build_fp_envelope`, `_contains_process_result`; `execute_process` returns `result.model_dump(mode="json")` | Existing tests still pass (may need fixture updates) |
| 4 | Simplify `worker/chord_tasks.py`: remove envelope handling in finalize functions | Parallel/scatter tests pass |
| 5 | Rewrite `core/outputs_handler.py` → `serialize_result()` function | Unit test: given dict + class + requested format → correct bytes |
| 6 | Update `api/router.py`: remove `_unwrap_fp_result()`, call `serialize_result()` | Integration test: full request → correct response |
| 7 | Update `core/models.py`: remove `response` from cache key hash, strip format from outputs | Cache key test: same inputs + different formats → same key |
| 8 | Add registration-time validation in `processes/process_registry.py` | Test: invalid process class → `ProcessRegistrationError` at import |
| 9 | Update example processes in `examples/run_example.py` | Examples run end-to-end |
| 10 | Update/add tests for new behavior | Full test suite green |

---

## Parallel / Scatter Considerations

For parallel processes, `merge_results()` currently returns a dict. Under the new
design:

- Each `parallel_step` returns its contribution (can be any serializable value).
- `merge_results()` receives all contributions and returns a `BaseProcessResult`.
- The chord finalize callback calls `merge_results()` → `result.model_dump(mode="json")`.

For scatter processes, same pattern — `merge_results()` assembles the final
`BaseProcessResult` from sub-results.

No special envelope handling needed — merge always produces a model, model always
dumps to JSON for caching/transport.

---

## Open Questions

1. **Multi-output document mode**: When `response=document`, OGC spec wraps all outputs
   in a single JSON document. Should `serialize_result()` handle this, or should we keep
   a separate code path? → Likely: `serialize_result()` returns either a single `Response`
   (for `raw` mode / single output) or a JSON document dict (for `document` mode).

2. **Streaming / large outputs**: Should `serialize()` return `bytes` or
   `bytes | AsyncIterator[bytes]`? Start with `bytes`, add streaming later if needed.

3. **Default serializer**: If no `output_serializers` entry exists for an output, fall
   back to `model_dump_json()` for that field (i.e., plain JSON serialization as default)?
   → Likely yes, keeps simple cases simple.
