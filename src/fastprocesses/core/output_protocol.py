from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class ProcessResult(Protocol):
    """
    Protocol for wrapping process output values with their serializers.
    Kept for backward compatibility during the v2 migration.
    """

    def serialize(self, media_type: str) -> bytes: ...

    def supported_media_types(self) -> list[str]: ...


class BaseProcessResult(BaseModel):
    """
    Base class for process results.

    Subclass this and add one field per output_id. Declare serializers via
    ``output_serializers`` so the library knows how to convert each output
    field to bytes for a given media type.

    Example::

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
                rows = "\\n".join(f"{k},{v}" for k, v in self.frequencies.items())
                return f"word,count\\n{rows}".encode()
    """

    # {output_id: {media_type: method_name}}
    output_serializers: ClassVar[dict[str, dict[str, str]]] = {}

    def serialize(self, output_id: str, media_type: str) -> bytes:
        """Serialize the named output field to the requested media type."""
        serializers = self.output_serializers.get(output_id, {})
        method_name = serializers.get(media_type)
        if method_name is None:
            raise ValueError(
                f"No serializer for output '{output_id}' / media type '{media_type}'. "
                f"Available: {list(serializers.keys())}"
            )
        return getattr(self, method_name)()
