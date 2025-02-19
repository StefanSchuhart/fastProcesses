import sys
import subprocess
import signal

def main():
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

    def handle_signal(sig, frame):
        # Forward the signal to the Celery process
        process.send_signal(sig)

    # Register signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        # Wait for the Celery process to complete
        process.wait()
    except KeyboardInterrupt:
        # Handle the KeyboardInterrupt to ensure a warm shutdown
        process.send_signal(signal.SIGINT)
        process.wait()

if __name__ == "__main__":
    main()
