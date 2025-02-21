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

class ProcessDescription(BaseModel):
  id: str
  title: str
  description: str
  version: str
  inputs: Dict[str, Dict[str, Any]]
  outputs: Dict[str, Dict[str, Any]]

class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"

class ProcessExecRequestBody(BaseModel):
    inputs: Dict[str, Any]
    mode: Optional[ExecutionMode] = ExecutionMode.ASYNC
    response: Optional[str] = "document"

class CalculationTask(BaseModel):
    inputs: Dict[str, Any]

    def _hash_dict(self):
       return hashlib.sha256(json.dumps(self.inputs, sort_keys=True).encode()).hexdigest()

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