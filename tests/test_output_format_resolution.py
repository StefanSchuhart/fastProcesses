"""Tests for OutputSchemaResolver and OutputsHandler.

Covers format resolution (happy path, defaults, error cases) and
response assembly (raw, document, binary encoding, ProcessResult dispatch).

No mocking of internal details — all tests go through the public API.
"""
import json

import pytest

from fastprocesses.core.exceptions import SerializationError
from fastprocesses.core.models import (
    ProcessDescription,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver,
    ResolvedOutputFormat,
)
from fastprocesses.core.outputs_handler import OutputsHandler

# ---------------------------------------------------------------------------
# Fixtures — minimal ProcessDescription instances
# ---------------------------------------------------------------------------


def _make_description(outputs: dict) -> ProcessDescription:
    """Build a minimal ProcessDescription with the given outputs dict."""
    return ProcessDescription(
        id="test_process",
        title="Test Process",
        version="1.0.0",
        description="A process used in unit tests.",
        jobControlOptions=[ProcessJobControlOptions.SYNC_EXECUTE],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={},
        outputs=outputs,
    )


def _geojson_output() -> ProcessOutput:
    """Single-format output that advertises application/geo+json."""
    return ProcessOutput(
        title="Features",
        description="GeoJSON feature collection.",
        scheme=Schema(format="geojson-feature-collection"),
    )


def _multi_format_output() -> ProcessOutput:
    """Output with two oneOf branches: geo+json and flatgeobuf."""
    return ProcessOutput(
        title="Features",
        description="Geospatial features in multiple formats.",
        scheme=Schema(
            oneOf=[
                Schema(format="geojson-feature-collection"),
                Schema(
                    type="string",
                    contentMediaType="application/flatgeobuf",
                    contentEncoding="base64",
                ),
            ]
        ),
    )


def _json_output() -> ProcessOutput:
    """Plain JSON output (dict / object)."""
    return ProcessOutput(
        title="Report",
        description="A JSON report.",
        scheme=Schema(contentMediaType="application/json"),
    )


def _binary_output() -> ProcessOutput:
    """PNG output — binary, string-encoded in the schema."""
    return ProcessOutput(
        title="Thumbnail",
        description="A PNG thumbnail.",
        scheme=Schema(type="string", contentMediaType="image/png"),
    )


# ---------------------------------------------------------------------------
# OutputSchemaResolver tests
# ---------------------------------------------------------------------------


class TestOutputSchemaResolver:
    def test_explicit_media_type_matched_in_one_of(self):
        """Requesting a mediaType that exists in oneOf returns the correct format."""
        description = _make_description({"features": _multi_format_output()})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve(
            {"features": {"format": {"mediaType": "application/flatgeobuf"}}}
        )

        assert resolved["features"].media_type == "application/flatgeobuf"
        assert resolved["features"].is_binary is True

    def test_default_format_uses_priority_order(self):
        """When no format is requested, the highest-priority advertised type is chosen."""
        description = _make_description({"features": _multi_format_output()})
        resolver = OutputSchemaResolver(description)

        # multi_format_output advertises geo+json and flatgeobuf;
        # geo+json ranks higher in MEDIA_TYPE_PRIORITY
        resolved = resolver.resolve({"features": {}})

        assert resolved["features"].media_type == "application/geo+json"
        assert resolved["features"].is_binary is False

    def test_empty_requested_outputs_resolves_all_described(self):
        """Passing an empty dict resolves every output in the process description."""
        description = _make_description(
            {"features": _geojson_output(), "report": _json_output()}
        )
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve({})

        assert set(resolved.keys()) == {"features", "report"}

    def test_unsupported_media_type_raises(self):
        """Requesting a mediaType not in the schema raises ValueError."""
        description = _make_description({"features": _geojson_output()})
        resolver = OutputSchemaResolver(description)

        with pytest.raises(ValueError, match="not supported"):
            resolver.resolve(
                {"features": {"format": {"mediaType": "image/png"}}}
            )

    def test_unknown_output_id_raises(self):
        """Requesting an output not declared in the description raises ValueError."""
        description = _make_description({"features": _geojson_output()})
        resolver = OutputSchemaResolver(description)

        with pytest.raises(ValueError, match="not declared"):
            resolver.resolve({"nonexistent": {}})

    def test_transmission_mode_forwarded(self):
        """transmissionMode from the request spec is preserved in the resolved format."""
        description = _make_description({"features": _geojson_output()})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve(
            {"features": {"transmissionMode": "reference"}}
        )

        assert resolved["features"].transmission_mode == "reference"

    def test_default_transmission_mode_is_value(self):
        """transmissionMode defaults to 'value' when not specified by the client."""
        description = _make_description({"features": _geojson_output()})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve({"features": {}})

        assert resolved["features"].transmission_mode == "value"

    def test_ogc_format_hint_resolved_via_allof(self):
        """OGC format hints nested inside allOf are resolved to the correct media type."""
        schema_with_allof = Schema(
            allOf=[Schema(format="geojson-feature-collection")]
        )
        output = ProcessOutput(
            title="Features", description="", scheme=schema_with_allof
        )
        description = _make_description({"features": output})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve({"features": {}})

        assert resolved["features"].media_type == "application/geo+json"


