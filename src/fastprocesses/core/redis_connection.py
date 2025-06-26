from socket import socket
import time
from typing import Optional

import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from fastprocesses.core.logging import logger


class RedisConnection:
    """
    Unified Redis connection handler with robust retry and reconnection logic.
    """
    def __init__(self, url: str, connection_config: Optional[dict] = None,
        retry_config: Optional[dict] = None):
        self._pool: Optional[redis.ConnectionPool] = None
        self._redis: Optional[redis.Redis] = None
        self.url = url
        self.connection_config = connection_config or {
            'socket_connect_timeout': 30,
            'socket_timeout': 30,
            'socket_keepalive': True,
            'health_check_interval': 30,
            'retry_on_timeout': True,
            'max_connections': 20,
        }
        self.retry_config = retry_config or {
            'max_retries': 100,
            'retry_on_startup': True,
            'base_delay': 1,
            'max_delay': 60,
        }
        self.connection_errors = (
            ConnectionError, 
            TimeoutError, 
            ConnectionResetError,
            OSError, 
            IOError, 
            EOFError
        )

    def _create_connection_pool(self):
        retry = Retry(
            ExponentialBackoff(
                cap=self.retry_config['max_delay'],
                base=self.retry_config['base_delay']
            ),
            retries=self.retry_config["max_retries"]
        )
        self._pool = redis.ConnectionPool.from_url(
            self.url,
            retry=retry,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError],
            **self.connection_config
        )

    def _establish_connection(self):
        if not self._pool:
            self._create_connection_pool()
        max_retries = self.retry_config['max_retries']
        base_delay = self.retry_config['base_delay']

        for attempt in range(max_retries + 1):
            try:
                self._redis = redis.Redis(connection_pool=self._pool)
                self._redis.ping()
                logger.info(
                    "Redis connection established successfully"
                )
                return
            except (ConnectionError, TimeoutError, ConnectionResetError, OSError) as e:
                if attempt == max_retries:
                    logger.error(
                        "Failed to establish Redis connection after "
                        f"{max_retries} attempts"
                    )
                    raise ConnectionError(f"Could not connect to Redis: {e}")
                delay = min(base_delay * (2 ** attempt), self.retry_config['max_delay'])
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
        """Execute Redis command with Kombu-style error handling."""
        client = self.client
        try:
            command = getattr(client, command_name)
            return command(*args, **kwargs)
        except self.connection_errors as exc:
            logger.warning(f"Redis connection error, reconnecting: {exc}")
            # Reset client to force reconnection (Kombu approach)
            self._redis = None
            self._pool = None
            
            # Retry once with new connection
            client = self.client
            command = getattr(client, command_name)
            return command(*args, **kwargs)