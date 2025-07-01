# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).

## [Development version]

#### Added

#### Changed

#### Fixed

### Planned
- further improve storing jobs and job results in cache using a dedicated object model (eventually using redis_om)
- implement callback mechanism according to [OGC API Processes requirment class](https://docs.ogc.org/is/18-062r2/18-062r2.html#toc52)

## [0.15] - dev
### [0.15.1] - 2025-06-30
#### Changed
- removed unsued celery worker
- removed unsued task routes
- added celery config log (debug)

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
