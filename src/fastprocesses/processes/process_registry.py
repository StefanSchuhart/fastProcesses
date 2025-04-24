# src/fastprocesses/processes/process_registry.py
import json
from pydoc import locate
from typing import List

import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger
from fastprocesses.core.models import ProcessDescription


class ProcessRegistry:
    """Manages the registration and retrieval of available processs (processes)."""

    def __init__(self):
        """Initializes the ProcessRegistry with Redis connection."""
        self.retry = Retry(ExponentialBackoff(cap=10, base=1), -1)
        self.redis = redis.Redis.from_url(
            str(settings.results_cache.connection),
            retry=self.retry,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError],
            health_check_interval=1,
        )
        self.registry_key = "process_registry"

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
                f"Process data to be registered:"
                f"\n{json.dumps(process_data, indent=4)}"
            )

            result = self.redis.hset(
                self.registry_key, process_id, json.dumps(process_data)
            )

            logger.debug(f"Redis hset result for registered process: {result}")

            if result == 1:
                logger.info(f"Process {process_id} registered successfully")

            if result == 0:
                logger.info(f"Process {process_id} already registered")

        except redis.RedisError as e:
            logger.error(f"Failed to write to Redis: {e}")
            raise

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
        return [key.decode("utf-8") for key in self.redis.hkeys(self.registry_key)]

    def has_process(self, process_id: str) -> bool:
        """
        Checks if a process is registered.

        Args:
            process_id (str): The ID of the process.

        Returns:
            bool: True if the process is registered, False otherwise.
        """
        logger.debug(f"Checking if process with ID {process_id} is registered")
        return self.redis.hexists(self.registry_key, process_id)

    def get_process(self, process_id: str) -> BaseProcess:
        """
        Dynamically loads and instantiates a process process:
        1. Retrieves process metadata from Redis
        2. Uses Python's module system to locate the class
        3. Instantiates a new process instance

        The locate() function dynamically imports the class based on its path.
        """
        logger.info(f"Retrieving process with ID: {process_id}")
        process_data = self.redis.hget(self.registry_key, process_id)

        if not process_data:
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        process_info = json.loads(process_data)
        process_class = locate(process_info["class_path"])

        logger.debug(
            f"Class path for Process {process_id}: {process_info['class_path']}"
        )

        if not process_class:
            logger.error(f"Process class {process_info['class_path']} not found!")
            return None

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
