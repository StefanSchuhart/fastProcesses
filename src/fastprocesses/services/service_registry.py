# src/fastprocesses/services/service_registry.py
import json
from pydoc import locate
from typing import Any, Dict, List

import redis

from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger


class ProcessRegistry:
    """Manages the registration and retrieval of available services (processes)."""

    def __init__(self):
        """Initializes the ProcessRegistry with Redis connection."""
        self.redis = redis.Redis.from_url(str(settings.redis_cache_url))
        self.registry_key = "service_registry"

    def register_service(self, process_id: str, service: BaseProcess):
        """
        Registers a new service.

        Args:
            process_id (str): The ID of the process.
            service (BaseProcess): The service object.
        """
        logger.info(f"Registering service with ID: {process_id}")
        service_data = {
            "description": service.get_description(),
            "class_path": f"{service.__module__}.{service.__class__.__name__}"
        }
        logger.debug(f"Service data to be registered: {service_data}")
        self.redis.hset(self.registry_key, process_id, json.dumps(service_data))

    def get_service_ids(self) -> List[str]:
        """
        Retrieves the IDs of all registered services.

        Returns:
            List[str]: A list of service IDs.
        """
        logger.debug("Retrieving all registered service IDs")
        return [key.decode('utf-8') for key in self.redis.hkeys(self.registry_key)]

    def has_service(self, process_id: str) -> bool:
        """
        Checks if a service is registered.

        Args:
            process_id (str): The ID of the process.

        Returns:
            bool: True if the service is registered, False otherwise.
        """
        logger.debug(f"Checking if service with ID {process_id} is registered")
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
        logger.info(f"Retrieving service with ID: {process_id}")
        service_data = self.redis.hget(self.registry_key, process_id)
        if not service_data:
            logger.error(f"Service {process_id} not found!")
            raise ValueError(f"Service {process_id} not found!")
        service_info = json.loads(service_data)
        service_class = locate(service_info["class_path"])
        if not service_class:
            logger.error(f"Service class {service_info['class_path']} not found!")
            raise ValueError(f"Service class {service_info['class_path']} not found!")
        return service_class()

# Global instance of ProcessRegistry
_global_process_registry = ProcessRegistry()

def get_process_registry() -> ProcessRegistry:
    """Returns the global ProcessRegistry instance."""
    return _global_process_registry

def register_process(process_id: str):
    """Decorator to register a process implicitly."""
    def decorator(cls):
        get_process_registry().register_service(process_id, cls())
        return cls
    return decorator
