from fastprocesses.core.base_process import BaseProcess
from typing import Dict, Any

class SimpleProcess(BaseProcess):
    def get_description(self) -> Dict[str, Any]:
        return {
            "id": "simple_process",
            "title": "Simple Process",
            "version": "1.0.0",
            "description": "A simple example process",
            "inputs": {
                "input_text": {
                    "title": "Input Text",
                    "description": "Text to process",
                    "schema": {
                        "type": "string"
                    },
                    "minOccurs": 1,
                    "maxOccurs": 1
                }
            },
            "outputs": {
                "output_text": {
                    "title": "Output Text",
                    "description": "Processed text",
                    "schema": {
                        "type": "string"
                    }
                }
            }
        }

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "input_text" in inputs

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        input_text = inputs["input_text"]
        output_text = input_text.upper()
        return {"output_text": output_text}
