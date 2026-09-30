# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).

## [Development version]

#### Added

#### Changed
- Documented that `late_validate` must not mutate `inputs` and that its return value is ignored (raise `ValueError` to fail); `resolve_remote_inputs` is the supported place for input transformations
- `late_validate` return annotation widened to `bool | None`; existing overrides remain compatible

#### Fixed

### Planned
- further improve storing jobs and job results in cache using a dedicated object model (eventually using redis_om)
- implement callback mechanism according to [OGC API Processes requirment class](https://docs.ogc.org/is/18-062r2/18-062r2.html#toc52)

## [0.24]

### [0.24.1] - 2026-09-30

#### Changed
- `late_validate` should not mutate inputs, but only log and fail
- changed `late_validate`s return signature to None to accompany correct usage pattern 


### [0.24.0] - 2026-09-30
#### Added
- Configurable result size and cache read limits, with a specific error when results exceed the size limit
- Optional API network policy and new cache settings in the Helm chart
#### Changed
- Unified job result caching and separated job request caching; duplicate results are stored once and referenced by pointer
- Compressed cached results with `orjson` to reduce storage, and reused Redis connections per temporary result cache instance
- Process registry lookups now use the Celery broker; result cache hashes include the process ID to isolate processes
- Reduced memory use while reading and parsing large cached results
#### Fixed
- Failed chord jobs no longer appear as jobs that are merely not ready
- Output format registration validates both directions against the process description and resolves OGC format hints
- Corrected JSON-like string construction and removed unnecessary expansion of results for size logging

## [0.23]

### [0.23.3] - 2026-07-29
#### Added
- Local process store alongside Redis for process registration
#### Changed
- Default result cache database is now 1
#### Fixed
- Celery result expiry now converts configured days to seconds correctly

### [0.23.2] - 2026-07-29
#### Fixed
- Startup no longer crashes when Redis is unavailable

### [0.23.1] - 2026-07-24
#### Added
- Multipart output responses

### [0.23.0] - 2026-07-17
#### Added
- Reference output transmission (`transmissionMode: reference`) and an example of resolving references
- Missing conformance class declaration
#### Fixed
- Location headers now use an absolute base URL
- Exceptions raised by process implementations are reported to users without leaking unpicklable errors into Celery

## [0.22]

### [0.22.6] - 2026-06-30
#### Added
- Option to skip input validation when debugging large datasets

### [0.22.5] - 2026-06-30
#### Added
- Worker status messages
#### Fixed
- Errors in chord subtasks no longer prevent finalization or hide the traceback

### [0.22.4] - 2026-06-30
#### Fixed
- Persist the outputs requested in the original execution request

### [0.22.3] - 2026-06-30
#### Fixed
- `BaseParallelProcess` retains access to the original execution body
- Corrected task output calculation and defaulted to native JSON when the schema omits a media type

### [0.22.2] - 2026-06-30
#### Added
- Allowed input origins configuration
#### Fixed
- `merge_results` can access resolved input data
- Corrected password environment variable handling

### [0.22.1] - 2026-06-23
#### Changed
- `resolve_remote_inputs` now receives a job progress callback; improved job progress messages
- Removed deprecated `mode: async` request body option

### [0.22.0] - 2026-06-18
#### Added
- Output format resolution and serialization helpers for simple, parallel, and scatter processes
- `BaseProcessResult` for process outputs, with result validation and registration errors
#### Changed
- Output handling now builds responses from resolved formats and schemas
- Synchronous execution polls the result cache during its remaining response window
#### Fixed
- Null values no longer cause schema validation errors
- Job status includes the expected information when results come from cache
- Corrected cache key consistency and output serialization issues

## [0.21]

### [0.21.1] - 2026-05-26
#### Fixed
- Cache lookups now run for `BaseScatterProcess` and `BaseParallelProcess`

### [0.21.0] - 2026-05-22
#### Added
- Separate Celery broker connection configuration
#### Changed
- Store large worker data separately to reduce memory consumption

## [0.20]

### [0.20.3] - 2026-05-22
#### Changed
- Version bump only

### [0.20.2] - 2026-05-22
#### Changed
- Reduced worker polling interval and gossip in job mode
#### Fixed
- Tasks are picked up again when a worker dies

### [0.20.1] - 2026-05-21
#### Changed
- version bump (release candidate promoted)

### [0.20.0] - 2026-05-21
#### Changed
- Major internal refactor of `celery_app` into modular components: job status updates, pre-execution pipeline, execution strategies, and chord tasks are now separate modules
- `celery_app` is now a thin execution wrapper delegating to the extracted components
- No public API changes

## [0.19]

### [0.19.5] - 2026-05-21
#### Fixed
- Workers now cancel tasks and re-queue them on Redis connection loss instead of dropping them
- Fixed broadcast shutdown bug that incorrectly sent a stop signal to all workers connected to the same Redis instance
#### Changed
- Reduced polling interval for more responsive task handling
- Worker state is stored in Redis for observability

### [0.19.4] - 2026-05-21
#### Added
- `celery_queue` setting to target a named Celery queue
- Queue and process registry isolation enforced: multiple fastprocesses instances can share the same Redis instance without interference
#### Changed
- Improved logging of cornerstone events

### [0.19.3] - 2026-05-20
#### Added
- Process-specific late validation hook (`validate_inputs`) as an overridable no-op for library users
#### Fixed
- Removed duplicate missing required-inputs check

### [0.19.2] - 2026-05-19
#### Fixed
- Schema model `$ref` field now correctly uses Pydantic aliases

### [0.19.1] - 2026-05-19
#### Fixed
- Default values for Schema model fields were not applied correctly

### [0.19.0] - 2026-05-19
#### Added
- `resolve_remote_inputs` hook: workers call this when inputs are referenced by URL rather than provided inline
- `DataFetchError` exception for clean, user-facing error messages when remote data cannot be fetched
#### Changed
- Schema model enforcement applied recursively to nested Schema fields
- `model_dump` now serialises by alias where required
- Replaced all remaining "service" terminology with "process" throughout the codebase
- Internal app details no longer exposed to library users

## [0.18]

### [0.18.4] - 2026-05-15
#### Added
- Setting for large dataset support
#### Changed
- Improved handling of large request body sizes, reducing Python-level iterations on full body parsing

### [0.18.3] - 2026-05-12
#### Fixed
- Job status was not updated from fanout processes (`BaseParallelProcess` / `BaseScatterProcess`)
- Log message was flooding the console

### [0.18.2] - 2026-05-12
#### Added
- JSON schema registry parameter for resolving external schemas

### [0.18.1] - 2026-05-12
#### Added
- Automated job status counter — no manual progress updates required in `BaseProcess` subclasses
#### Fixed
- Log was flooding the console with the full input data payload
- Dead code path removed (serialised values always have a `decode` method)

### [0.18.0] - 2026-04-22
#### Added
- Generic Helm chart for Kubernetes deployment
#### Changed
- `merge_results` now receives `exec_body` as an additional argument so implementations have access to the original inputs if needed

## [0.17]

### [0.17.0] - 2026-04-20
#### Added
- Graceful handling of inaccessible remote JSON schemas (returns a specific exception instead of crashing)
- Extended CI test matrix to cover additional Python versions

## [0.16]

### [0.16.0] - 2026-03-12
#### Added
- `BaseParallelProcess` for data fan-out (split → map → merge): splits a large input into chunks, processes each on a separate worker, merges partial results
- `BaseScatterProcess` + `@parallel_step` decorator for operation fan-out: runs multiple independent operations on the same input concurrently and merges named results
- Both patterns use Celery chords — the orchestrating task returns immediately, making them fully compatible with KEDA autoscaling

## [0.15] - dev

### [0.15.5] - 2025-07-02
#### Fixed
- when results will be retrieved from cache job status is "accepted", not immediately "successful", because worker cold starts can be slow

### [0.15.4] - 2025-07-02
#### Fixed
- added missing job "started" date


### [0.15.3] - 2025-07-02
#### Added
- job mode: worker shuts down on task completion (set FP_CELERY_JOB_MODE to "true") 

### [0.15.2] - 2025-07-01
#### Changed
- removed unsued celery worker
- removed unsued task routes
- added celery config log (debug)

# Fixed
- handle process class in path not found more gracefully

### [0.15.1] - 2025-06-30
#### Fixed
- settings for time to live results cache and job status cache was hours instead of days

#### changed
- implement retry mechanism when calling celery tasks and redis cache
- improved celery worker config

### [0.15.0] - 2025-06-26

#### changed
- implement retry mechanism when calling celery tasks and redis cache
- improved celery worker config

## [0.14]

### [0.14.5] - 2025-06-25

#### Changed
- made sure worker accepting only one task and queue is emptied onyl when a worker finishes (ensuring that workers not getting killed before done when scaling with keda in k8s)

### [0.14.4] - 2025-06-25

#### Changed
- improved response time for execution requests when user input validation is complex and takes time (moved deep input validation to celery worker)

### [0.14.3] - 2025-06-25

#### Changed
- improved job error message in case of validation errors

### [0.14.2] - 2025-06-24

#### Changed
- namespaced celery tasks
- directly checking cache for results instead of using a celery task

### [0.14.1] - 2025-05-28

### Added
- added some link to landing page

### [0.14.0] - 2025-05-27

#### Added
- allow to add metadata to process
- simple html landing page (with content negotiation)

#### Changed
- settings have now a common "FP_" prefix to distinguish from other apps settings
- internal settings and logging initialization is more concise

## [0.13]
### [0.13.0] - 2025-05-26

#### Fixed
- various typing errors
- a problem where a job status stays on running, even if it failed

#### Changed
- updated celery and redis packages

#### Added
- integrated input validation uses schema fragment from process description

## [0.12]
### [0.12.0] - 2025-05-15

#### Fixed
- when retrieving results from cache user provided inputs *and outputs* will be factored in, not only inputs  
- make sure outputs not specified by the user are excluded from /jobs/{jobId}/results page

## [0.11]
### [0.11.0] - 2025-05-14

#### Added
- log message when the cache is missed
- custom Exceptions for various error events

#### Changed
- greatly improved error handling (distinguish between user input error, process execution errors and library errors) 
- give process users and library users meaningful error messages in the correct place (job message/result, logs)


#### Fixed
- JobStatusCode types

## [0.10]
### [0.10.0] - 2025-04-25

#### Changed
- improved cache handling and distinguish between caching jobs and results (TTL)
- added various new settings to customize the caches
- made progress_callback more descriptive,
- improved handling, limiting it to update the message and updated fields only...
- and renamed it to **job_progress_callback**
- replaced celerys AsyncResult for job status retrieval by getting the job info stored in redis

#### Added

#### Fixed
fix: store the error message when a job fails in job details
fix: result_expires must be seconds
fix: return None, if no process class was found in path (dont try to do something like  `None()`

## [0.9]
### [0.9.0] - 2025-04-08

### new features
- read process description from yaml
- using Prefer header to determine the execution mode (sync/async) instead of "mode" in body
### internal bugfixes and changes
- fix: missing links in jobs
- fix: empty job list because of invalid search key
- added: JobStatusCode for consistent job status naming
- fix: using JobStatusInfo throughout the code for consistent Job status updates
- improvement: consistent method naming
