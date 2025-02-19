import sys

from loguru import logger

# Configure logger
logger.remove()  # Remove default logger
logger.add(
    sys.stderr, 
    level="ERROR", 
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}"
)  # Log errors to stderr with timestamp
logger.add(
    "logs/fastprocesses_{time}.log", 
    level="INFO", 
    rotation="1 week", 
    retention="1 month", 
    format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}"
)  # Log to file with rotation and timestamp

# Example usage
logger.info("Logging setup complete.")
