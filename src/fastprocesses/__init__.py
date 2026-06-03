"""fastProcesses package.

A library to create a FastAPI-based  OGC API Processes wrapper around existing projects.
"""

from __future__ import annotations

from fastprocesses.core.exceptions import ProcessRegistrationError, SerializationError
from fastprocesses.core.output_protocol import BaseProcessResult, ProcessResult
from fastprocesses.core.output_schema_resolver import (
    OutputSchemaResolver,
    ResolvedOutputFormat,
)
from fastprocesses.core.outputs_handler import serialize_result

__all__ = [
    "BaseProcessResult",
    "OutputSchemaResolver",
    "ProcessRegistrationError",
    "ProcessResult",
    "ResolvedOutputFormat",
    "SerializationError",
    "serialize_result",
]
