import hashlib
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

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

class ProcessInputs(BaseModel):
  inputs: Dict[str, Any]

class CalculationTask(ProcessInputs):
    def _hash_dict(self):
       return hashlib.sha256(json.dumps(self.inputs, sort_keys=True).encode()).hexdigest()

    @property
    def celery_key(self) -> str:
        return self._hash_dict()

class ProcessResponse(BaseModel):
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
