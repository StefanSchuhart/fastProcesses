import time
from socket import socket
from typing import Optional

import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from fastprocesses.core.logging import logger


class RedisConnection:
    """Unified Redis connection handler with bounded retry and reconnection logic.

    The defaults here are tuned to avoid blocking API worker threads for
    excessively long periods when Redis is unavailable. If you need different
    behavior, pass a custom ``retry_config`` when instantiating this class.
    """

    def __init__(
        self,
        url: str,
        connection_config: Optional[dict] = None,
        retry_config: Optional[dict] = None,
    ):
        self._pool: Optional[redis.ConnectionPool] = None
        self._redis: Optional[redis.Redis] = None
        self.url = url
        # Connection-level timeouts kept reasonably small to avoid long blocks
        self.connection_config = connection_config or {
            "socket_connect_timeout": 5,
            "socket_timeout": 5,
            "socket_keepalive": True,
            "health_check_interval": 30,
            "retry_on_timeout": True,
            "max_connections": 20,
        }
        # Bounded retry configuration so total wait time stays manageable
        self.retry_config = retry_config or {
            "max_retries": 5,
            "retry_on_startup": True,
            "base_delay": 0.5,
            "max_delay": 5,
        }
        self.connection_errors = (
            ConnectionError,
            TimeoutError,
            ConnectionResetError,
            OSError,
            IOError,
            EOFError,
        )

    def _create_connection_pool(self):
        retry = Retry(
            ExponentialBackoff(
                cap=self.retry_config["max_delay"],
                base=self.retry_config["base_delay"],
            ),
            retries=self.retry_config["max_retries"],
        )
        self._pool = redis.ConnectionPool.from_url(
            self.url,
            retry=retry,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError],
            **self.connection_config,
        )

    def _establish_connection(self):
        if not self._pool:
            self._create_connection_pool()
        max_retries = self.retry_config["max_retries"]
        base_delay = self.retry_config["base_delay"]

        start = time.monotonic()
        for attempt in range(max_retries + 1):
            try:
                self._redis = redis.Redis(connection_pool=self._pool)
                self._redis.ping()
                elapsed = time.monotonic() - start
                logger.info(
                    "Redis connection established successfully after "
                    f"{attempt + 1} attempt(s) in {elapsed:.2f}s"
                )
                return
            except (ConnectionError, TimeoutError, ConnectionResetError, OSError) as e:
                if attempt == max_retries:
                    elapsed = time.monotonic() - start
                    logger.error(
                        "Failed to establish Redis connection after "
                        f"{max_retries} attempts (waited {elapsed:.2f}s): {e}"
                    )
                    raise ConnectionError(f"Could not connect to Redis: {e}")
                delay = min(base_delay * (2**attempt), self.retry_config["max_delay"])
                logger.warning(
                    f"Redis connection attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)

    @property
    def client(self) -> redis.Redis:
        if self._redis is None:
            self._establish_connection()
        assert self._redis is not None
        return self._redis

    def _execute_redis_command(self, command_name: str, *args, **kwargs):
        """Execute Redis command with bounded reconnection.

        Any connection error will trigger a single reconnection attempt. If that
        also fails, the exception is propagated to the caller instead of
        retrying indefinitely, which helps avoid silently blocking API workers
        when Redis is unhealthy.
        """

        start = time.monotonic()
        client = self.client
        try:
            command = getattr(client, command_name)
            result = command(*args, **kwargs)
            elapsed = time.monotonic() - start
            if elapsed > 1.0:
                logger.warning(
                    f"Redis command '{command_name}' took {elapsed:.2f}s to complete"
                )
            return result
        except self.connection_errors as exc:
            logger.warning(f"Redis connection error, reconnecting once: {exc}")
            # Reset client to force reconnection (Kombu approach)
            self._redis = None
            self._pool = None

            # Retry once with new connection
            client = self.client
            command = getattr(client, command_name)
            result = command(*args, **kwargs)
            elapsed = time.monotonic() - start
            if elapsed > 1.0:
                logger.warning(
                    f"Redis command '{command_name}' succeeded after reconnect "
                    f"in {elapsed:.2f}s"
                )
            return result
