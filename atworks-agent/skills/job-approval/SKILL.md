---
name: job-approval
description: What is waiting for approval, applying a job the operator already approved on the Jobs page, discarding a staged job. / 승인 대기 목록, 이미 승인된 job 적용, 스테이징 취소.
---

# Job approval

- `get_pending_jobs` first; refer to jobs by id and show one with `present_job_preview` only when it was not shown this turn.
- A job's card lists every environment, schedule and data set the approval covers; if the operator wants only part of it, discard and stage a narrower job.
- `apply_job` only for a job the operator approved on the Jobs page; when the gate holds, tell the operator approval happens there and stop.
- `discard_job` when the operator rejects or replaces a job. Confirm after the call succeeds.
