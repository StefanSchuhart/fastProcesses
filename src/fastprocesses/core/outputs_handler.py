from __future__ import annotations

import base64
import json
from typing import Any
from uuid import uuid4

from fastapi.responses import JSONResponse, Response

from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.core.output_reference_publisher import OutputReferencePublisher
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver,
    ResolvedOutputFormat,
)


def serialize_result(
    result: BaseProcessResult,
    requested_outputs: dict[str, Any],
    response_mode: str,
    process_description: ProcessDescription,
    reference_publisher: OutputReferencePublisher | None = None,
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
        return _build_raw_response(result, resolved, reference_publisher)
    return _build_document_response(result, resolved, reference_publisher)


def _build_raw_response(
    result: BaseProcessResult,
    resolved: dict[str, ResolvedOutputFormat],
    reference_publisher: OutputReferencePublisher | None,
) -> Response:
    if len(resolved) != 1:
        return _build_raw_multipart_response(result, resolved, reference_publisher)

    output_id, output_format = next(iter(resolved.items()))
    payload = result.serialize(output_id, output_format.media_type)

    if output_format.transmission_mode == "reference":
        if reference_publisher is None:
            raise ValueError(
                "transmissionMode='reference' requested, but no "
                "reference_publisher is configured."
            )
        href = reference_publisher.publish(
            payload,
            output_id=output_id,
            media_type=output_format.media_type,
        )
        response = Response(status_code=204)
        response.headers["Link"] = (
            f'<{href}>; rel="alternate"; type="{output_format.media_type}"'
        )
        return response

    return Response(content=payload, media_type=output_format.media_type)


def _build_raw_multipart_response(
    result: BaseProcessResult,
    resolved: dict[str, ResolvedOutputFormat],
    reference_publisher: OutputReferencePublisher | None,
) -> Response:
    """Build a multipart/related response for raw multi-output requests."""
    boundary = f"fp-{uuid4().hex}"
    parts: list[bytes] = []

    for output_id, output_format in resolved.items():
        payload = result.serialize(output_id, output_format.media_type)
        part_media_type = output_format.media_type

        if output_format.transmission_mode == "reference":
            if reference_publisher is None:
                raise ValueError(
                    "transmissionMode='reference' requested, but no "
                    "reference_publisher is configured."
                )
            href = reference_publisher.publish(
                payload,
                output_id=output_id,
                media_type=output_format.media_type,
            )
            payload = json.dumps(
                {"href": href, "type": output_format.media_type}
            ).encode("utf-8")
            part_media_type = "application/json"

        part_header = (
            f"--{boundary}\r\n"
            f"Content-Type: {part_media_type}\r\n"
            f"Content-ID: <{output_id}>\r\n\r\n"
        ).encode("utf-8")
        parts.append(part_header + payload + b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return Response(
        content=body,
        media_type=f'multipart/related; boundary="{boundary}"',
    )


def _build_document_response(
    result: BaseProcessResult,
    resolved: dict[str, ResolvedOutputFormat],
    reference_publisher: OutputReferencePublisher | None,
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

        if output_format.transmission_mode == "reference":
            if reference_publisher is None:
                raise ValueError(
                    "transmissionMode='reference' requested, but no "
                    "reference_publisher is configured."
                )
            href = reference_publisher.publish(
                payload,
                output_id=output_id,
                media_type=output_format.media_type,
            )
            document[output_id] = {
                "href": href,
                "type": output_format.media_type,
            }
            continue

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

