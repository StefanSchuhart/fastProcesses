import json
from typing import Any

import redis
from fastapi.encoders import jsonable_encoder
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger


class TempResultCache:
    def __init__(self, key_prefix: str, ttl_hours: int):
        self._retry = Retry(ExponentialBackoff(cap=10, base=1), -1)
        self._redis = redis.Redis.from_url(
            str(settings.results_cache.connection),
            retry=self._retry,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError],
            health_check_interval=1,
        )
        self._key_prefix = key_prefix
        self._ttl_hours = ttl_hours

    def get(self, key: str) -> dict:
        logger.debug(f"Getting cache for key: {key}")
        key = self._make_key(key)
        serialized_value = self._redis.get(key)

        if serialized_value is not None:
            logger.debug(f"Received data from cache: {serialized_value[:80]}")

            return json.loads(serialized_value)

        logger.info(f"Cache miss for key: {key}")
        return None

    def put(self, key: str, value: Any) -> str:
        logger.debug(f"Putting cache for key: {key}")
        key = self._make_key(key)
        # note: exclude_none=True is used to exclude None values from the JSON serialization

        jsonable_value = jsonable_encoder(value, exclude_none=True)
        serialized_value = json.dumps(jsonable_value)
        ttl = self._ttl_hours * 60 * 60  # Convert hours to seconds
        self._redis.setex(key, ttl, serialized_value)

        return serialized_value

    def delete(self, key: str) -> None:
        logger.debug(f"Deleting cache for key: {key}")
        key = self._make_key(key)
        self._redis.delete(key)

    def _make_key(self, key: str) -> str:
        if isinstance(key, bytes):
            key = key.decode("utf-8")  # Decode bytes to string
        return f"{self._key_prefix}:{key}"

    def keys(self, pattern: str = "*") -> list[str]:
        """
        Get all keys matching the pattern.

        Args:
            pattern (str): Redis key pattern to match. Defaults to "*".

        Returns:
            list[str]: List of matching keys without the prefix
        """
        logger.debug(f"Getting keys matching pattern: {pattern}")
        full_pattern = self._make_key(pattern)
        keys = self._redis.keys(full_pattern)
        # Remove prefix from keys before returning
        prefix_len = len(self._key_prefix) + 1  # +1 for the colon
        return [key[prefix_len:] for key in keys]
