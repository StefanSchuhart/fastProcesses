from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ProcessResult(Protocol):
    """
    Protocol for wrapping process output values with their serializers.
    Process authors return instances of this from execute().
    The library calls .serialize(media_type) -> bytes.
    """

    def serialize(self, media_type: str) -> bytes: ...

    def supported_media_types(self) -> list[str]: ...


class BaseProcessResult:
    """
    Optional convenience base. Process authors may subclass this
    or implement ProcessResult directly.

    Example::

        class GeoDataFrameResult(BaseProcessResult):
            def __init__(self, gdf):
                super().__init__(gdf)
                self.register("application/geo+json", lambda v: v.to_json().encode())
                self.register("application/flatgeobuf", lambda v: v.to_file(...))
    """

    def __init__(self, value: Any) -> None:
        self._value = value
        self._serializers: dict[str, Callable[[Any], bytes]] = {}

    def register(
        self, media_type: str, fn: Callable[[Any], bytes]
    ) -> "BaseProcessResult":
        """Register a serializer for a media type. Returns self for fluent chaining."""
        self._serializers[media_type] = fn
        return self

    def serialize(self, media_type: str) -> bytes:
        from fastprocesses.core.exceptions import SerializationError

        if media_type not in self._serializers:
            raise SerializationError(
                f"{type(self).__name__} has no serializer for '{media_type}'. "
                f"Supported: {self.supported_media_types()}"
            )
        return self._serializers[media_type](self._value)

    def supported_media_types(self) -> list[str]:
        return list(self._serializers.keys())
