import zlib
from typing import Any
from pydantic import RedisDsn

import orjson
from fastapi.encoders import jsonable_encoder

from fastprocesses.core.exceptions import ResultTooLargeError
from fastprocesses.core.logging import logger
from fastprocesses.core.redis_connection import RedisConnection

# Default unconditional read-side ceiling, used when a TempResultCache isn't
# constructed with an explicit hard_read_ceiling_bytes. Independent of
# max_size_bytes: protects against ever decoding/parsing a value large enough
# to amplify into hundreds of MB during zlib/orjson decode.
_DEFAULT_HARD_READ_CEILING_BYTES = 50 * 1024 * 1024  # 50 MiB


class TempResultCache:
    def __init__(
        self,
        key_prefix: str,
        ttl_days: int,
        connection: str | RedisDsn | None = None,
        redis_connection: RedisConnection | None = None,
        max_size_bytes: int | None = None,
        hard_read_ceiling_bytes: int | None = _DEFAULT_HARD_READ_CEILING_BYTES,
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
        self._max_size_bytes = max_size_bytes
        self._hard_read_ceiling_bytes = hard_read_ceiling_bytes

    @property
    def _redis(self):
        return self.redis_connection.client

    def get(self, key: str) -> dict | None:
        logger.debug(f"Getting cache for key: {key}")
        made_key = self._make_key(key)

        size = self.redis_connection._execute_redis_command("strlen", made_key)
        logger.info(f"Cache entry size for key {made_key}: {size} bytes")

        if (
            self._hard_read_ceiling_bytes is not None
            and size
            and size > self._hard_read_ceiling_bytes
        ):
            logger.error(
                f"Refusing to read oversized cache entry for key {made_key}: "
                f"{size} bytes exceeds hard ceiling of "
                f"{self._hard_read_ceiling_bytes} bytes"
            )
            raise ResultTooLargeError(made_key, size, self._hard_read_ceiling_bytes)

        serialized_value = self.redis_connection._execute_redis_command("get", made_key)

        if isinstance(serialized_value, (bytes, str)):
            logger.debug(f"Received data from cache: {str(serialized_value)[:80]}")
            # zlib.decompress works directly on bytes; no bytes->str->dict hop.
            raw = serialized_value.encode("utf-8") if isinstance(serialized_value, str) else serialized_value
            return orjson.loads(zlib.decompress(raw))
        logger.info(f"Cache miss for key: {made_key}")
        return None

    def put(self, key: str, value: Any) -> bytes:
        logger.debug(f"Putting cache for key: {key}")
        made_key = self._make_key(key)
        jsonable_value = jsonable_encoder(value, exclude_none=True)
        serialized_value = zlib.compress(orjson.dumps(jsonable_value))

        if (
            self._max_size_bytes is not None
            and len(serialized_value) > self._max_size_bytes
        ):
            raise ResultTooLargeError(
                made_key, len(serialized_value), self._max_size_bytes
            )

        ttl = self._ttl_days * 24 * 60 * 60  # Convert days to seconds

        self.redis_connection._execute_redis_command(
            "setex", made_key, ttl, serialized_value
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
