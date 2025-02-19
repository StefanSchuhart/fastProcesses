import signal
import subprocess
import sys

from fastprocesses.core.logging import logger


def main():
    logger.info("Starting Celery worker")
    # Construct the Celery command
    celery_command = [
        "celery",
        "-A",
        "src.fastprocesses.worker.celery_app",
        "worker",
        "--loglevel=info"
    ]

    # Append any additional arguments passed to the script
    celery_command.extend(sys.argv[1:])

    # Start the Celery process
    process = subprocess.Popen(celery_command)
    logger.info("Celery worker started")

    def handle_signal(sig, frame):
        logger.info(f"Received signal: {sig}")
        # Forward the signal to the Celery process
        process.send_signal(sig)

    # Register signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        # Wait for the Celery process to complete
        process.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down Celery worker")
        # Handle the KeyboardInterrupt to ensure a warm shutdown
        process.send_signal(signal.SIGINT)
        process.wait()

if __name__ == "__main__":
    main()
