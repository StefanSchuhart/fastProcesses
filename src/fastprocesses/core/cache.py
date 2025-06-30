import json
from typing import Any
from pydantic import RedisDsn

from fastapi.encoders import jsonable_encoder

from fastprocesses.core.logging import logger
from fastprocesses.core.redis_connection import RedisConnection


class TempResultCache:
    def __init__(
        self,
        key_prefix: str,
        ttl_days: int,
        connection: str | RedisDsn | None = None,
        redis_connection: RedisConnection | None = None,
    ):
        if redis_connection is None:
            if connection is None:
                raise ValueError(
                    "Either redis_connection or connection string must be provided."
                )
            redis_connection = RedisConnection(str(connection))
        self.redis_connection = redis_connection
        self._key_prefix = key_prefix
        self._ttl_days = ttl_days

    @property
    def _redis(self):
        return self.redis_connection.client

    def get(self, key: str) -> dict | None:
        logger.debug(f"Getting cache for key: {key}")
        key = self._make_key(key)

        serialized_value = self.redis_connection._execute_redis_command("get", key)

        if serialized_value is not None and hasattr(serialized_value, "decode"):
            logger.debug(f"Received data from cache: {str(serialized_value)[:80]}")
            return json.loads(serialized_value.decode("utf-8"))
        elif isinstance(serialized_value, bytes):
            logger.debug(f"Received data from cache: {str(serialized_value)[:80]}")
            return json.loads(serialized_value.decode("utf-8"))
        elif isinstance(serialized_value, str):
            logger.debug(f"Received data from cache: {serialized_value[:80]}")
            return json.loads(serialized_value)
        logger.info(f"Cache miss for key: {key}")
        return None

    def put(self, key: str, value: Any) -> str:
        logger.debug(f"Putting cache for key: {key}")
        key = self._make_key(key)
        jsonable_value = jsonable_encoder(value, exclude_none=True)
        serialized_value = json.dumps(jsonable_value)
        ttl = self._ttl_days * 24 * 60 * 60  # Convert days to seconds

        self.redis_connection._execute_redis_command(
            "setex", key, ttl, serialized_value
        )

        return serialized_value

    def delete(self, key: str) -> None:
        logger.debug(f"Deleting cache for key: {key}")
        key = self._make_key(key)

        self.redis_connection._execute_redis_command("delete", key)

    def _make_key(self, key: str) -> str:
        if isinstance(key, bytes):
            key = key.decode("utf-8")  # Decode bytes to string

        return f"{self._key_prefix}:{key}"

    def keys(self, pattern: str = "*") -> list[str]:
        logger.debug(f"Getting keys matching pattern: {pattern}")
        full_pattern = self._make_key(pattern)

        keys = self.redis_connection._execute_redis_command("keys", full_pattern)

        prefix_len = len(self._key_prefix) + 1  # +1 for the colon
        return [key.decode("utf-8")[prefix_len:] for key in keys]
