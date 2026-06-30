from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastprocesses.core.models import ProcessDescription, Schema


# Normative OGC semantic hint → IANA media type mapping (OGC API Processes 18-062r2)
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
    media_type: str         # canonical IANA media type string
    transmission_mode: str  # "value" | "reference"
    is_binary: bool         # True → bytes; False → JSON-native object
    schema_branch: Schema   # matched oneOf branch (or root schema)


def _media_type_from_schema(schema: Schema) -> str | None:
    """Extract the implied media type from a Schema node.

    Checks, in order:
    1. contentMediaType (explicit IANA type on string-encoded values)
    2. format field matching a known OGC semantic hint
    3. Recursion into allOf branches (OGC often places hints there)
    """
    if schema.contentMediaType:
        return schema.contentMediaType

    if schema.format and schema.format in OGC_FORMAT_HINTS:
        return OGC_FORMAT_HINTS[schema.format]

    for sub_schema in schema.allOf or []:
        media_type = _media_type_from_schema(sub_schema)
        if media_type:
            return media_type

    return None


def _find_branch(schema: Schema, requested_media_type: str) -> Schema | None:
    """Return the oneOf branch whose implied media type matches requested_media_type.

    Returns None when no branch (or the root schema) implies that type.
    """
    for branch in schema.oneOf or []:
        if _media_type_from_schema(branch) == requested_media_type:
            return branch
    # No oneOf branch matched; check whether the root schema itself implies the type
    if _media_type_from_schema(schema) == requested_media_type:
        return schema
    return None


def _default_media_type(schema: Schema) -> str | None:
    """Pick the highest-priority supported media type from the schema's oneOf branches.

    Falls back to the first non-None candidate when none of the priority types match,
    and returns None when the schema carries no media type information at all.
    """
    # Collect the implied media type from every oneOf branch (or the root schema when
    # there are no branches). Entries may be None for branches with no media type hint.
    candidate_branches = schema.oneOf or [schema]
    candidate_media_types = [
        _media_type_from_schema(branch) for branch in candidate_branches
    ]

    # Prefer types in priority order so we always return the most capable format first
    for preferred_media_type in MEDIA_TYPE_PRIORITY:
        if preferred_media_type in candidate_media_types:
            return preferred_media_type

    # None of the preferred types matched — return whatever the schema advertises
    advertised_media_type = next(
        (media_type for media_type in candidate_media_types if media_type is not None),
        None,
    )
    if advertised_media_type is not None:
        return advertised_media_type

    # OGC examples frequently describe JSON-native outputs (array/object/number
    # etc.) without explicit contentMediaType/format hints. Treat these as
    # application/json by default so resolver behavior matches spec examples.
    if schema.oneOf is None:
        return "application/json"

    return None


def _is_binary(schema: Schema) -> bool:
    """True when the wire value is bytes rather than a JSON-native object.

    GeoJSON is a JSON object (not binary). FlatGeobuf, GeoTIFF, PNG are binary.
    A schema branch is binary when its type is "string" with a contentMediaType
    that is not a JSON dialect.
    """
    return (
        schema.type == "string"
        and schema.contentMediaType
        not in (None, "application/json", "application/geo+json")
    )


class OutputSchemaResolver:
    def __init__(self, process_description: ProcessDescription) -> None:
        self._desc = process_description

    def resolve(
        self, requested_outputs: dict[str, Any]
    ) -> dict[str, ResolvedOutputFormat]:
        """Resolve the execute request outputs dict against the process description.

        Returns one ResolvedOutputFormat per requested output.
        If requested_outputs is empty, all described outputs are resolved with
        their default formats (OGC API Processes req. 27).
        """
        # OGC API Processes req. 27: when the client omits "outputs" entirely,
        # resolve all described outputs using their default formats
        if not requested_outputs:
            requested_outputs = {output_id: {} for output_id in self._desc.outputs}

        resolved: dict[str, ResolvedOutputFormat] = {}

        for output_id, output_spec in requested_outputs.items():
            if output_id not in self._desc.outputs:
                raise ValueError(
                    f"Output '{output_id}' not declared in process description."
                )

            # ProcessOutput declares the field as .scheme with alias "schema"
            schema = self._desc.outputs[output_id].scheme
            # output_spec may be None when client writes {"outputs": {"my_out": null}}
            output_spec = output_spec if isinstance(output_spec, dict) else {}

            # The client may include {"format": {"mediaType": "application/geo+json"}}
            # inside the per-output spec to request a specific serialization format
            format_object = output_spec.get("format") or {}
            requested_media_type = (
                format_object.get("mediaType")
                if isinstance(format_object, dict)
                else None
            )

            if requested_media_type:
                # Client specified a format — validate it is advertised by the process
                matched_branch = _find_branch(schema, requested_media_type)
                if matched_branch is None:
                    advertised_media_types = [
                        _media_type_from_schema(branch)
                        for branch in (schema.oneOf or [schema])
                    ]
                    advertised = [
                        mt for mt in advertised_media_types if mt is not None
                    ]
                    raise ValueError(
                        f"Output '{output_id}': mediaType"
                        f" '{requested_media_type}' not supported."
                        f" Advertised: {advertised}"
                    )
                media_type = requested_media_type
                branch = matched_branch
            else:
                # No format requested — pick the highest-priority default
                # (the first MEDIA_TYPE_PRIORITY entry the process advertises)
                media_type = _default_media_type(schema)
                if not media_type:
                    raise ValueError(
                        f"Output '{output_id}': no format requested and "
                        f"no default derivable from process description."
                    )
                # Locate the matching branch so _is_binary can inspect it accurately;
                # fall back to the root schema when there are no oneOf branches
                branch = _find_branch(schema, media_type) or schema

            resolved[output_id] = ResolvedOutputFormat(
                output_id=output_id,
                media_type=media_type,
                transmission_mode=output_spec.get("transmissionMode", "value"),
                is_binary=_is_binary(branch),
                schema_branch=branch,
            )

        return resolved
