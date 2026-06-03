from __future__ import annotations

import base64
import json
from typing import Any

from fastapi.responses import JSONResponse, Response

from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver,
    ResolvedOutputFormat,
)


def serialize_result(
    result: BaseProcessResult,
    requested_outputs: dict[str, Any],
    response_mode: str,
    process_description: ProcessDescription,
) -> Response:
    """Serialize a ``BaseProcessResult`` to an HTTP response at the API boundary.

    This is the single serialization point for all process output — called in
    the router after the canonical result dict has been reconstructed into a
    result model via ``result_class.model_validate(cached_dict)``.

    Args:
        result: A ``BaseProcessResult`` instance holding the output field values.
        requested_outputs: The ``outputs`` dict from the execute request body.
        response_mode: ``"raw"`` or ``"document"`` (OGC API Processes §7.11).
        process_description: Used to resolve the media type for each output.

    Returns:
        A ``Response`` (raw mode) or ``JSONResponse`` (document mode).
    """
    resolver = OutputSchemaResolver(process_description)
    resolved = resolver.resolve(requested_outputs or {})

    if response_mode == "raw":
        return _build_raw_response(result, resolved)
    return _build_document_response(result, resolved)


def _build_raw_response(
    result: BaseProcessResult,
    resolved: dict[str, ResolvedOutputFormat],
) -> Response:
    if len(resolved) != 1:
        raise ValueError(
            "response='raw' requires exactly one output; "
            f"got {list(resolved)}"
        )
    output_id, output_format = next(iter(resolved.items()))
    payload = result.serialize(output_id, output_format.media_type)
    return Response(content=payload, media_type=output_format.media_type)


def _build_document_response(
    result: BaseProcessResult,
    resolved: dict[str, ResolvedOutputFormat],
) -> JSONResponse:
    """Return a JSON document with OGC-spec qualified value wrappers.

    Per OGC API Processes ``results.yaml``, each output is a qualified value::

        {
            "frequencies": {
                "value": {"hello": 3, "world": 2},
                "mediaType": "application/json"
            }
        }

    Binary outputs add ``"encoding": "base64"`` and base64-encode the value.
    """
    document: dict[str, Any] = {}

    for output_id, output_format in resolved.items():
        payload = result.serialize(output_id, output_format.media_type)

        if output_format.is_binary:
            document[output_id] = {
                "value": base64.b64encode(payload).decode("ascii"),
                "mediaType": output_format.media_type,
                "encoding": "base64",
            }
        else:
            try:
                document[output_id] = {
                    "value": json.loads(payload),
                    "mediaType": output_format.media_type,
                }
            except (json.JSONDecodeError, UnicodeDecodeError):
                document[output_id] = {
                    "value": payload.decode("utf-8", errors="replace"),
                    "mediaType": output_format.media_type,
                }

    return JSONResponse(content=document)

