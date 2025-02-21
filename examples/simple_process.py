from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.models import ProcessDescription, ProcessInput, ProcessJobControlOptions, ProcessOutput, ProcessOutputTransmission, Schema
from fastprocesses.processes.process_registry import register_process
from typing import Dict, Any

@register_process("simple_process")
class SimpleProcess(BaseProcess):
    def get_description(self) -> ProcessDescription:
        return ProcessDescription(
            id="simple_process",
            title="Simple Process",
            version="1.0.0",
            description="A simple example process",
            jobControlOptions=[
                ProcessJobControlOptions.SYNC_EXECUTE,
                ProcessJobControlOptions.ASYNC_EXECUTE
            ],
            outputTransmission=[
                ProcessOutputTransmission.VALUE
            ],
            inputs={
                "input_text": ProcessInput(
                    title="Input Text",
                    description="Text to process",
                    schema=Schema(
                        type="string",
                        minLength=1,
                        maxLength=1000
                    )
                )
            },
            outputs={
                "output_text": ProcessOutput(
                    title="Output Text",
                    description="Processed text",
                    schema=Schema(
                        type="string"
                    )
                )
            },
            keywords=["text", "processing"],
            metadata={
                "created": "2024-02-19",
                "provider": "Example Organization"
            }
        )

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        input_text = inputs["input_text"]
        output_text = input_text.upper()
        return {"output_text": output_text}