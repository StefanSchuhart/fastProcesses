from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, List

from pydantic import BaseModel

from fastprocesses.core.models import ProcessDescription


class BaseProcess(ABC):
    process_description: ClassVar[ProcessDescription]

    def get_description(self) -> ProcessDescription:
        """
        Returns the OGC API Process description.
        
        Returns:
            ProcessDescription: Complete process description following OGC API standard
        """
        if not hasattr(self, 'process_description'):
            raise NotImplementedError(
                f"Process class {self.__class__.__name__} must define 'process_description'"
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
    async def execute(self, exec_body: Dict[str, Any]) -> Dict[str, Any]:
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
        description = self.get_description()
        required_inputs = description.get("inputs", {})
        
        # Check for missing required inputs
        for input_name, input_desc in required_inputs.items():
            if input_desc.get("minOccurs", 0) > 0 and input_name not in inputs:
                raise ValueError(
                    f"Missing required input '{input_name}'. "
                    f"Description: {input_desc.get('description', 'No description available')}"
                )
            
            # Validate input type if schema is provided
            if input_name in inputs and "schema" in input_desc:
                expected_type = input_desc["schema"].get("type")
                if expected_type == "string" and not isinstance(inputs[input_name], str):
                    raise ValueError(
                        f"Invalid type for input '{input_name}'. "
                        f"Expected string, got {type(inputs[input_name]).__name__}. "
                        f"Description: {input_desc.get('description', 'No description available')}"
                    )
                elif expected_type == "number" and not isinstance(inputs[input_name], (int, float)):
                    raise ValueError(
                        f"Invalid type for input '{input_name}'. "
                        f"Expected number, got {type(inputs[input_name]).__name__}. "
                        f"Description: {input_desc.get('description', 'No description available')}"
                    )
                # Add more type validations as needed
        
        return True
    
    def validate_outputs(self, outputs: str | List[str]) -> bool:
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
        available_outputs = description.get("outputs", {}).keys()
        
        if not available_outputs:
            raise ValueError("Process has no defined outputs")
        
        # Convert single string to list for uniform handling
        output_list = [outputs] if isinstance(outputs, str) else outputs
        
        if not output_list:
            # If no outputs specified, all outputs are considered valid
            return True
        
        # Validate each output identifier
        invalid_outputs = [out for out in output_list if out not in available_outputs]
        if invalid_outputs:
            available = ", ".join(available_outputs)
            invalid = ", ".join(invalid_outputs)
            raise ValueError(
                f"Invalid output identifiers: {invalid}. "
                f"Available outputs are: {available}"
            )
        
        return True