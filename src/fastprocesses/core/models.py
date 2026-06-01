import hashlib
import json
from datetime import datetime
from enum import Enum, StrEnum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

import yaml
from fastapi.encoders import jsonable_encoder
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    computed_field
)


class OGCExceptionResponse(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None


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


# TODO: needs to be passed to outputs keys and when part of the data validated
# TODO: transmission mode can be different for each output
# https://schemas.opengis.net/ogcapi/processes/part1/1.0/examples/json/ProcessDescription.json
class ProcessOutputTransmission(str, Enum):
    VALUE = "value"
    REFERENCE = "reference"


class ResponseType(str, Enum):
    RAW = "raw"
    DOCUMENT = "document"


class Schema(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    # --- References ---
    # validation_alias accepts both 'ref' (Python) and '$ref' (JSON);
    # serialization_alias ensures model_dump(by_alias=True) outputs '$ref'.
    ref: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ref", "$ref"),
        serialization_alias="$ref",
    )

    # --- Core keywords ---
    # JSON Schema allows type to be a string OR an array of strings
    type: Optional[Union[str, List[str]]] = None
    format: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    default: Optional[Any] = None
    const: Optional[Any] = None
    enum: Optional[List[Any]] = None

    # --- Numeric validation ---
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    exclusiveMinimum: Optional[float] = None
    exclusiveMaximum: Optional[float] = None
    multipleOf: Optional[float] = None

    # --- String validation ---
    minLength: Optional[int] = None
    maxLength: Optional[int] = None
    pattern: Optional[str] = None

    # --- Array validation ---
    items: Optional[Union["Schema", List["Schema"]]] = None
    minItems: Optional[int] = None
    maxItems: Optional[int] = None
    uniqueItems: Optional[bool] = None

    # --- Object validation ---
    properties: Optional[Dict[str, "Schema"]] = None
    patternProperties: Optional[Dict[str, "Schema"]] = None
    additionalProperties: Optional[Union[bool, "Schema"]] = None
    required: Optional[List[str]] = None
    minProperties: Optional[int] = None
    maxProperties: Optional[int] = None

    # --- Schema composition ---
    allOf: Optional[List["Schema"]] = None
    anyOf: Optional[List["Schema"]] = None
    oneOf: Optional[List["Schema"]] = None
    # 'not' is a Python keyword; use alias
    not_: Optional["Schema"] = Field(default=None, alias="not")

    # --- Content annotation (draft 2019-09+) ---
    contentMediaType: Optional[str] = None
    contentEncoding: Optional[str] = None
    contentSchema: Optional[str] = None

class ProcessInput(BaseModel):
    title: str
    description: str
    scheme: Schema = Field(
        validation_alias=AliasChoices("scheme", "schema"),
        serialization_alias="schema",
    )
    minOccurs: int = 1
    maxOccurs: Optional[int] = 1
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        exclude_none = True
        populate_by_name = True

class Metadata(BaseModel):
    title: str
    role: str
    href: str

class ProcessOutput(BaseModel):
    title: str
    description: str
    scheme: Schema = Field(
        validation_alias=AliasChoices("scheme", "schema"),
        serialization_alias="schema",
    )
    metadata: List[Metadata] = []
    keywords: List[str] = []

    model_config = ConfigDict(
        populate_by_name=True,
    )


class ProcessSummary(BaseModel):
    """
    The OGC conform ProcessSummary Model.
    """

    id: str
    title: str
    version: str
    jobControlOptions: List[ProcessJobControlOptions]
    outputTransmission: List[ProcessOutputTransmission]
    links: Optional[List[Link]] = None

    class Config:
        ignore_extra = True
        exclude_none = True
        populate_by_name = True


class ProcessDescription(ProcessSummary):
    description: str
    jobControlOptions: List[ProcessJobControlOptions]
    outputTransmission: List[ProcessOutputTransmission]
    keywords: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    inputs: Dict[str, ProcessInput]
    outputs: Dict[str, ProcessOutput]

    @classmethod
    def from_yaml(cls, file_path: str) -> "ProcessDescription":
        """
        Reads a YAML file and parses it into a ProcessDescription instance.

        Args:
            file_path (str): Path to the YAML file.

        Returns:
            ProcessDescription: Parsed ProcessDescription object.
        """
        with open(file_path, "r") as yaml_file:
            yaml_data = yaml.safe_load(yaml_file)

        # Validate and parse the YAML data into the ProcessDescription model
        return cls.model_validate(yaml_data)

ProcessList = TypeAdapter(
    List[ProcessSummary]
)

class ProcessesSummary(BaseModel):
    processes: List[ProcessSummary]
    links: Optional[List[Link]] = None


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class OutputControl(BaseModel):
    transmissionMode: Literal["value", "reference"] | None = "value"
    format: dict | None = None


class ProcessExecRequestBody(BaseModel):
    inputs: Dict[str, Any]
    outputs: dict[str, OutputControl] | None = None
    mode: Optional[ExecutionMode] = ExecutionMode.ASYNC
    response: ResponseType = ResponseType.RAW


def deserialize_json(value: Any) -> Any:
    return jsonable_encoder(value)


class CalculationTask(BaseModel):
    inputs: Annotated[Dict[str, Any], AfterValidator(deserialize_json)]
    outputs: (
        Annotated[dict[str, OutputControl], AfterValidator(deserialize_json)]
        | None
    ) = None
    response: ResponseType = ResponseType.RAW

    def _hash_dict(self):
        # Include response in the cache key so different response modes
        # (e.g. "raw" vs "document") produce distinct cache entries.
        # "outputs" already carries per-output format hints when provided.
        response_value = (
            self.response.value
            if hasattr(self.response, "value")
            else str(self.response)
        )
        data = {
            "inputs": self.inputs,
            "outputs": self.outputs,
            "response": response_value,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()

    @computed_field
    @property
    def celery_key(self) -> str:
        return self._hash_dict()


class ProcessExecResponse(BaseModel):
    status: str
    jobID: str
    type: str = "process"


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
    links: List[Link] = []

    model_config = ConfigDict(
        populate_by_name=True,
    )


class JobList(BaseModel):
    jobs: List[JobStatusInfo]
    links: List[Link]


class JobStatusCode(StrEnum):
    """
    Job status codes for the OGC API Processes.
    """

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    DISMISSED = "dismissed"
