import json

import redis
from fastapi.encoders import jsonable_encoder

from fastprocesses.core.config import settings
from fastprocesses.core.logging import logger


class Cache:
    def __init__(self, key_prefix: str, ttl_days: int):
        self._redis = redis.Redis.from_url(str(settings.redis_cache_url), decode_responses=True)
        self._key_prefix = key_prefix
        self._ttl_days = ttl_days

    def get(self, key: str) -> dict:
        logger.debug(f"Getting cache for key: {key}")
        key = self._make_key(key)
        serialized_value = self._redis.get(key)
        
        logger.debug(f"{serialized_value}")
        return None if serialized_value is None else json.loads(serialized_value)

    def put(self, key: str, value: dict) -> None:
        logger.debug(f"Putting cache for key: {key}")
        key = self._make_key(key)
        jsonable_value = jsonable_encoder(value)
        serialized_value = json.dumps(jsonable_value)
        ttl = self._ttl_days * 86400
        self._redis.setex(key, ttl, serialized_value)

    def delete(self, key: str) -> None:
        logger.debug(f"Deleting cache for key: {key}")
        key = self._make_key(key)
        self._redis.delete(key)

    def _make_key(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"