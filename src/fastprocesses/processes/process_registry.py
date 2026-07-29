# src/fastprocesses/processes/process_registry.py
import json
import typing
from pydoc import locate
from typing import List, Type, cast

import redis.exceptions

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
    ) or return_type is BaseProcessResult:
        # No annotation, not a BaseProcessResult subclass, or the abstract base
        # itself (inherited from BaseParallelProcess / BaseScatterProcess) —
        # no validation needed.
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
        schema = output_desc.scheme
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
    """Manages the registration and retrieval of available processes.

    The in-process dict ``_local`` is the primary store and is always
    authoritative on worker pods (where ``@register_process`` decorators
    run at import time).  Redis is a secondary sync so that API pods —
    which don't import process modules — can still list and look up
    processes registered by workers.

    Reads prefer ``_local`` and only fall back to Redis when the local
    store is empty, which is the normal state on a pure-API pod.  This
    means the registry stays fully functional even when Redis is
    unavailable or OOM.
    """

    def __init__(self, redis_connection: RedisConnection | None = None):
        self.registry_key = f"process_registry:{settings.FP_CELERY_QUEUE}"
        if redis_connection is None:
            redis_connection = RedisConnection(str(settings.results_cache.connection))
        self.redis_connection = redis_connection
        self._local: dict[str, dict] = {}

    @property
    def redis(self):
        return self.redis_connection.client

    def register_process(self, process_id: str, process: BaseProcess):
        """
        Registers a process in the local store and syncs to Redis.

        The local store is always written first so that the process is
        immediately available regardless of Redis health.  The Redis sync
        is best-effort: infrastructure errors (OOM, connection refused)
        are logged but do not prevent the process from being usable.
        """
        description: ProcessDescription = process.get_description()

        # Validate the process class is correctly wired before persisting
        _validate_process_registration(process_id, process, description)

        description_dict = description.model_dump(exclude_none=True)
        process_data = {
            "description": description_dict,
            "class_path": f"{process.__module__}.{process.__class__.__name__}",
        }
        logger.debug(
            f"Process data to be registered:\n{json.dumps(process_data, indent=4)[:50]}"
        )

        self._local[process_id] = process_data
        logger.info(f"Process {process_id} registered locally")

        try:
            result = self.redis_connection._execute_redis_command(
                'hset',
                self.registry_key,
                process_id,
                json.dumps(process_data)
            )
            logger.debug(f"Redis hset result for registered process: {result}")
        except redis.exceptions.RedisError as e:
            # Redis infrastructure errors (e.g. OOM, connection refused) must not
            # prevent the process from being usable — it is already in _local.
            logger.error(
                f"Failed to sync process {process_id} to Redis "
                f"(still available locally): {e}"
            )

    def get_process_ids(self) -> List[str]:
        """
        Retrieves the IDs of all registered processes.

        Returns the local store when populated (worker pods); falls back
        to Redis for API pods that don't run @register_process decorators.
        """
        logger.debug("Retrieving all registered process IDs")
        if self._local:
            return list(self._local.keys())

        keys: list[bytes] = self.redis_connection._execute_redis_command(  # type: ignore
            "hkeys", self.registry_key
        )
        return [key.decode("utf-8") for key in keys]

    def has_process(self, process_id: str) -> bool:
        """
        Checks if a process is registered.
        """
        logger.debug(f"Checking if process with ID {process_id} is registered")
        if self._local:
            return process_id in self._local

        return self.redis_connection._execute_redis_command(
            'hexists',
            self.registry_key,
            process_id
        )

    def get_process(self, process_id: str) -> BaseProcess:
        """
        Dynamically loads and instantiates a process.

        Resolves from the local store when populated; falls back to Redis
        for API pods.  Uses Python's module system to locate and
        instantiate the class from its stored dotted path.
        """
        logger.info(f"Retrieving process with ID: {process_id}")

        if self._local:
            process_info = self._local.get(process_id)
        else:
            raw = self.redis_connection._execute_redis_command(
                'hget',
                self.registry_key,
                process_id
            )
            process_info = json.loads(raw) if raw else None  # type: ignore

        if not process_info:
            logger.error(f"Process {process_id} not found!")
            raise ValueError(f"Process {process_id} not found!")

        logger.debug(f"Process data retrieved for {process_id}.")

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
