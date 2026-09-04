from datetime import UTC, datetime

from atworks_agent.serialization import job_record
from atworks_agent.types import JobKind, JobSchedule, JobSpec, TestDataSet


def test_job_record_carries_derived_matrix_counts():
    job = JobSpec(job_id="job-0001", kind=JobKind.SCHEDULED_RUN, summary="s",
                  api_ids=["api-001", "api-002"], target_envs=["dev", "stg"],
                  schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3),
                             JobSchedule(kind="once", at="09:00", from_date="2026-09-08", count=1)],
                  test_data=[TestDataSet(label="S1", values={"amount": "1"}),
                             TestDataSet(label="S2", values={"amount": "-1"})],
                  created_at=datetime.now(UTC), created_by="op")

    record = job_record(job)

    assert record["matrix_size"] == 8            # 2 apis x 2 envs x 2 data sets
    assert record["total_executions"] == 4        # 3 + 1
    assert record["remaining_executions"] == 4
    assert record["runs_total"] == 32
