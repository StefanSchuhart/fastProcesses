import redis
import json
from typing import Dict, Any, List
from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.config import settings
from fastprocesses.core.base_process import BaseProcess
from pydoc import locate

class ServiceRegistry:
    """Manages the registration and retrieval of available services (processes)."""

    def __init__(self):
        """Initializes the ServiceRegistry with Redis connection."""
        self.redis = redis.Redis.from_url(str(settings.redis_cache_url))
        self.registry_key = "service_registry"

    def register_service(self, process_id: str, service: BaseProcess):
        """
        Registers a new service.

        Args:
            process_id (str): The ID of the process.
            service (BaseProcess): The service object.
        """
        service_data = {
            "description": service.get_description(),
            "class_path": f"{service.__module__}.{service.__class__.__name__}"
        }
        self.redis.hset(self.registry_key, process_id, json.dumps(service_data))

    def get_service_ids(self) -> List[str]:
        """
        Retrieves the IDs of all registered services.

        Returns:
            List[str]: A list of service IDs.
        """
        return [key.decode('utf-8') for key in self.redis.hkeys(self.registry_key)]

    def has_service(self, process_id: str) -> bool:
        """
        Checks if a service is registered.

        Args:
            process_id (str): The ID of the process.

        Returns:
            bool: True if the service is registered, False otherwise.
        """
        return self.redis.hexists(self.registry_key, process_id)

    def get_service(self, process_id: str) -> BaseProcess:
        """
        Retrieves a registered service.

        Args:
            process_id (str): The ID of the process.

        Returns:
            BaseProcess: The service object.

        Raises:
            ValueError: If the service is not found.
        """
        service_data = self.redis.hget(self.registry_key, process_id)
        if not service_data:
            raise ValueError(f"Service {process_id} not found!")
        service_info = json.loads(service_data)
        service_class = locate(service_info["class_path"])
        if not service_class:
            raise ValueError(f"Service class {service_info['class_path']} not found!")
        return service_class()

# Global instance of ServiceRegistry
_global_service_registry = ServiceRegistry()

def get_service_registry() -> ServiceRegistry:
    """Returns the global ServiceRegistry instance."""
    return _global_service_registry