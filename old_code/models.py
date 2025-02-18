from pydantic import BaseModel
from typing import List, Dict, Any, Optional

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

class ProcessRequest(BaseModel):
  data: Dict[str, Any]

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

