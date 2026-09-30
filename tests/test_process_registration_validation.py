"""Tests for _validate_process_registration — bidirectional format checks.

Covers both directions of the advertised-format <-> output_serializers
relationship, plus the semantic-hint-as-dict-key guard rail.
"""
from typing import ClassVar

import pytest

from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.exceptions import ProcessRegistrationError
from fastprocesses.core.models import (
    ProcessDescription,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.processes.process_registry import _validate_process_registration


def _make_description(outputs: dict) -> ProcessDescription:
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


def _geojson_flatgeobuf_output() -> ProcessOutput:
    return ProcessOutput(
        title="Features",
        description="",
        scheme=Schema(
            oneOf=[
                Schema(allOf=[Schema(format="geojson-feature-collection")]),
                Schema(type="string", contentMediaType="application/flatgeobuf"),
            ]
        ),
    )


def _make_process(result_cls):
    """Build a minimal BaseProcess subclass whose execute() returns result_cls."""

    class _FakeProcess(BaseProcess):
        async def execute(self, exec_body, job_progress_callback=None) -> result_cls:  # type: ignore[valid-type]
            raise NotImplementedError

    return _FakeProcess()


def test_validation_passes_when_serializers_match_advertised_formats():
    """Result class with matching serializers registers without error."""

    class MatchingResult(BaseProcessResult):
        features: dict
        output_serializers: ClassVar = {
            "features": {
                "application/geo+json": "_to_geojson",
                "application/flatgeobuf": "_to_fgb",
            }
        }

    description = _make_description({"features": _geojson_flatgeobuf_output()})
    process = _make_process(MatchingResult)

    _validate_process_registration("test_process", process, description)


def test_validation_fails_when_orphan_serializer_key_present():
    """A serializer entry not advertised by the schema is rejected."""

    class OrphanResult(BaseProcessResult):
        features: dict
        output_serializers: ClassVar = {
            "features": {
                "application/geo+json": "_to_geojson",
                "application/flatgeobuf": "_to_fgb",
                "image/png": "_to_png",  # not advertised anywhere
            }
        }

    description = _make_description({"features": _geojson_flatgeobuf_output()})
    process = _make_process(OrphanResult)

    with pytest.raises(ProcessRegistrationError, match="not advertised"):
        _validate_process_registration("test_process", process, description)


def test_validation_fails_when_serializer_key_is_semantic_hint():
    """Using an OGC format hint (not an IANA media type) as a dict key is rejected."""

    class HintKeyResult(BaseProcessResult):
        features: dict
        output_serializers: ClassVar = {
            "features": {
                "geojson-feature-collection": "_to_geojson",  # wrong: hint, not IANA type
                "application/flatgeobuf": "_to_fgb",
            }
        }

    description = _make_description({"features": _geojson_flatgeobuf_output()})
    process = _make_process(HintKeyResult)

    with pytest.raises(ProcessRegistrationError, match="OGC format hint"):
        _validate_process_registration("test_process", process, description)


def test_validation_fails_when_advertised_format_missing_serializer():
    """Pre-existing check: advertised format with no serializer entry is rejected."""

    class IncompleteResult(BaseProcessResult):
        features: dict
        output_serializers: ClassVar = {
            "features": {"application/geo+json": "_to_geojson"}
            # missing application/flatgeobuf
        }

    description = _make_description({"features": _geojson_flatgeobuf_output()})
    process = _make_process(IncompleteResult)

    with pytest.raises(ProcessRegistrationError, match="no entry for it"):
        _validate_process_registration("test_process", process, description)
