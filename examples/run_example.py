import asyncio
from typing import Any, Callable, Dict

from pydantic import BaseModel
import uvicorn

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
from fastprocesses.processes.process_registry import register_process

class TextModel(BaseModel):
    input_text: str | dict

    class Config:
        schema_extra = {
            "example": {
                "input_text": "Hello, World!",
                "output_text": "HELLO, WORLD!",
            }
        } 

class TextModelOut(BaseModel):
    output_text: str

    class Config:
        schema_extra = {
            "example": {
                "output_text": "HELLO, WORLD!",
            }
        }
@register_process("simple_process")
class SimpleProcess(BaseProcess):
    # Define process description as a class variable
    process_description = ProcessDescription(
        id="simple_process",
        title="Simple Procprocess_ess",
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
                scheme=Schema(type="dict", minLength=1, maxLength=1000),
            )
        },
        outputs={
            "output_text": ProcessOutput(
                title="Output Text",
                description="Processed text",
                scheme=Schema(type="string"),
            )
        },
        keywords=["text", "processing"],
        metadata={"created": "2024-02-19", "provider": "Example Organization"},
    )

    async def execute(
        self,
        exec_body: dict[str, Any],
        progress_callback: Callable[[int, str], None] | None = None
    ) -> Dict[str, Any]:

        # Report start if callback is provided
        if progress_callback:
            progress_callback(10, "Processing input")

        text_model = TextModel.model_validate(exec_body["inputs"])

        # Simulate some processing time
        if progress_callback:
            progress_callback(30, "Converting text")

        await asyncio.sleep(0.5)  # Simulate work
        output_text = text_model.input_text.get("one").upper()
        output_model = TextModelOut(output_text=output_text)

        if progress_callback:
            progress_callback(70, "Finalizing results")

        await asyncio.sleep(0.3)  # More simulated work

        if progress_callback:
            progress_callback(90, "Preparing output")

        return output_model


# Create the FastAPI app
app = OGCProcessesAPI(
    title="Simple OGC API Process - example api",
    version="1.0.0",
    description="A simple API for running processes",
).get_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        log_level=None,
    )
