from typing import Protocol


class JobProgressCallback(Protocol):
    def __call__(self, progress: int, message: str) -> None:
        """
        A callback function to report progress.

        Args:
            progress (int): The progress percentage (0-100).
            message (str): A message describing the current progress.
        """
        pass
