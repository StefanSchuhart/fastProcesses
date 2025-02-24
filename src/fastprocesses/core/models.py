from enum import Enum
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field


class Link(BaseModel):
  href: str
  rel: str
  type: str

class Landing(BaseModel):
  title: str
  description: str
  links: List[Link]

class Conformance(BaseModel):
  conformsTo: List[str]

class ProcessJobControlOptions(str, Enum):
    SYNC_EXECUTE = "sync-execute"
    ASYNC_EXECUTE = "async-execute"
    DISMISS = "dismiss"

class ProcessOutputTransmission(str, Enum):
    VALUE = "value"
    REFERENCE = "reference"

class ResponseType(str, Enum):
    RAW = "raw"
    DOCUMENT = "document"

class Schema(BaseModel):
    type: str
    format: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    minLength: Optional[int] = None
    maxLength: Optional[int] = None
    pattern: Optional[str] = None
    enum: Optional[List[Any]] = None

class ProcessInput(BaseModel):
    title: str
    description: str
    schema: Schema
    minOccurs: Optional[int] = 1
    maxOccurs: Optional[int] = 1
    metadata: Optional[Dict[str, Any]] = None

class ProcessOutput(BaseModel):
    title: str
    description: str
    scheme: Schema = Field(alias="schema")
    metadata: Optional[Dict[str, Any]] = None

class ProcessDescription(BaseModel):
    id: str
    title: str
    description: str
    version: str
    jobControlOptions: List[ProcessJobControlOptions]
    outputTransmission: List[ProcessOutputTransmission]
    inputs: Dict[str, ProcessInput]
    outputs: Dict[str, ProcessOutput]
    links: Optional[List[Link]] = None
    keywords: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None

class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"

class ProcessExecRequestBody(BaseModel):
    inputs: Dict[str, Any]
    outputs: List[str] | None = None
    mode: Optional[ExecutionMode] = ExecutionMode.ASYNC
    response: Optional[ResponseType] = ResponseType.RAW

class CalculationTask(BaseModel):
    inputs: Dict[str, Any]
    outputs: List[str] | None = None
    response: ResponseType = ResponseType.RAW

    def _hash_dict(self):
       return hashlib.sha256(
           json.dumps(self.inputs, sort_keys=True).encode()
        ).hexdigest()

    @computed_field
    def celery_key(self) -> str:
        return self._hash_dict()

class ProcessExecResponse(BaseModel):
  status: str
  jobID: str
  type: str = "process"

class ProcessSummary(BaseModel):
    """
    The OGC conform ProcessSummary Model.
    """

    id: str
    version: str
    links: Optional[list[Link]] = None

class JobStatusInfo(BaseModel):
    jobID: str
    status: str
    type: str = "process"
    processID: Optional[str] = None
    message: Optional[str] = None
    created: Optional[datetime] = None
    started: Optional[datetime] = None
    finished: Optional[datetime] = None
    updated: Optional[datetime] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    links: Optional[List[Link]] = None

class JobList(BaseModel):
    jobs: List[JobStatusInfo]
    links: List[Link]