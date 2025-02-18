from abc import ABC, abstractmethod
from typing import Dict, Any
from pydantic import BaseModel

class BaseProcess(ABC):
    @abstractmethod
    def get_description(self) -> Dict[str, Any]:
        """Return OGC API Process description"""
        pass
    
    @abstractmethod
    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the process"""
        pass
    
    @abstractmethod
    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate process inputs"""
        pass
