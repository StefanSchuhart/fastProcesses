# src/fastprocesses/processes/process_registry.py
import json
from pydoc import locate
import socket
import time
from typing import List, Optional, Type, cast
from urllib.parse import urlparse

import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from fastprocesses.core.base_process import BaseProcess
from fastprocesses.common import settings
from fastprocesses.core.exceptions import ProcessClassNotFoundError
from fastprocesses.core.logging import logger
from fastprocesses.core.models import ProcessDescription


class ProcessRegistry:
    """Manages the registration and retrieval of available processs (processes)."""

    """Redis registry with Celery-inspired connection handling."""

    def __init__(self):
        self.registry_key = "process_registry"
        self._pool: Optional[redis.ConnectionPool] = None
        self._redis: Optional[redis.Redis] = None
        
        # Connection configuration (similar to Celery's transport options)
        self.connection_config = {
            'socket_connect_timeout': 30,
            'socket_timeout': 30,
            'socket_keepalive': True,
            'health_check_interval': 30,
            'retry_on_timeout': True,
            'max_connections': 20,
        }
        
        # Retry configuration
        self.retry_config = {
            'max_retries': 100,
            'retry_on_startup': True,
            'base_delay': 1,
            'max_delay': 60,
        }

    def _create_connection_pool(self):
        """Create Redis connection pool with retry configuration."""
        retry = Retry(
            ExponentialBackoff(cap=self.retry_config['max_delay'], base=self.retry_config['base_delay']), 
            retries=3
        )
        
        self._pool = redis.ConnectionPool.from_url(
            str(settings.results_cache.connection),
            retry=retry,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError],
            **self.connection_config
        )

    def _establish_connection(self):
        """Establish Redis connection with Celery-style retry logic."""
        if not self._pool:
            self._create_connection_pool()
            
        max_retries = self.retry_config['max_retries']
        base_delay = self.retry_config['base_delay']
        
        for attempt in range(max_retries + 1):
            try:
                self._redis = redis.Redis(connection_pool=self._pool)
                
                # Test connection (similar to Celery's health check)
                self._redis.ping()
                logger.info("Redis connection established successfully")
                return
                
            except (ConnectionError, TimeoutError, ConnectionResetError, OSError) as e:
                if attempt == max_retries:
                    logger.error(f"Failed to establish Redis connection after {max_retries} attempts")
                    raise ConnectionError(f"Could not connect to Redis: {e}")
                
                # Exponential backoff (similar to Celery's retry logic)
                delay = min(base_delay * (2 ** attempt), self.retry_config['max_delay'])
                logger.warning(f"Redis connection attempt {attempt + 1} failed: {e}. Retrying in {delay}s...")
                time.sleep(delay)

    @property 
    def redis(self):
        """Lazy connection property with automatic reconnection."""
        if self._redis is None:
            self._establish_connection()
        return self._redis

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
        keys: list[bytes] = self.redis.hkeys(self.registry_key) # type: ignore

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

        return self.redis.hexists(self.registry_key, process_id) # type: ignore

    def get_process(self, process_id: str) -> BaseProcess:
        """
        Dynamically loads and instantiates a process:
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

        process_info = json.loads(process_data) # type: ignore
        logger.debug(
            f"Process data retrieved from Redis:"
            f"\n{json.dumps(process_info, indent=4)}"
        )

        process_class = cast(Type[BaseProcess], locate(process_info["class_path"]))

        logger.debug(
            f"Class path for Process {process_id}: {process_info['class_path']}"
        )

        if not process_class:
            logger.error(f"Process class {process_info['class_path']} not found!")
            raise ProcessClassNotFoundError(process_info['class_path'])

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
