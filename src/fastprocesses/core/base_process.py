import asyncio
import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable, ClassVar, Dict, List

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel

from fastprocesses.core.models import OutputControl, ProcessDescription
from fastprocesses.core.types import JobProgressCallback


class BaseProcess(ABC):
    process_description: ClassVar[ProcessDescription]

    def get_description(self) -> ProcessDescription:
        """
        Returns the OGC API Process description.

        Returns:
            ProcessDescription: Complete process description following OGC API standard
        """
        if not hasattr(self, "process_description"):
            raise NotImplementedError(
                f"Process class {self.__class__.__name__} must "
                "define 'process_description'"
            )
        return self.process_description

    @classmethod
    def create_description(cls, description_dict: Dict[str, Any]) -> ProcessDescription:
        """
        Creates a ProcessDescription from a dictionary.

        Args:
            description_dict (Dict[str, Any]): Dictionary containing process description

        Returns:
            ProcessDescription: Validated process description object
        """
        return ProcessDescription.model_validate(description_dict)

    @abstractmethod
    def execute(
        self,
        exec_body: Dict[str, Any],
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel | Awaitable[BaseModel]:
        """
        Executes the process with given inputs.

        Args:
            inputs (Dict[str, Any]): Input parameters matching the process description

        Returns:
            Dict[str, Any]: Output values matching the process description

        Raises:
            ValueError: If inputs are invalid
        """
        pass

    def run_execute(
        self,
        exec_body: dict,
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel:
        """
        Calls the execute method, handling both sync and async implementations.
        Always returns a BaseModel, never an awaitable.
        """
        result = self.execute(exec_body, job_progress_callback=job_progress_callback)
        if inspect.isawaitable(result):
            if asyncio.iscoroutine(result):
                return asyncio.run(result)
            else:

                async def _await_result():
                    return await result

                return asyncio.run(_await_result())
        else:
            return result

    def quick_validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Quickly checks that all required input fields are present.
        Does NOT perform deep schema validation.
        """
        description: ProcessDescription = self.get_description()
        required_inputs = description.inputs

        # Check for missing required inputs only
        for input_name, input_desc in required_inputs.items():
            if input_desc.minOccurs > 0 and input_name not in inputs:
                raise ValueError(
                    f"Missing required input '{input_name}'. "
                    f"Description: {input_desc.description}"
                )
        return True

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Validates the input data against the process description.

        Args:
            inputs (Dict[str, Any]): The input data to validate

        Returns:
            bool: True if inputs are valid

        Raises:
            ValueError: With detailed error message if validation fails
        """
        # TODO: consider using fastjsonschema for better performance
        description: ProcessDescription = self.get_description()
        required_inputs = description.inputs

        # First, check all provided inputs
        for input_name, input_value in inputs.items():
            if input_name not in required_inputs:
                raise ValueError(
                    f"Provided input '{input_name}' is "
                    "not defined in the process description."
                )
            input_desc = required_inputs[input_name]
            try:
                input_schema = input_desc.scheme.model_dump(exclude_unset=True)
                jsonschema_validate(instance=input_value, schema=input_schema)
            except JSONSchemaValidationError as e:
                raise ValueError(
                    f"Input '{input_name}' validation failed: {e.message}. "
                    f"Description: {input_desc.scheme.model_dump(exclude_unset=True)}"
                )

        # Then, check for missing required inputs
        for input_name, input_desc in required_inputs.items():
            if input_desc.minOccurs > 0 and input_name not in inputs:
                raise ValueError(
                    f"Missing required input '{input_name}'. "
                    f"Description: {input_desc.description}"
                )

        return True

    def validate_outputs(
        self, outputs: dict[str, dict[str, OutputControl]] | None
    ) -> bool:
        """
        Validates the outputs parameter against the process description.

        Args:
            outputs: Single output identifier or list of output identifiers

        Returns:
            bool: True if outputs are valid

        Raises:
            ValueError: If any output identifier is invalid
        """
        description = self.get_description()
        available_outputs = description.outputs.keys()

        if not available_outputs:
            raise ValueError("Process has no defined outputs")

        if outputs is None:
            # If no outputs specified, all outputs are considered valid
            return True

        if not isinstance(outputs, dict):
            raise ValueError("Outputs must be a dict mapping.")

        # Validate each output identifier in the outputs dict
        invalid_outputs = [
            out for out in outputs.keys() if out not in available_outputs
        ]
        if invalid_outputs:
            available = ", ".join(available_outputs)
            invalid = ", ".join(invalid_outputs)
            raise ValueError(
                f"Invalid output identifiers: {invalid}. "
                f"Available outputs are: {available}"
            )

        # Optionally, validate OutputControl objects if needed
        # for out, control in outputs.items():
        #     if not isinstance(control, dict) or not all(
        #         isinstance(v, OutputControl) for v in control.values()
        #     ):
        #         raise ValueError(
        #             f"Output '{out}' must map to a dict of OutputControl objects."
        #         )

        return True


class BaseParallelProcess(BaseProcess, ABC):
    """
    A ``BaseProcess`` variant that fans out work across multiple Celery workers
    using a split → parallel execute → merge pattern.

    Library users implement three methods:

    1. ``split_inputs(exec_body)`` — partition the incoming request into N
       independent work items.  Each item is a plain ``dict`` that will be
       passed verbatim to ``execute_single``.

    2. ``execute_single(item, job_progress_callback)`` — process **one** work
       item.  This method is called on a dedicated Celery worker for every item
       returned by ``split_inputs``.

    3. ``merge_results(results)`` — fold the N partial results (returned as
       ``dict`` objects after Celery serialisation) into the final ``BaseModel``
       output.

    Example usage::

        @register_process("batch_upper")
        class BatchUpperProcess(BaseParallelProcess):
            process_description = ProcessDescription.from_yaml("batch_upper.yaml")

            def split_inputs(self, exec_body: dict) -> list[dict]:
                words = exec_body["inputs"]["words"]
                chunk_size = 10
                return [
                    {"inputs": {"words": words[i : i + chunk_size]}}
                    for i in range(0, len(words), chunk_size)
                ]

            def execute_single(
                self, item: dict, job_progress_callback=None
            ) -> BatchResult:
                words = item["inputs"]["words"]
                return BatchResult(upper=[w.upper() for w in words])

            def merge_results(self, results: list[dict]) -> BatchResult:
                combined = [word for r in results for word in r["upper"]]
                return BatchResult(upper=combined)

    The orchestration (Celery ``group`` dispatch, progress reporting, and result
    collection) is handled entirely by the library — users never interact with
    Celery primitives directly.

    Note:
        When ``execute`` is called **outside** of a Celery worker (e.g. in unit
        tests), the default implementation runs ``execute_single`` serially so
        that no broker is required.
    """

    @abstractmethod
    def split_inputs(self, exec_body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Partition the execution body into N independent work items.

        Args:
            exec_body: The full execution request body (same structure as the
                ``exec_body`` received by ``execute``).

        Returns:
            A list of dicts, one per parallel work item.  Each dict is passed
            as-is to ``execute_single`` on a separate Celery worker.
        """
        ...

    @abstractmethod
    def execute_single(
        self,
        item: Dict[str, Any],
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel:
        """
        Process a single work item.

        This method is invoked on a dedicated Celery worker for each item
        produced by ``split_inputs``.

        Args:
            item: One element from the list returned by ``split_inputs``.
            job_progress_callback: Optional progress callback (may be ``None``
                when called from a subtask context).

        Returns:
            A ``BaseModel`` instance representing the partial result.
        """
        ...

    @abstractmethod
    def merge_results(self, results: List[Dict[str, Any]]) -> BaseModel:
        """
        Combine N partial results into the final output.

        Args:
            results: List of partial results.  Each element is the ``dict``
                produced by serialising the ``BaseModel`` returned from
                ``execute_single`` (i.e. the output of ``model.model_dump()``).

        Returns:
            The merged ``BaseModel`` to be stored as the job result.
        """
        ...

    # ------------------------------------------------------------------
    # Default execute() — serial fallback used outside Celery (e.g. tests)
    # The Celery worker overrides this behaviour by dispatching a group of
    # execute_parallel_item subtasks when it detects a BaseParallelProcess.
    # ------------------------------------------------------------------

    def execute(
        self,
        exec_body: Dict[str, Any],
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel:
        """
        Serial fallback: runs ``execute_single`` for each item in sequence.

        In production the Celery worker bypasses this method and fans out
        ``execute_single`` calls across multiple workers instead.  This
        implementation is retained so that ``BaseParallelProcess`` subclasses
        work correctly in unit tests without a running broker.
        """
        items = self.split_inputs(exec_body)
        raw_results: List[Dict[str, Any]] = []
        for item in items:
            partial = self.execute_single(item, job_progress_callback)
            if inspect.isawaitable(partial):
                if asyncio.iscoroutine(partial):
                    partial = asyncio.run(partial)
                else:
                    async def _await(p=partial):
                        return await p
                    partial = asyncio.run(_await())
            raw_results.append(partial.model_dump(exclude_none=True))
        return self.merge_results(raw_results)
