# src/fastprocesses/processes/process_registry.py
import json
import typing
from pydoc import locate
from typing import List, Type, cast

from fastprocesses.common import settings
from fastprocesses.core.base_process import BaseProcess
from fastprocesses.core.exceptions import ProcessClassNotFoundError, ProcessRegistrationError
from fastprocesses.core.logging import logger
from fastprocesses.core.models import ProcessDescription
from fastprocesses.core.output_protocol import BaseProcessResult
from fastprocesses.core.output_schema_resolver import _media_type_from_schema
from fastprocesses.core.redis_connection import RedisConnection


def _validate_process_registration(
    process_id: str,
    process: BaseProcess,
    description: ProcessDescription,
) -> None:
    """Validate that a BaseProcessResult-returning process is correctly wired.

    Raises ProcessRegistrationError if:
    - execute()'s return annotation is not a BaseProcessResult subclass
    - any output ID in the description is missing as a field on the result model
    - any media type advertised by the description lacks an output_serializers entry
    """
    try:
        hints = typing.get_type_hints(type(process).execute)
    except Exception:
        return  # can't introspect — skip validation

    return_type = hints.get("return")
    if return_type is None or not (
        isinstance(return_type, type) and issubclass(return_type, BaseProcessResult)
    ):
        # Legacy process (plain BaseModel or no annotation) — no validation needed
        return

    model_fields = set(return_type.model_fields.keys())
    serializers: dict[str, dict[str, str]] = return_type.output_serializers

    for output_id, output_desc in (description.outputs or {}).items():
        # 1. Result model must have a field for every declared output
        if output_id not in model_fields:
            raise ProcessRegistrationError(
                process_id,
                f"output '{output_id}' is declared in the process description but "
                f"has no corresponding field on {return_type.__name__}",
            )

        # 2. Every advertised media type must have an output_serializers entry
        schema = output_desc.schema_
        if schema is None:
            continue

        branches = schema.oneOf if schema.oneOf else [schema]
        for branch in branches:
            media_type = _media_type_from_schema(branch)
            if media_type is None:
                continue
            available = serializers.get(output_id, {})
            if media_type not in available:
                raise ProcessRegistrationError(
                    process_id,
                    f"output '{output_id}' advertises media type '{media_type}' but "
                    f"{return_type.__name__}.output_serializers has no entry for it. "
                    f"Available: {list(available.keys()) or 'none'}",
                )


class ProcessRegistry:
    """Manages the registration and retrieval of available processs (processes)."""

    def __init__(self, redis_connection: RedisConnection | None = None):
        self.registry_key = f"process_registry:{settings.FP_CELERY_QUEUE}"
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

            # Validate the process class is correctly wired before persisting
            _validate_process_registration(process_id, process, description)

            # serialize the description
            description_dict = description.model_dump(exclude_none=True)
            process_data = {
                "description": description_dict,
                "class_path": f"{process.__module__}.{process.__class__.__name__}",
            }
            logger.debug(
                f"Process data to be registered:\n{json.dumps(process_data, indent=4)[:50]}"
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
            f"Process data retrieved from Redis for {process_id}."
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
