# worker/celery_app.py
import asyncio
from celery import Task
from typing import Any, Dict

from fastprocesses.core.logging import logger
from fastprocesses.common import celery_app, redis_cache
from fastprocesses.processes.process_registry import get_process_registry
from fastprocesses.core.models import CalculationTask

class CacheResultTask(Task):
    def on_success(self, retval, task_id, args, kwargs):
        try:
            calculation_task = CalculationTask(**args[1])  # Get the calculation task from args
            key = calculation_task.celery_key
            redis_cache.put(key=key, value=retval)
            logger.info(f"Saved result with key {key} to cache: {retval}")
        except Exception as e:
            logger.error(f"Error in cache result task: {e}")

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

@celery_app.task(name="check_cache")
def check_cache(calculation_task: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check if results exist in cache and return status
    """
    task = CalculationTask(**calculation_task)
    cached_result = redis_cache.get(key=task.celery_key)
    
    if cached_result:
        logger.info(f"Cache hit for key {task.celery_key}")
        return {"status": "HIT", "result": cached_result}

    logger.info(f"Cache miss for key {task.celery_key}")
    return {"status": "MISS"}

@celery_app.task(name="find_result_in_cache")
def find_result_in_cache(celery_key: str) -> dict | None:
    """
    Retrieve result from cache
    """
    result = redis_cache.get(key=celery_key)
    if result:
        logger.info(f"Retrieved result from cache for key {celery_key}")
    return result