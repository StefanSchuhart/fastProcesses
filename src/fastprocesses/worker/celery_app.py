# worker/celery_app.py
import asyncio
from celery import signals, Task
from typing import Any, Dict

from fastprocesses.core.logging import logger
from fastprocesses.common import celery_app, redis_cache
from fastprocesses.services.service_registry import get_process_registry

class CacheResultTask(Task):
    def on_success(self, retval, task_id, args, kwargs):
        key = args[0]["celery_key"]
        redis_cache.put(key=key, value=retval)
        logger.info(f"Saved result with key {key} to cache.")

@celery_app.task(name="execute_process", base=CacheResultTask)
def execute_process(process_id: str, data: Dict[str, Any]):
    logger.info(f"Executing process {process_id} with data {data}")
    try:
        service = get_process_registry().get_service(process_id)
        if asyncio.iscoroutinefunction(service.execute):
            result = asyncio.run(service.execute(data))
        else:
            result = service.execute(data)
        logger.info(f"Process {process_id} executed successfully with result {result}")
        return result
    except Exception as e:
        logger.error(f"Error executing process {process_id}: {e}")
        raise

@celery_app.task(name="find_result_in_cache")
def find_result_in_cache(celery_key: str) -> dict | None:
    # Returns result dict or None
    return redis_cache.get(key=celery_key)
