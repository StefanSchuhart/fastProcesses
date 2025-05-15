# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).

## [Development version]

### Added

### Changed

### Fixed

### Planned
- improve/implement retry mechanisms when calling celery tasks
- further improve storing jobs and job results in cache using a dedicated object model (eventually using redis_om)
- implement callback mechanism according to [OGC API Processes requirment class](https://docs.ogc.org/is/18-062r2/18-062r2.html#toc52)

## [0.12.0] - 2025-05-15

### Fixed
- when retrieving results from cache user provided inputs *and outputs* will be factored in, not only inputs  
- make sure outputs not specified by the user are excluded from /jobs/{jobId}/results page

## [0.11.0] - 2025-05-14

### Added
- log message when the cache is missed
- custom Exceptions for various error events

### Changed
- greatly improved error handling (distinguish between user input error, process execution errors and library errors) 
- give process users and library users meaningful error messages in the correct place (job message/result, logs)


### Fixed
- JobStatusCode types

## [0.10.0] - 2025-04-25

### Changed
- improved cache handling and distinguish between caching jobs and results (TTL)
- added various new settings to customize the caches
- made progress_callback more descriptive,
- improved handling, limiting it to update the message and updated fields only...
- and renamed it to **job_progress_callback**
- replaced celerys AsyncResult for job status retrieval by getting the job info stored in redis

### Added

### Fixed
fix: store the error message when a job fails in job details
fix: result_expires must be seconds
fix: return None, if no process class was found in path (dont try to do something like  `None()`

## [0.9.0] - 2025-04-08

### new features
- read process description from yaml
- using Prefer header to determine the execution mode (sync/async) instead of "mode" in body
### internal bugfixes and changes
- fix: missing links in jobs
- fix: empty job list because of invalid search key
- added: JobStatusCode for consistent job status naming
- fix: using JobStatusInfo throughout the code for consistent Job status updates
- improvement: consistent method naming
