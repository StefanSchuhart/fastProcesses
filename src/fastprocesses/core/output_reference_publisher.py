from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4


@runtime_checkable
class OutputReferencePublisher(Protocol):
    """Publishes output payload bytes and returns a retrievable href."""

    def publish(self, payload: bytes, *, output_id: str, media_type: str) -> str:
        """Persist payload and return the absolute/relative retrieval href."""


class LocalFileReferencePublisher:
    """Reference publisher that writes payloads to a local directory.

    The caller is responsible for serving ``base_directory`` via HTTP at
    ``base_href`` (for example through a static-files route or reverse proxy).
    """

    def __init__(self, base_directory: str | Path, base_href: str = "/results"):
        self._base_directory = Path(base_directory)
        self._base_directory.mkdir(parents=True, exist_ok=True)
        self._base_href = base_href.rstrip("/") or "/results"

    def publish(self, payload: bytes, *, output_id: str, media_type: str) -> str:
        guessed_ext = mimetypes.guess_extension(media_type.split(";", 1)[0].strip())
        ext = guessed_ext or ".bin"
        filename = f"{output_id}-{uuid4().hex}{ext}"
        path = self._base_directory / filename
        path.write_bytes(payload)
        return f"{self._base_href}/{filename}"
