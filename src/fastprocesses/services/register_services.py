from typing import Any, Dict
from fastprocesses.services.service_registry import ServiceRegistry
from fastprocesses.core.base_process import BaseProcess

class ExampleProcess(BaseProcess):
    def get_description(self) -> Dict[str, Any]:
        return {
            "id": "example_process",
            "title": "Example Process",
            "description": "An example process",
            "version": "1.0.0",
            "inputs": {"input1": {"type": "string"}},
            "outputs": {"output1": {"type": "string"}}
        }

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return {"output1": f"Processed {inputs['input1']}"}

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "input1" in inputs

def register_services():
    registry = ServiceRegistry()
    registry.register_service("example_process", ExampleProcess())
