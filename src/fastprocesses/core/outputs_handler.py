from __future__ import annotations

import base64
import json
from typing import Any

from fastapi.responses import JSONResponse, Response

from fastprocesses.core.exceptions import SerializationError
from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.output_protocol import ProcessResult
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver,
    ResolvedOutputFormat,
)


class OutputsHandler:
    """Bridges process execute() return values and the OGC API Processes wire format.

    The handler owns two concerns:
    - Format resolution: which media type each output should be serialized to,
      derived from the execute request body and the process description.
    - Serialization dispatch: calling the right serializer on each output value
      and assembling the final FastAPI Response.

    Process authors instantiate this inside execute() and call build_response()::

        return OutputsHandler(
            process_description=self.process_description,
            execute_request=exec_body,
        ).build_response({
            "risk_features": GeoDataFrameResult(risk_gdf),
            "report":        {"summary": "ok"},   # plain dict → JSON fallback
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
        """Resolve formats, serialize all outputs, and return the HTTP response.

        The response shape is controlled by the ``response`` field of the execute
        request body (OGC API Processes §7.11):
        - ``"raw"``      → single binary or text response (exactly one output required)
        - ``"document"`` → JSON envelope with one key per output
        """
        response_mode = self._request.get("response", "raw")
        requested_outputs = self._request.get("outputs") or {}
        resolved = self._resolver.resolve(requested_outputs)

        missing_outputs = set(resolved) - set(results)
        if missing_outputs:
            raise ValueError(
                f"execute() returned no value for requested output(s): "
                f"{missing_outputs}"
            )

        if response_mode == "raw":
            return self._build_raw_response(results, resolved)
        return self._build_document_response(results, resolved)

    # ------------------------------------------------------------------
    # Private response builders
    # ------------------------------------------------------------------

    def _build_raw_response(
        self,
        results: dict[str, Any],
        resolved: dict[str, ResolvedOutputFormat],
    ) -> Response:
        """Return a single-output response whose body is the raw serialized bytes.

        OGC API Processes §7.11: ``response=raw`` is only valid when exactly
        one output is requested.
        """
        if len(resolved) != 1:
            raise ValueError(
                "response='raw' requires exactly one output; "
                f"got {list(resolved)}"
            )
        output_id, output_format = next(iter(resolved.items()))
        payload = self._serialize(output_id, results[output_id], output_format.media_type)
        return Response(content=payload, media_type=output_format.media_type)

    def _build_document_response(
        self,
        results: dict[str, Any],
        resolved: dict[str, ResolvedOutputFormat],
    ) -> JSONResponse:
        """Return a JSON document with one key per output.

        Binary outputs are base64-encoded so they can travel inside JSON.
        Outputs with transmissionMode='reference' are stored externally and
        represented as {href, type} objects.
        """
        document: dict[str, Any] = {}

        for output_id, output_format in resolved.items():
            if output_format.transmission_mode == "reference":
                # Store the value externally and return a link to it
                href = self._store_and_get_href(
                    output_id, results[output_id], output_format
                )
                document[output_id] = {"href": href, "type": output_format.media_type}
                continue

            payload = self._serialize(
                output_id, results[output_id], output_format.media_type
            )

            if output_format.is_binary:
                # Binary payloads cannot be embedded in JSON directly;
                # encode as base64 and annotate with mediaType + encoding
                document[output_id] = {
                    "value": base64.b64encode(payload).decode("ascii"),
                    "mediaType": output_format.media_type,
                    "encoding": "base64",
                }
            else:
                # JSON-native output: parse bytes back to a Python object so
                # it nests cleanly inside the document envelope
                document[output_id] = json.loads(payload)

        return JSONResponse(content=document)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _serialize(
        self, output_id: str, value: Any, media_type: str
    ) -> bytes:
        """Dispatch serialization for a single output value.

        Resolution order:
        1. Value implements ProcessResult protocol → delegate entirely to .serialize()
        2. Value is already bytes → pass through (process did its own serialization)
        3. JSON-compatible primitive + JSON media type → built-in json.dumps fallback
        4. String value + text media type → encode to bytes fallback
        5. No path matched → raise SerializationError with an actionable message
        """
        # 1. ProcessResult protocol — the value knows how to serialize itself
        if isinstance(value, ProcessResult):
            return value.serialize(media_type)

        # 2. Raw bytes — the process already serialized the output
        if isinstance(value, bytes):
            return value

        # 3. JSON-compatible primitives with a JSON media type
        if media_type in ("application/json", "application/geo+json"):
            if isinstance(value, (dict, list, str, int, float, bool, type(None))):
                return json.dumps(value, ensure_ascii=False).encode()

        # 4. Plain strings with a text media type
        if media_type in ("text/plain", "text/html"):
            if isinstance(value, str):
                return value.encode()

        # 5. No serialization path found — give the process author a clear message
        raise SerializationError(
            f"Output '{output_id}': cannot serialize {type(value).__name__!r} "
            f"as '{media_type}'. "
            f"Wrap the value in a BaseProcessResult subclass that registers "
            f"a serializer for this media type."
        )

    def _store_and_get_href(
        self,
        output_id: str,
        value: Any,
        output_format: ResolvedOutputFormat,
    ) -> str:
        """Store the output externally and return the URL to retrieve it.

        Not implemented in the base class. Subclass OutputsHandler and override
        this method to support transmissionMode='reference'.
        """
        raise NotImplementedError(
            f"transmissionMode='reference' requested for output '{output_id}' "
            f"but _store_and_get_href is not implemented. "
            f"Subclass OutputsHandler and override this method."
        )
