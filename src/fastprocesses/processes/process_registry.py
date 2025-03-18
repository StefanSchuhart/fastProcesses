# src/fastprocesses/services/service_registry.py
import json
from pydoc import locate
from typing import List

import redis

from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger
from fastprocesses.core.models import ProcessDescription


class ProcessRegistry:
    """Manages the registration and retrieval of available services (processes)."""

    def __init__(self):
        """Initializes the ProcessRegistry with Redis connection."""
        self.redis = redis.Redis.from_url(str(settings.redis_cache_url))
        self.registry_key = "service_registry"

    def register_service(self, process_id: str, service: BaseProcess):
        """
        Registers a process service in Redis:
        - Stores process description and class path for dynamic loading
        - Uses Redis hash structure for efficient lookups
        - Enables service discovery and instantiation
        """
        try:
            description: ProcessDescription = service.get_description()
            
            # serialize the description
            description_dict = description.model_dump(exclude_none=True)
            service_data = {
                "description": description_dict,
                "class_path": f"{service.__module__}.{service.__class__.__name__}"
            }
            logger.debug(f"Service data to be registered: {service_data}")
            self.redis.hset(self.registry_key, process_id, json.dumps(service_data))
        except Exception as e:
            logger.error(f"Failed to register service {process_id}: {e}")
            raise

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
        Dynamically loads and instantiates a process service:
        1. Retrieves service metadata from Redis
        2. Uses Python's module system to locate the class
        3. Instantiates a new service instance
        
        The locate() function dynamically imports the class based on its path.
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
            # raise ValueError(f"Service class {service_info['class_path']} not found!")
            
        return service_class()

# Global instance of ProcessRegistry
_global_process_registry = ProcessRegistry()

def get_process_registry() -> ProcessRegistry:
    """Returns the global ProcessRegistry instance."""
    return _global_process_registry

def register_process(process_id: str):
    """
    Decorator for automatic process registration.
    Allows processes to self-register by simply using @register_process decorator.
    Example:
        @register_process("my_process")
        class MyProcess(BaseProcess):
            ...
    """
    def decorator(cls):
        if not hasattr(cls, 'process_description'):
            raise ValueError(f"Process {cls.__name__} must define a 'description' class variable")
        get_process_registry().register_service(process_id, cls())
        return cls
    return decorator

