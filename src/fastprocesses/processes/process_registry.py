# src/fastprocesses/processes/process_registry.py
import json
from pydoc import locate
from typing import List, Type, cast

from fastprocesses.common import settings
from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.exceptions import ProcessClassNotFoundError
from fastprocesses.core.logging import logger
from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.redis_connection import RedisConnection


class ProcessRegistry:
    """Manages the registration and retrieval of available processs (processes)."""

    def __init__(self, redis_connection: RedisConnection | None = None):
        self.registry_key = "process_registry"
        if redis_connection is None:
            redis_connection = RedisConnection(str(settings.results_cache.connection))
        self.redis_connection = redis_connection

    @property
    def redis(self):
        return self.redis_connection.client

    def register_process(self, process_id: str, process: BaseProcess):
        """
        Registers a process process in Redis:
        - Stores process description and class path for dynamic loading
        - Uses Redis hash structure for efficient lookups
        - Enables process discovery and instantiation
        """
        try:
            description: ProcessDescription = process.get_description()

            # serialize the description
            description_dict = description.model_dump(exclude_none=True)
            process_data = {
                "description": description_dict,
                "class_path": f"{process.__module__}.{process.__class__.__name__}",
            }
            logger.debug(
                f"Process data to be registered:\n{json.dumps(process_data, indent=4)}"
            )

            result = self.redis_connection._execute_redis_command(
                'hset', 
                self.registry_key, 
                process_id, 
                json.dumps(process_data)
            )

            logger.debug(f"Redis hset result for registered process: {result}")

            if result == 1:
                logger.info(f"Process {process_id} registered successfully")

            if result == 0:
                logger.info(f"Process {process_id} already registered")

        except Exception as e:
            logger.error(f"Failed to register process {process_id}: {e}")
            raise

    def get_process_ids(self) -> List[str]:
        """
        Retrieves the IDs of all registered processes.

        Returns:
            List[str]: A list of process IDs.
        """
        logger.debug("Retrieving all registered process IDs")
        keys: list[bytes] = self.redis_connection._execute_redis_command("hkeys",self.registry_key)  # type: ignore

        return [key.decode("utf-8") for key in keys]

    def has_process(self, process_id: str) -> bool:
        """
        Checks if a process is registered.

        Args:
            process_id (str): The ID of the process.

        Returns:
            bool: True if the process is registered, False otherwise.
        """
        logger.debug(f"Checking if process with ID {process_id} is registered")

        return self.redis_connection._execute_redis_command(
            'hexists', 
            self.registry_key, 
            process_id
        )

    def get_process(self, process_id: str) -> BaseProcess:
        """
        Dynamically loads and instantiates a process:
        1. Retrieves process metadata from Redis
        2. Uses Python's module system to locate the class
        3. Instantiates a new process instance

        The locate() function dynamically imports the class based on its path.
        """
        logger.info(f"Retrieving process with ID: {process_id}")
        process_data = self.redis_connection._execute_redis_command(
            'hget', 
            self.registry_key, 
            process_id
        )

        if not process_data:
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        process_info = json.loads(process_data)  # type: ignore
        logger.debug(
            f"Process data retrieved from Redis:\n{json.dumps(process_info, indent=4)}"
        )

        process_class = cast(Type[BaseProcess], locate(process_info["class_path"]))

        logger.debug(
            f"Class path for Process {process_id}: {process_info['class_path']}"
        )

        if not process_class:
            logger.error(f"Process class {process_info['class_path']} not found!")
            raise ProcessClassNotFoundError(process_info["class_path"])

        return process_class()


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
        if not hasattr(cls, "process_description"):
            raise ValueError(
                f"Process {cls.__name__} must define a 'description' class variable"
            )
        get_process_registry().register_process(process_id, cls())
        return cls

    return decorator