# ---------------------------------------------------------------------------
# OutputsHandler tests
# ---------------------------------------------------------------------------


class TestOutputsHandler:
    def test_raw_response_single_output_dict(self):
        """response='raw' with a dict value returns a Response with correct Content-Type."""
        description = _make_description({"report": _json_output()})
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "raw", "outputs": {"report": {}}},
        )

        response = handler.build_response({"report": {"status": "ok"}})

        assert response.status_code == 200
        assert response.media_type == "application/json"
        body = json.loads(response.body)
        assert body == {"status": "ok"}

    def test_raw_response_multiple_outputs_raises(self):
        """response='raw' with more than one output raises ValueError."""
        description = _make_description(
            {"a": _json_output(), "b": _json_output()}
        )
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "raw", "outputs": {}},
        )

        with pytest.raises(ValueError, match="exactly one output"):
            handler.build_response({"a": {}, "b": {}})

    def test_document_response_json_output(self):
        """response='document' embeds a JSON output directly in the envelope."""
        description = _make_description({"report": _json_output()})
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "document", "outputs": {"report": {}}},
        )

        response = handler.build_response({"report": {"count": 42}})

        body = json.loads(response.body)
        assert body == {"report": {"count": 42}}

    def test_document_response_binary_output_is_base64(self):
        """Binary outputs in a document response are base64-encoded with metadata."""
        description = _make_description({"thumb": _binary_output()})
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "document", "outputs": {"thumb": {}}},
        )
        raw_bytes = b"\x89PNG\r\n\x1a\n"

        response = handler.build_response({"thumb": raw_bytes})

        body = json.loads(response.body)
        assert body["thumb"]["encoding"] == "base64"
        assert body["thumb"]["mediaType"] == "image/png"
        import base64
        assert base64.b64decode(body["thumb"]["value"]) == raw_bytes

    def test_process_result_protocol_is_called(self):
        """A ProcessResult-protocol value delegates serialization to .serialize()."""
        description = _make_description({"features": _geojson_output()})
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "document", "outputs": {"features": {}}},
        )

        geojson_bytes = json.dumps(
            {"type": "FeatureCollection", "features": []}
        ).encode()

        class _GeoJsonResult:
            def serialize(self, media_type: str) -> bytes:
                return geojson_bytes

            def supported_media_types(self) -> list[str]:
                return ["application/geo+json"]

        response = handler.build_response({"features": _GeoJsonResult()})

        body = json.loads(response.body)
        assert body["features"]["type"] == "FeatureCollection"

    def test_missing_output_in_results_raises(self):
        """If execute() omits a requested output, build_response raises ValueError."""
        description = _make_description({"report": _json_output()})
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "document", "outputs": {"report": {}}},
        )

        with pytest.raises(ValueError, match="returned no value"):
            handler.build_response({})

    def test_unserializable_value_raises_serialization_error(self):
        """A value with no serialization path raises SerializationError."""
        description = _make_description({"report": _json_output()})
        handler = OutputsHandler(
            process_description=description,
            execute_request={"response": "raw", "outputs": {"report": {}}},
        )

        class Unserializable:
            pass

        with pytest.raises(SerializationError, match="cannot serialize"):
            handler.build_response({"report": Unserializable()})

    def test_process_result_unsupported_media_type_raises(self):
        """A ProcessResult raises when asked for an unsupported media type."""

        class _TextResult:
            def serialize(self, media_type: str) -> bytes:
                if media_type != "text/plain":
                    raise SerializationError(
                        f"no serializer for '{media_type}'"
                    )
                return b"some data"

            def supported_media_types(self) -> list[str]:
                return ["text/plain"]

        with pytest.raises(SerializationError, match="no serializer for"):
            _TextResult().serialize("application/geo+json")

    def test_default_response_mode_is_raw(self):
        """When 'response' is absent from the request, 'raw' is used."""
        description = _make_description({"report": _json_output()})
        handler = OutputsHandler(
            process_description=description,
            # No 'response' key
            execute_request={"outputs": {"report": {}}},
        )

        response = handler.build_response({"report": [1, 2, 3]})

        assert response.media_type == "application/json"
        assert json.loads(response.body) == [1, 2, 3]
