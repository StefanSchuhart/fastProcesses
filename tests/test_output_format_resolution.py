"""Tests for OutputSchemaResolver and serialize_result.

Covers format resolution (happy path, defaults, error cases) and
response assembly (raw, document, binary encoding).

No mocking of internal details — all tests go through the public API.
"""
import json
from typing import ClassVar

import pytest

from fastprocesses.core.models import (
    ProcessDescription,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver,
    ResolvedOutputFormat,
)
from fastprocesses.core.outputs_handler import serialize_result

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

    def test_default_json_for_plain_array_schema(self):
        """A plain array schema with no media hints defaults to application/json."""
        output = ProcessOutput(
            title="Array output",
            description="",
            scheme=Schema(type="array", items={"type": "integer"}),
        )
        description = _make_description({"array_output": output})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve({"array_output": {}})

        assert resolved["array_output"].media_type == "application/json"
        assert resolved["array_output"].is_binary is False

    def test_default_json_for_plain_object_schema(self):
        """A plain object schema with no media hints defaults to application/json."""
        output = ProcessOutput(
            title="Object output",
            description="",
            scheme=Schema(type="object", properties={"x": {"type": "number"}}),
        )
        description = _make_description({"object_output": output})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve({"object_output": {}})

        assert resolved["object_output"].media_type == "application/json"
        assert resolved["object_output"].is_binary is False

    def test_default_json_for_plain_number_schema(self):
        """A plain number schema with no media hints defaults to application/json."""
        output = ProcessOutput(
            title="Number output",
            description="",
            scheme=Schema(type="number"),
        )
        description = _make_description({"number_output": output})
        resolver = OutputSchemaResolver(description)

        resolved = resolver.resolve({"number_output": {}})

        assert resolved["number_output"].media_type == "application/json"
        assert resolved["number_output"].is_binary is False


# ---------------------------------------------------------------------------
# Minimal BaseProcessResult subclasses used by serialization tests
# ---------------------------------------------------------------------------


class _JsonResult(BaseProcessResult):
    """Result with a plain JSON output."""
    report: dict | list

    output_serializers: ClassVar = {
        "report": {"application/json": "_to_json"}
    }

    def _to_json(self) -> bytes:
        return json.dumps(self.report, ensure_ascii=False).encode()


class _GeoJsonResult(BaseProcessResult):
    """Result with a GeoJSON output."""
    features: dict

    output_serializers: ClassVar = {
        "features": {
            "application/geo+json": "_to_geojson",
            "application/flatgeobuf": "_to_flatgeobuf",
        }
    }

    def _to_geojson(self) -> bytes:
        return json.dumps(self.features).encode()

    def _to_flatgeobuf(self) -> bytes:
        # Stub — real impl would encode flatgeobuf
        return b"\x66\x67\x62"  # "fgb" magic bytes


class _BinaryResult(BaseProcessResult):
    """Result with a binary (PNG) output."""
    thumb: bytes

    output_serializers: ClassVar = {
        "thumb": {"image/png": "_to_png"}
    }

    def _to_png(self) -> bytes:
        return self.thumb


# ---------------------------------------------------------------------------
# serialize_result tests
# ---------------------------------------------------------------------------


class TestSerializeResult:
    def test_raw_response_single_json_output(self):
        """response='raw' with a JSON output returns correct Content-Type and body."""
        description = _make_description({"report": _json_output()})
        result = _JsonResult(report={"status": "ok"})

        response = serialize_result(result, {"report": {}}, "raw", description)

        assert response.status_code == 200
        assert response.media_type == "application/json"
        assert json.loads(response.body) == {"status": "ok"}

    def test_raw_response_multiple_outputs_raises(self):
        """response='raw' with more than one output raises ValueError."""
        description = _make_description(
            {"features": _geojson_output(), "report": _json_output()}
        )

        class _MultiResult(BaseProcessResult):
            features: dict
            report: dict
            output_serializers: ClassVar = {
                "features": {"application/geo+json": "_f"},
                "report": {"application/json": "_r"},
            }
            def _f(self) -> bytes: return b"{}"
            def _r(self) -> bytes: return b"{}"

        result = _MultiResult(features={}, report={})
        with pytest.raises(ValueError, match="exactly one output"):
            serialize_result(result, {}, "raw", description)

    def test_document_response_json_output_qualified_value(self):
        """response='document' wraps JSON output as a qualified value with mediaType."""
        description = _make_description({"report": _json_output()})
        result = _JsonResult(report={"count": 42})

        response = serialize_result(result, {"report": {}}, "document", description)

        body = json.loads(response.body)
        assert body["report"]["value"] == {"count": 42}
        assert body["report"]["mediaType"] == "application/json"

    def test_document_response_binary_output_is_base64(self):
        """Binary outputs in document mode are base64-encoded with encoding metadata."""
        import base64
        description = _make_description({"thumb": _binary_output()})
        raw_bytes = b"\x89PNG\r\n\x1a\n"
        result = _BinaryResult(thumb=raw_bytes)

        response = serialize_result(result, {"thumb": {}}, "document", description)

        body = json.loads(response.body)
        assert body["thumb"]["encoding"] == "base64"
        assert body["thumb"]["mediaType"] == "image/png"
        assert base64.b64decode(body["thumb"]["value"]) == raw_bytes

    def test_geojson_raw_response(self):
        """GeoJSON output in raw mode returns application/geo+json."""
        description = _make_description({"features": _geojson_output()})
        fc = {"type": "FeatureCollection", "features": []}
        result = _GeoJsonResult(features=fc)

        response = serialize_result(result, {"features": {}}, "raw", description)

        assert response.media_type == "application/geo+json"
        assert json.loads(response.body) == fc

    def test_document_response_geojson_qualified_value(self):
        """GeoJSON in document mode is wrapped as a qualified value."""
        description = _make_description({"features": _geojson_output()})
        fc = {"type": "FeatureCollection", "features": []}
        result = _GeoJsonResult(features=fc)

        response = serialize_result(result, {"features": {}}, "document", description)

        body = json.loads(response.body)
        assert body["features"]["value"] == fc
        assert body["features"]["mediaType"] == "application/geo+json"

    def test_unknown_media_type_raises(self):
        """Requesting a media type with no registered serializer raises ValueError."""
        description = _make_description({"report": _json_output()})
        result = _JsonResult(report={})

        with pytest.raises(ValueError, match="No serializer"):
            # Force resolution to a type that has no serializer entry
            # by patching output_serializers to be empty
            result.__class__.output_serializers = {}
            try:
                serialize_result(result, {"report": {}}, "raw", description)
            finally:
                result.__class__.output_serializers = {
                    "report": {"application/json": "_to_json"}
                }

    def test_default_empty_outputs_resolves_all(self):
        """Empty requested_outputs resolves all described outputs."""
        description = _make_description({"report": _json_output()})
        result = _JsonResult(report=[1, 2, 3])

        response = serialize_result(result, {}, "raw", description)

        assert response.media_type == "application/json"
        assert json.loads(response.body) == [1, 2, 3]
