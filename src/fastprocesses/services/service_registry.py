from typing import Dict, Any, List
from ..core.models import ProcessDescription

class ServiceRegistry:
    """Manages the registration and retrieval of available services (processes)."""

    def __init__(self):
        """Initializes the ServiceRegistry with an empty registry."""
        self.registry = {}

    def register_service(self, process_id: str, service: Any):
        """
        Registers a new service.

        Args:
            process_id (str): The ID of the process.
            service (Any): The service object.
        """
        self.registry[process_id] = service

    def get_service_ids(self) -> List[str]:
        """
        Retrieves the IDs of all registered services.

        Returns:
            List[str]: A list of service IDs.
        """
        return list(self.registry.keys())

    def has_service(self, process_id: str) -> bool:
        """
        Checks if a service is registered.

        Args:
            process_id (str): The ID of the process.

        Returns:
            bool: True if the service is registered, False otherwise.
        """
        return process_id in self.registry

    def get_service(self, process_id: str) -> Any:
        """
        Retrieves a registered service.

        Args:
            process_id (str): The ID of the process.

        Returns:
            Any: The service object.

        Raises:
            ValueError: If the service is not found.
        """
        if not self.has_service(process_id):
            raise ValueError(f"Service {process_id} not found!")
        return self.registry[process_id]


# Global instance of ServiceRegistry
_global_service_registry = ServiceRegistry()

def get_service_registry() -> ServiceRegistry:
    """Returns the global ServiceRegistry instance."""
    return _global_service_registry
