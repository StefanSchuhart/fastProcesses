from typing import Any, Dict

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
                scheme=Schema(type="string", minLength=1, maxLength=1000),
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

    async def execute(self, exec_body: dict[str, Any]) -> Dict[str, Any]:
        input_text = exec_body["inputs"]["input_text"]
        output_text = input_text.upper()
        return {"output_text": output_text}


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
