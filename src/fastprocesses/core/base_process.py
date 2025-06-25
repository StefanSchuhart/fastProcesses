import asyncio
import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable, ClassVar, Dict

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel

from fastprocesses.core.models import OutputControl, ProcessDescription
from fastprocesses.core.types import JobProgressCallback


class BaseProcess(ABC):
    process_description: ClassVar[ProcessDescription]

    def get_description(self) -> ProcessDescription:
        """
        Returns the OGC API Process description.

        Returns:
            ProcessDescription: Complete process description following OGC API standard
        """
        if not hasattr(self, "process_description"):
            raise NotImplementedError(
                f"Process class {self.__class__.__name__} must "
                "define 'process_description'"
            )
        return self.process_description

    @classmethod
    def create_description(cls, description_dict: Dict[str, Any]) -> ProcessDescription:
        """
        Creates a ProcessDescription from a dictionary.

        Args:
            description_dict (Dict[str, Any]): Dictionary containing process description

        Returns:
            ProcessDescription: Validated process description object
        """
        return ProcessDescription.model_validate(description_dict)

    @abstractmethod
    def execute(
        self,
        exec_body: Dict[str, Any],
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel | Awaitable[BaseModel]:
        """
        Executes the process with given inputs.

        Args:
            inputs (Dict[str, Any]): Input parameters matching the process description

        Returns:
            Dict[str, Any]: Output values matching the process description

        Raises:
            ValueError: If inputs are invalid
        """
        pass

    def run_execute(
        self,
        exec_body: dict,
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel:
        """
        Calls the execute method, handling both sync and async implementations.
        Always returns a BaseModel, never an awaitable.
        """
        result = self.execute(exec_body, job_progress_callback=job_progress_callback)
        if inspect.isawaitable(result):
            if asyncio.iscoroutine(result):
                return asyncio.run(result)
            else:

                async def _await_result():
                    return await result

                return asyncio.run(_await_result())
        else:
            return result

    def quick_validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Quickly checks that all required input fields are present.
        Does NOT perform deep schema validation.
        """
        description: ProcessDescription = self.get_description()
        required_inputs = description.inputs

        # Check for missing required inputs only
        for input_name, input_desc in required_inputs.items():
            if input_desc.minOccurs > 0 and input_name not in inputs:
                raise ValueError(
                    f"Missing required input '{input_name}'. "
                    f"Description: {input_desc.description}"
                )
        return True

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Validates the input data against the process description.

        Args:
            inputs (Dict[str, Any]): The input data to validate

        Returns:
            bool: True if inputs are valid

        Raises:
            ValueError: With detailed error message if validation fails
        """
        # TODO: consider using fastjsonschema for better performance
        description: ProcessDescription = self.get_description()
        required_inputs = description.inputs

        # First, check all provided inputs
        for input_name, input_value in inputs.items():
            if input_name not in required_inputs:
                raise ValueError(
                    f"Provided input '{input_name}' is "
                    "not defined in the process description."
                )
            input_desc = required_inputs[input_name]
            try:
                input_schema = input_desc.scheme.model_dump(exclude_unset=True)
                jsonschema_validate(instance=input_value, schema=input_schema)
            except JSONSchemaValidationError as e:
                raise ValueError(
                    f"Input '{input_name}' validation failed: {e.message}. "
                    f"Description: {input_desc.scheme.model_dump(exclude_unset=True)}"
                )

        # Then, check for missing required inputs
        for input_name, input_desc in required_inputs.items():
            if input_desc.minOccurs > 0 and input_name not in inputs:
                raise ValueError(
                    f"Missing required input '{input_name}'. "
                    f"Description: {input_desc.description}"
                )

        return True

    def validate_outputs(
        self, outputs: dict[str, dict[str, OutputControl]] | None
    ) -> bool:
        """
        Validates the outputs parameter against the process description.

        Args:
            outputs: Single output identifier or list of output identifiers

        Returns:
            bool: True if outputs are valid

        Raises:
            ValueError: If any output identifier is invalid
        """
        description = self.get_description()
        available_outputs = description.outputs.keys()

        if not available_outputs:
            raise ValueError("Process has no defined outputs")

        if outputs is None:
            # If no outputs specified, all outputs are considered valid
            return True

        if not isinstance(outputs, dict):
            raise ValueError("Outputs must be a dict mapping.")

        # Validate each output identifier in the outputs dict
        invalid_outputs = [
            out for out in outputs.keys() if out not in available_outputs
        ]
        if invalid_outputs:
            available = ", ".join(available_outputs)
            invalid = ", ".join(invalid_outputs)
            raise ValueError(
                f"Invalid output identifiers: {invalid}. "
                f"Available outputs are: {available}"
            )

        # Optionally, validate OutputControl objects if needed
        # for out, control in outputs.items():
        #     if not isinstance(control, dict) or not all(
        #         isinstance(v, OutputControl) for v in control.values()
        #     ):
        #         raise ValueError(
        #             f"Output '{out}' must map to a dict of OutputControl objects."
        #         )

        return True
