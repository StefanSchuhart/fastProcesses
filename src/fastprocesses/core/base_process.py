from abc import ABC, abstractmethod
from typing import Dict, Any
from pydantic import BaseModel

from fastprocesses.core.models import ProcessDescription

class BaseProcess(ABC):
    @abstractmethod
    def get_description(self) -> ProcessDescription:
        """
        Returns the OGC API Process description.
        
        The description must include:
        - Process ID and metadata (title, description, version)
        - Input definitions with schemas
        - Output definitions with schemas
        - Job control options (sync/async execution)
        - Output transmission methods
        
        Returns:
            ProcessDescription: Complete process description following OGC API standard
        """
        pass
    
    @abstractmethod
    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
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