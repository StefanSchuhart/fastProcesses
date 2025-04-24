import logging
import sys

from loguru import logger

# Remove existing handlers
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

loggers = (
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "fastapi",
    "asyncio",
    "starlette",
)

for logger_name in loggers:
    logging_logger = logging.getLogger(logger_name)
    logging_logger.handlers = []
    logging_logger.propagate = True

class ErrorLogFilter:
    def __init__(self):
        self.error_logged = False

    def __call__(self, record):
        if record["level"].name == "ERROR":
            self.error_logged = True
        return self.error_logged

class InterceptHandler(logging.Handler):
    def emit(self, record):
        # Get corresponding Loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller to get correct stack depth
        frame, depth = logging.currentframe(), 2
        while frame.f_back and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

# Configure logger
logger.remove()  # Remove default logger

# Error logs to stderr
logger.add(
    sys.stderr, 
    level="ERROR", 
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}"
)


# Instantiate the filter, to create files only when errors are logged
error_log_filter = ErrorLogFilter()

# Error logs to file with weekly rotation
logger.add(
    "logs/errors_{time}.log", 
    level="ERROR", 
    rotation="1 week", 
    retention="1 month", 
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}",
    filter=error_log_filter
)

# Info logs to stdout
# TODO read loglevel from settings!
logger.add(
    sys.stdout, 
    level="DEBUG", 
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}",
    backtrace=True,
    diagnose=True,
)


# Intercept standard logging
logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO)

# Example usage
logger.info("Logging setup complete.")
