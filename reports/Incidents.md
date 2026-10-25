
---

## Incident: Poison messages replayed on every new job (fastprocesses)

**Symptom (prod, 2026-10-25)**

- Starting one job (`target_crs=EPSG:25832`) spawns many additional workers. They log `xyz.py:279: UserWarning: Geometry is in a geographic CRS. Results from 'buffer' are likely incorrect` and get OOMKilled.
- No matching POST from us. Between jobs the system is quiet; the extra workers only appear when a new job is submitted.

**Findings**

- Broker: queue `celery` empty, but `HLEN unacked` = 5. All five are `fastprocesses.execute_process` messages with different job IDs, all `redelivered: true`, and at least one carries `target_crs = http://www.opengis.net/def/crs/EPSG/0/4326`. They were submitted by some other client.
- A geographic `target_crs` is passed unchecked to `project_crs_to_metric` (`gdf.to_crs(target_crs)`). The 30 m halo becomes 30°, so every work item contains all streets and flood features, and `_run_parallel` holds them all in memory, which leads to the OOM. With a metric `target_crs` the warning cannot occur (verified for workspace code and v0.13.4).

**Mechanism**

1. fastprocesses sets `task_acks_late=True`, `task_reject_on_worker_lost=True` and `visibility_timeout = FP_CELERY_TASK_TLIMIT_HARD + 300` (2100 s). There is no limit on redeliveries and no dead-letter queue.
2. On OOMKill the whole pod gets SIGKILL, so `reject_on_worker_lost` never fires; the message stays in the `unacked` hash.
3. kombu's Redis transport (5.6) only restores expired unacked messages from inside a *running* worker (`maybe_restore_messages`, every 10 s). With KEDA scaled to zero, nothing restores them, and KEDA's list-length trigger does not see `unacked`.
4. A new POST scales up one worker. It restores all expired unacked messages into the queue, KEDA scales up further, and each new worker picks up a poison message and gets OOMKilled again. The cycle repeats on the next POST.

**Solution**

1. *Done (this repo):* `target_crs` validator rejects non-projected CRSs (`models/inputs.py`, test `test_pipeline_params_rejects_geographic_target_crs`). New requests like this fail before they are queued. This does not drain the existing messages: the worker downloads remote inputs before the input model with the new check runs.
2. *Ops, one-off:* with workers scaled to zero, delete the poison messages from the broker (DB 0):
   ```bash
   for tag in $($R ZRANGE unacked_index 0 -1); do $R HDEL unacked "$tag"; $R ZREM unacked_index "$tag"; done
   ```
   Inspect first with `$R --raw HVALS unacked | grep -oE '"(task|id|redelivered)": ?("[^"]*"|true|false)|target_crs[^,]{0,80}'`. Find the originating client via `job_status:job:<id>` (DB 1, field `created`) and the API logs (`Task queued: ... job_id=<id>`).
3. *fastprocesses (to implement):* add a delivery limit. At the start of `execute_process`, `INCR` a per-task-id counter in Redis (TTL ≈ result TTL). Above N (e.g. 3), mark the job FAILED with a clear message, log the poison message and return, so the message is acked. This breaks the loop for any input that deterministically kills the worker, not just this CRS case.
4. *Optional:* make KEDA aware of `unacked` (e.g. alert on `HLEN unacked > 0` while no worker runs), so stuck messages are visible before the next job triggers them.