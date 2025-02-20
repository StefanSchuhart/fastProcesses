import hashlib
import json
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

class ProcessExecRequestBody(BaseModel):
  inputs: Dict[str, Any]
  outputs: Dict[str, Any] = {}
  response: Literal["document", "raw"] = "raw"

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


class ProcessList(BaseModel):
    """
    The OGC conform ProcessList Model.
    """

    processes: list[ProcessSummary]
    links: list[Link]
