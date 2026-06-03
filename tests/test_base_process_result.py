"""Unit tests for the new BaseProcessResult(BaseModel) design.

Covers: subclassing, field serialization, ClassVar exclusion from model_dump(),
serialize() dispatch, and error paths.
"""
from __future__ import annotations

from typing import ClassVar

import pytest

from fastprocesses.core.output_protocol import BaseProcessResult


# ---------------------------------------------------------------------------
# A minimal concrete subclass used across multiple tests.
# ---------------------------------------------------------------------------


class _FrequencyResult(BaseProcessResult):
    frequencies: dict[str, int]

    output_serializers: ClassVar = {
        "frequencies": {
            "application/json": "to_json",
            "text/csv": "to_csv",
        }
    }

    def to_json(self) -> bytes:
        import json

        return json.dumps(self.frequencies).encode()

    def to_csv(self) -> bytes:
        rows = "\n".join(f"{k},{v}" for k, v in self.frequencies.items())
        return f"word,count\n{rows}".encode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBaseProcessResultModel:
    def test_subclass_is_valid_pydantic_model(self):
        result = _FrequencyResult(frequencies={"hello": 3, "world": 2})
        assert result.frequencies == {"hello": 3, "world": 2}

    def test_output_serializers_excluded_from_model_dump(self):
        result = _FrequencyResult(frequencies={"a": 1})
        dumped = result.model_dump(mode="json")
        assert "output_serializers" not in dumped
        assert dumped == {"frequencies": {"a": 1}}

    def test_model_validate_roundtrip(self):
        original = _FrequencyResult(frequencies={"x": 5})
        raw = original.model_dump(mode="json")
        restored = _FrequencyResult.model_validate(raw)
        assert restored.frequencies == {"x": 5}


class TestBaseProcessResultSerialize:
    def test_serialize_json(self):
        import json

        result = _FrequencyResult(frequencies={"hello": 3})
        data = result.serialize("frequencies", "application/json")
        assert isinstance(data, bytes)
        assert json.loads(data) == {"hello": 3}

    def test_serialize_csv(self):
        result = _FrequencyResult(frequencies={"hello": 3, "world": 2})
        data = result.serialize("frequencies", "text/csv")
        lines = data.decode().splitlines()
        assert lines[0] == "word,count"
        assert "hello,3" in lines
        assert "world,2" in lines

    def test_serialize_unknown_media_type_raises(self):
        result = _FrequencyResult(frequencies={"a": 1})
        with pytest.raises(ValueError, match="No serializer for output 'frequencies'"):
            result.serialize("frequencies", "application/xml")

    def test_serialize_unknown_output_id_raises(self):
        result = _FrequencyResult(frequencies={"a": 1})
        with pytest.raises(ValueError, match="No serializer for output 'unknown'"):
            result.serialize("unknown", "application/json")

    def test_error_message_lists_available_types(self):
        result = _FrequencyResult(frequencies={"a": 1})
        with pytest.raises(ValueError, match="application/json"):
            result.serialize("frequencies", "text/html")


class TestBaseProcessResultInheritance:
    def test_subclass_can_override_output_serializers(self):
        """Each subclass has its own output_serializers, not a shared mutable dict."""

        class _OtherResult(BaseProcessResult):
            value: str

            output_serializers: ClassVar = {
                "value": {"text/plain": "to_plain"}
            }

            def to_plain(self) -> bytes:
                return self.value.encode()

        result = _OtherResult(value="hi")
        assert result.serialize("value", "text/plain") == b"hi"
        # The base class default is empty; _FrequencyResult's serializers are unaffected.
        assert BaseProcessResult.output_serializers == {}
        assert "value" not in _FrequencyResult.output_serializers

    def test_base_class_has_no_serializers(self):
        assert BaseProcessResult.output_serializers == {}
