
from typing import Protocol


class ProgressCallback(Protocol):
    def __call__(self, progress: int, message: str, status: str | None = None) -> None:
        """
        A callback function to report progress.

        Args:
            progress (int): The progress percentage (0-100).
            message (str): A message describing the current progress.
            status (str | None): The current status (e.g., "RUNNING", "SUCCESSFUL").
        """
        pass
