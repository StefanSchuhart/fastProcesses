# fastprocesses

A library to create a FastAPI-based OGC API Processes wrapper around existing projects. This library simplifies the process of defining and registering processes, making it easy to build and deploy OGC API Processes.

## Version: 0.3.0

### Description

fastprocesses is a Python library that provides a simple and efficient way to create OGC API Processes using FastAPI. It allows you to define processes, register them, and expose them through a FastAPI application with minimal effort.

### Features

- **Ease of Use**: Simplifies the creation and registration of OGC API Processes.
- **FastAPI Integration**: Leverages FastAPI for building high-performance APIs.
- **Asynchronous Support**: Supports asynchronous process execution using Celery.
- **Configuration Management**: Uses Pydantic for robust configuration management.
- **Extensibility**: Easily extendable to add custom processes and services.
- **Redis Integration**: Built-in support for Redis as a message broker and result backend.
- **Command-Line Utilities**: Provides utilities for starting the FastAPI server and Celery worker.
- **Logging**: Uses `loguru` for modern and easy-to-configure logging with log rotation.

### Usage

1. **Define a Process**: Create a new process by subclassing `BaseProcess` and using the `@register_process` decorator.

    ```python
    from fastprocesses.core.base_process import BaseProcess
    from fastprocesses.services.service_registry import register_process
    from typing import Dict, Any

    @register_process("simple_process")
    class SimpleProcess(BaseProcess):
        def get_description(self) -> Dict[str, Any]:
            return {
                "id": "simple_process",
                "title": "Simple Process",
                "version": "1.0.0",
                "description": "A simple example process",
                "inputs": {
                    "input_text": {
                        "title": "Input Text",
                        "description": "Text to process",
                        "schema": {
                            "type": "string"
                        },
                        "minOccurs": 1,
                        "maxOccurs": 1
                    }
                },
                "outputs": {
                    "output_text": {
                        "title": "Output Text",
                        "description": "Processed text",
                        "schema": {
                            "type": "string"
                        }
                    }
                }
            }

        async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
            input_text = inputs["input_text"]
            output_text = input_text.upper()
            return {"output_text": output_text}
    ```

2. **Run the FastAPI Application**: Use the `OGCProcessesAPI` class to create and run the FastAPI application.

    ```python
    import uvicorn
    from fastprocesses.api.server import OGCProcessesAPI

    app = OGCProcessesAPI().get_app()

    if __name__ == "__main__":
        uvicorn.run(app, host="0.0.0.0", port=8000)
    ```

3. **Start the Celery Worker**: Use the provided command-line utility to start the Celery worker.

    ```sh
    poetry run start-celery-worker --concurrency=4
    ```

4. **Setup Logging**: The library uses `loguru` for logging. Logs are written to both stderr and a file with rotation.

    ```python
    from fastprocesses.core.logging import logger

    logger.info("This is an info message.")
    logger.error("This is an error message.")
    ```

### Version Notes

- **0.1.3**: Initial release with support for defining and registering processes, running a FastAPI application, and starting a Celery worker using a custom command-line utility.