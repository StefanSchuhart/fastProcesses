import asyncio
import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable, ClassVar, Dict, List

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema import validate as jsonschema_validate
from jsonschema.exceptions import best_match as _jsonschema_best_match
from pydantic import BaseModel
from referencing import Registry
from referencing.exceptions import Unresolvable as _UnresolvableRef

from fastprocesses.core.logging import logger
from fastprocesses.core.models import OutputControl, ProcessDescription
from fastprocesses.core.types import JobProgressCallback

# Maps JSON Schema primitive type names to their Python equivalents.
# Used by quick_validate_inputs for a cheap top-level type check.
_JS_TYPE_TO_PYTHON: Dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


class BaseProcess(ABC):
    process_description: ClassVar[ProcessDescription]
    schema_registry: ClassVar[Registry | None] = None
    """Optional local ``referencing.Registry`` for resolving ``$ref`` URIs
    without network access.  Subclasses that reference remote schemas should
    build a registry from bundled local files and assign it here."""

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

    def resolve_remote_inputs(self, exec_body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Substitutes URI-valued inputs with the downloaded data they reference.

        The default implementation is a **no-op** that returns *exec_body*
        unchanged, so existing processes are unaffected.

        Override this in subclasses whose inputs may be supplied as URIs
        instead of inline data.  The worker calls this method *after*
        ``validate_inputs`` so that the process description schema is validated
        against the wire-format input (the URI reference object), and *before*
        ``late_validate`` which validates the resolved data.

        The HTTP fetch, authentication, redirect policy, and SSRF checks are
        the responsibility of the overriding application — fastprocesses does
        not impose a specific HTTP client or security policy here.  Raise
        ``SSRFBlockedError`` or ``ValueError`` for user-facing errors; the
        worker will mark the job as failed with the error message.

        Example::

            from fastprocesses.core.exceptions import SSRFBlockedError

            def resolve_remote_inputs(self, exec_body: dict) -> dict:
                url = exec_body["inputs"]["buildings"]
                if not url.startswith("https://trusted-domain.com/"):
                    raise SSRFBlockedError("URL not in allowed origins.")
                response = my_http_client.get(url, timeout=30)
                response.raise_for_status()
                inputs = {**exec_body["inputs"], "buildings": response.json()}
                return {**exec_body, "inputs": inputs}

        Note:
            The original *exec_body* (containing the URI string) is used for
            the cache key, not the resolved data.  This keeps cache hits
            stable across re-runs with the same URI.
        """
        return exec_body

    def late_validate(self, inputs: Dict[str, Any]) -> bool:
        """
        Process-specific validation of **resolved** inputs, called after
        :meth:`resolve_remote_inputs` and before ``execute``.

        The default implementation is a **no-op** that always returns ``True``,
        so existing processes are unaffected.

        Override this in subclasses that need to validate data that was
        fetched or transformed by ``resolve_remote_inputs`` — for example,
        validating a downloaded GeoJSON FeatureCollection against a
        process-specific Pydantic model, or spot-checking the first feature
        to detect structural problems early.

        Unlike :meth:`validate_inputs`, which uses the generic process
        description schema, ``late_validate`` has full access to the resolved
        data and can apply arbitrary business logic.  Raise ``ValueError`` to
        fail the job with a user-facing message.

        Example::

            from mypackage.models import BuildingFeatureCollection

            def late_validate(self, inputs: dict) -> bool:
                # Validate the fetched collection against a Pydantic model.
                # model_validate raises ValidationError on structural problems.
                BuildingFeatureCollection.model_validate(
                    inputs["buildings"]
                )
                return True
        """
        return True

    def quick_validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Cheap structural check run at the API boundary (before the job is queued).

        Catches the three most common user errors without any recursion:
        1. Unknown field names (e.g. a typo like ``"buldings"`` instead of
           ``"buildings"``)
        2. Missing required fields
        3. Top-level type mismatch (e.g. passing a string where an object is
           expected)

        Deep schema validation (nested properties, ``oneOf``, ``enum``,
        ``const``, …) is left to :meth:`validate_inputs` which runs on the
        worker after the job has been accepted.
        """
        description: ProcessDescription = self.get_description()
        defined_inputs = description.inputs

        # 1. Unknown fields — catches typos before a job is even queued
        unknown = sorted(k for k in inputs if k not in defined_inputs)
        if unknown:
            raise ValueError(
                f"Unknown input(s): {', '.join(unknown)}. "
                f"Expected: {', '.join(sorted(defined_inputs))}."
            )

        # 2. Missing required fields
        for name, desc in defined_inputs.items():
            if desc.minOccurs > 0 and name not in inputs:
                raise ValueError(
                    f"Missing required input '{name}'. "
                    f"Description: {desc.description}"
                )

        # 3. Top-level type check — O(n inputs), no recursion into nested data
        for name, value in inputs.items():
            schema_type = defined_inputs[name].scheme.type
            if schema_type is None:
                continue  # no top-level type declared (e.g. pure oneOf/allOf)
            declared = [schema_type] if isinstance(schema_type, str) else schema_type
            expected = tuple(
                py_t
                for js_t in declared
                if (py_t := _JS_TYPE_TO_PYTHON.get(js_t)) is not None
            )
            if not expected:
                continue
            # bool is a subclass of int; reject it unless "boolean" is declared
            if isinstance(value, bool) and "boolean" not in declared:
                raise ValueError(
                    f"Input '{name}' has wrong type: "
                    f"expected {', '.join(declared)}, got boolean."
                )
            if not isinstance(value, expected):
                raise ValueError(
                    f"Input '{name}' has wrong type: "
                    f"expected {', '.join(declared)}, "
                    f"got {type(value).__name__}."
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
        description: ProcessDescription = self.get_description()
        required_inputs = description.inputs

        # First, check all provided inputs
        for input_name, input_value in inputs.items():
            logger.info("Validating input '%s'", input_name)

            if input_name not in required_inputs:
                raise ValueError(
                    f"Provided input '{input_name}' is "
                    "not defined in the process description."
                )
            input_desc = required_inputs[input_name]
            try:
                input_schema = input_desc.scheme.model_dump(
                    exclude_unset=True, by_alias=True
                )
                jsonschema_validate(
                    instance=input_value,
                    schema=input_schema,
                    registry=self.schema_registry,
                )
            except JSONSchemaValidationError as e:
                # For oneOf/anyOf/allOf, best_match picks the deepest,
                # most specific sub-error instead of the generic top-level one.
                leaf = _jsonschema_best_match(e.context) if e.context else e
                # Build a readable path: features[42].properties.area_m2
                path_parts: list[str] = []
                for step in leaf.absolute_path:
                    if isinstance(step, int):
                        if path_parts:
                            path_parts[-1] += f"[{step}]"
                        else:
                            path_parts.append(f"[{step}]")
                    else:
                        path_parts.append(str(step))
                location = ".".join(path_parts) or "(root)"
                raise ValueError(
                    f"Input '{input_name}' validation failed: "
                    f"{leaf.message} (at: {location})."
                )
            except Exception as e:
                # jsonschema wraps referencing.exceptions.Unresolvable as
                # _WrappedReferencingError when a remote $ref cannot be fetched
                # (e.g. SSL failures, network restrictions inside workers).
                # Rather than crashing the job, log a warning and skip
                # schema validation for this input so execution can proceed.
                cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
                is_unresolvable = isinstance(e, _UnresolvableRef) or isinstance(
                    cause, _UnresolvableRef
                )
                if is_unresolvable:
                    logger.warning(
                        "Remote $ref for input '{}' could not be resolved ({}). "
                        "Skipping schema validation for this input and proceeding.",
                        input_name,
                        e,
                    )
                else:
                    raise

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


# ---------------------------------------------------------------------------
# parallel_step decorator
# ---------------------------------------------------------------------------

def parallel_step(fn):
    """
    Marks a ``BaseScatterProcess`` method as a parallel step.

    Each decorated method::

        @parallel_step
        def analyse_elevation(self, exec_body: dict) -> ElevationResult:
            ...

    - receives the **full** ``exec_body`` (same as ``execute`` would)
    - runs on its **own Celery worker** concurrently with every other step
    - must return a ``pydantic.BaseModel``

    The step name used in ``merge_results`` is the method name.
    """
    fn._is_parallel_step = True
    return fn


def get_parallel_steps(
    process: "BaseScatterProcess",
) -> Dict[str, Any]:
    """
    Returns a ``{name: bound_method}`` mapping of all ``@parallel_step``
    methods defined on *process*.

    The ``_is_parallel_step`` flag is checked on the **class** MRO so that
    ``patch.object`` (which replaces only the instance attribute) does not
    hide decorated steps.  The bound method is still fetched from the instance
    so that any active patch/wrap is called during execution.
    """
    step_names: List[str] = []
    for cls in type(process).__mro__:
        for name, attr in cls.__dict__.items():
            if name not in step_names and getattr(attr, "_is_parallel_step", False):
                step_names.append(name)

    return {name: getattr(process, name) for name in step_names}


# ---------------------------------------------------------------------------
# BaseScatterProcess
# ---------------------------------------------------------------------------

class BaseScatterProcess(BaseProcess, ABC):
    """
    A ``BaseProcess`` variant for running **multiple different operations on
    the same input** concurrently, then merging the results (scatter/gather).

    Library users define one ``@parallel_step`` method per operation and one
    ``merge_results`` method:

    Example usage::

        @register_process("geo_enrich")
        class GeoEnrichProcess(BaseScatterProcess):
            process_description = ProcessDescription.from_yaml("geo_enrich.yaml")

            @parallel_step
            def get_elevation(self, exec_body: dict) -> ElevationResult:
                coords = exec_body["inputs"]["coordinates"]
                return ElevationResult(value=lookup_elevation(coords))

            @parallel_step
            def get_land_use(self, exec_body: dict) -> LandUseResult:
                coords = exec_body["inputs"]["coordinates"]
                return LandUseResult(category=lookup_land_use(coords))

            def merge_results(
                self, results: dict[str, dict], exec_body: dict
            ) -> GeoEnrichResult:
                # results keys match the method names; exec_body holds the
                # original inputs in case the merge step needs them.
                return GeoEnrichResult(
                    elevation=results["get_elevation"]["value"],
                    land_use=results["get_land_use"]["category"],
                )

    In production each ``@parallel_step`` is dispatched as an independent
    Celery task so workers can run them genuinely in parallel.

    Note:
        When ``execute`` is called outside of a Celery worker (e.g. in unit
        tests) the steps run serially in the order they are defined, so no
        broker is required.
    """

    @abstractmethod
    def merge_results(self, results: Dict[str, Any], exec_body: Dict[str, Any]) -> BaseModel:
        """
        Combine the results of all parallel steps into the final output.

        Args:
            results: ``{step_name: result_dict}`` mapping.  Each value is the
                ``dict`` produced by serialising the ``BaseModel`` returned by
                the corresponding ``@parallel_step`` method.
            exec_body: The original ``exec_body`` dict that was passed to each
                ``@parallel_step``.  Useful when the merge step needs access
                to the raw inputs (e.g. to reconstruct GeoJSON features or
                validate parameters) without smuggling data through step
                results.

        Returns:
            The merged ``BaseModel`` to be stored as the job result.
        """
        ...

    # ------------------------------------------------------------------
    # Serial fallback used outside Celery (e.g. tests)
    # ------------------------------------------------------------------

    def execute(
        self,
        exec_body: Dict[str, Any],
        job_progress_callback: JobProgressCallback | None = None,
    ) -> BaseModel:
        """
        Serial fallback: runs each ``@parallel_step`` in definition order.

        In production the Celery worker bypasses this and dispatchess one
        ``execute_scatter_step`` task per step instead.
        """
        steps = get_parallel_steps(self)
        if not steps:
            raise NotImplementedError(
                f"{self.__class__.__name__} defines no @parallel_step methods."
            )
        raw: Dict[str, Any] = {}
        for name, method in steps.items():
            partial = method(exec_body)
            if inspect.isawaitable(partial):
                if asyncio.iscoroutine(partial):
                    partial = asyncio.run(partial)
                else:
                    async def _await(p=partial):
                        return await p
                    partial = asyncio.run(_await())
            raw[name] = partial.model_dump(exclude_none=True)
        return self.merge_results(raw, exec_body)
