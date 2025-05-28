import asyncio
from typing import Any, Callable

import uvicorn
from pydantic import BaseModel

from fastprocesses.api.server import OGCProcessesAPI
from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.models import (
    ProcessDescription,
    ProcessInput,
    ProcessJobControlOptions,
    ProcessOutput,
    ProcessOutputTransmission,
    Schema,
)
from fastprocesses.core.types import JobProgressCallback
from fastprocesses.processes.process_registry import register_process


class TextModel(BaseModel):
    input_text: str

    class Config:
        json_schema_extra = {
            "example": {
                "input_text": "Hello, World!",
                "output_text": "HELLO, WORLD!",
            }
        } 

class TextModelOut(BaseModel):
    upper: str | None = None
    lower: str | None = None

    class Config:
        json_schema_extra = {
            "example": {
                "output_text": "HELLO, WORLD!",
            }
        }
@register_process("simple_process")
class SimpleProcess(BaseProcess):
    # Define process description as a class variable,
    # you can load it from a YAML file
    process_description = ProcessDescription.from_yaml(
            "examples/run_example.yaml"
    )

    async def execute(
        self,
        exec_body: dict[str, dict],
        job_progress_callback: JobProgressCallback | None = None
    ) -> BaseModel:

        # Report start if callback is provided

        if job_progress_callback:
            job_progress_callback(10, "Processing input")

        text_model = TextModel.model_validate(exec_body["inputs"])

        # Simulate some processing time
        if job_progress_callback:
            job_progress_callback(30, "Converting text")

        await asyncio.sleep(5)  # Simulate work

        output = {}
        if "upper" in exec_body["outputs"].keys():
            output["upper"] = text_model.input_text.upper()

        if "lower" in exec_body["outputs"].keys():
            output["lower"] = text_model.input_text.lower()

        output_model = TextModelOut.model_validate(output)

        if job_progress_callback:
            job_progress_callback(70, "Finalizing results")

        await asyncio.sleep(0.3)  # More simulated work

        if job_progress_callback:
            job_progress_callback(90, "Preparing output")

        # raise Exception("This is a test exception")

        return output_model

@register_process("simple_process_2")
class SimpleProcess_2(BaseProcess):
    # Define process description as a class variable
    process_description = ProcessDescription(
        id="simple_process_2",
        title="Simple Process",
        version="1.0.0",
        description="A simple example process",
        jobControlOptions=[
            ProcessJobControlOptions.SYNC_EXECUTE,
            ProcessJobControlOptions.ASYNC_EXECUTE,
        ],
        outputTransmission=[ProcessOutputTransmission.VALUE],
        inputs={
            "input_text": ProcessInput(
                title="Input Text",
                description="Text to process",
                schema=Schema(type="string", minLength=1, maxLength=10),
            )
        },
        outputs={
            "output_text": ProcessOutput(
                title="Output Text",
                description="Processed text",
                schema=Schema(type="string"),
            )
        },
        keywords=["text", "processing"],
        metadata={"created": "2024-02-19", "provider": "Example Organization"},
    )

    async def execute(
        self,
        exec_body: dict[str, Any],
        job_progress_callback: Callable[[int, str], None] | None = None
    ) -> BaseModel:

        # Report start if callback is provided
        if job_progress_callback:
            job_progress_callback(10, "Processing input")

        text_model = TextModel.model_validate(exec_body["inputs"])

        # Simulate some processing time
        if job_progress_callback:
            job_progress_callback(30, "Converting text")

        await asyncio.sleep(0.5)  # Simulate work
        output_text = text_model.input_text.upper()
        output_model = TextModelOut(upper=output_text)

        if job_progress_callback:
            job_progress_callback(70, "Finalizing results")

        await asyncio.sleep(0.3)  # More simulated work

        if job_progress_callback:
            job_progress_callback(90, "Preparing output")

        return output_model

# Create the FastAPI app
app = OGCProcessesAPI(
    contact={
        "name": "LGV Hamburg",
        "url": "https://example.com",
        "email": "support@support.com",
    },
    license={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0",
    },
    terms_of_service="https://example.com/terms",
).get_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        log_level="DEBUG",
    )
