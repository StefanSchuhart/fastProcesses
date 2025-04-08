# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).


## Development version 0.10.0

## Version 0.9.0

### new features
- read process description from yaml
- using Prefer header to determine the execution mode (sync/async) instead of "mode" in body
### internal bugfixes and changes
- fix: missing links in jobs
- fix: empty job list because of invalid search key
- added: JobStatusCode for consistent job status naming
- fix: using JobStatusInfo throughout the code for consistent Job status updates
- improvement: consistent method naming
