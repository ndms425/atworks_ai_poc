import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksBackend,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    JobSchedule,
    JobSpec,
    JobStatus,
    RunResult,
    RunStatus,
    TestDataSet,
)
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import TEMPLATE, Reports, _matrix
from atworks_host.scheduler import Scheduler

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="sched", project_id="mes", operator="scheduler")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


async def test_tick_runs_due_jobs_only_and_writes_report(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="3일 09시", api_ids=["api-001", "api-003"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=3)]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 4, 8, 59, tzinfo=KST)) == []
    assert await sched.tick(datetime(2026, 9, 4, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 4, 9, 30, tzinfo=KST)) == []          # 같은 날 두 번 안 돈다
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert backend.ledger.get(job.job_id).remaining_executions == 1

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["provenance"]["generator"] == "refresh_runner"
    assert data["summary"]["total"] == 4 and data["summary"]["fail"] == 2
    html = reports.read_html(job.job_id)
    assert "refundAmount >= 0" in html and "<script id=\"report-data\"" in html


async def test_run_now_is_due_immediately(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="now", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 3, 14, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 3, 15, tzinfo=KST)) == []


async def test_report_html_escapes_json_and_uses_client_side_escaping(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    job = await backend.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="</script><img src=x onerror=alert(1)>",
                 api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT,
    )
    await backend.apply_job(SESSION, job.job_id)
    run = RunResult(run_id="run-9001", api_id="api-001", executed_at=datetime.now(UTC), target_env="dev",
                    status=RunStatus.FAIL, failed_rules=["<b>x</b>"], http_status=200, job_id=job.job_id)
    backend.runs[run.run_id] = run
    applied = backend.ledger.record_execution(job.job_id, [run.run_id], None)

    path = reports.write(applied, [run])
    html = path.read_text(encoding="utf-8")

    assert "\\u003c" in html
    assert "</script><img" not in html
    assert html.count("</script>") == 2

    template_source = TEMPLATE.read_text(encoding="utf-8")
    assert "esc(" in template_source


async def test_report_html_escapes_the_script_data_double_escape_sequence(tmp_path):
    # `<!--<script` inside the data script tag walks the HTML tokenizer into
    # script-data-double-escape, where a lone `</script>` does not close the block and
    # everything to EOF is swallowed — the `</` -only escape (the prior implementation)
    # did not cover this. Escaping every `<` removes the character from the markup
    # entirely, so this sequence can no longer reach the tokenizer at all.
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    job = await backend.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="<!--<script>alert(1)</script>",
                 api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT,
    )
    path = reports.write(job, [])
    html = path.read_text(encoding="utf-8")

    assert "<!--<script" not in html
    assert "</script>alert" not in html
    assert "\\u003c" in html


async def test_tick_survives_one_jobs_execution_error(tmp_path):
    class FlakyBackend(MockAtworks):
        bad_job_id: str | None = None

        async def execute_job_once(self, session, job_id, schedule_index=None):
            if job_id == self.bad_job_id:
                raise RuntimeError("boom")
            return await super().execute_job_once(session, job_id, schedule_index)

    backend = FlakyBackend(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    bad = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="bad", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    good = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="good", api_ids=["api-002"], target_envs=["dev"]), ActorKind.AGENT
    )
    backend.bad_job_id = bad.job_id
    await backend.apply_job(SESSION, bad.job_id)
    await backend.apply_job(SESSION, good.job_id)

    executed = await sched.tick(datetime(2026, 9, 3, 14, tzinfo=KST))

    assert executed == [good.job_id]
    bad_job = backend.ledger.get(bad.job_id)
    assert bad_job.remaining_executions == 0
    assert len(bad_job.guardrail_notes) == 1 and "execution failed" in bad_job.guardrail_notes[0]


async def test_report_failure_does_not_consume_a_second_slot(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)

    class BrokenReports(Reports):
        def write(self, job, runs, *, generator="refresh_runner", ignore_paths=(), per_api_ignore=None):
            raise OSError("disk full")

    sched = Scheduler(backend, BrokenReports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="3일 09시", api_ids=["api-001"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=3)]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 4, 9, 0, tzinfo=KST)) == [job.job_id]
    after = backend.ledger.get(job.job_id)
    assert after.remaining_executions == 2                      # exactly one slot consumed
    assert len(after.run_ids) == 1                        # the run was produced
    assert any(n.startswith("report failed") for n in after.guardrail_notes)
    assert not any(n.startswith("execution failed") for n in after.guardrail_notes)


def test_read_html_rejects_an_unsafe_id(tmp_path):
    reports = Reports(tmp_path)
    with pytest.raises(ValueError):
        reports.read_html("..\\x")


def test_all_runs_rejects_an_unsafe_id(tmp_path):
    reports = Reports(tmp_path)
    with pytest.raises(ValueError):
        reports.all_runs("../secret")


class RecordingBackend(AtworksBackend):
    """A minimal AtworksBackend the scheduler must be able to run against without ever
    reaching for a `ledger` or `runs` attribute (R44/I3) — the seam a REST backend
    would implement."""

    def __init__(self, job: JobSpec, run: RunResult):
        self.job = job
        self.run = run
        self.calls: list[str] = []

    async def search_apis(self, session, query="", updated_after=None, group=None, limit=20):
        raise NotImplementedError

    async def get_api(self, session, api_id):
        raise NotImplementedError

    async def list_runs(self, session, since=None, status=None, api_id=None, limit=50):
        raise NotImplementedError

    async def get_run(self, session, run_id):
        raise NotImplementedError

    async def count_runs(self, session, since, status):
        raise NotImplementedError

    async def stage_job(self, session, draft, actor_kind):
        raise NotImplementedError

    async def get_pending_jobs(self, session):
        raise NotImplementedError

    async def apply_job(self, session, job_id):
        raise NotImplementedError

    async def discard_job(self, session, job_id, actor_kind):
        raise NotImplementedError

    async def stage_rule(self, session, draft, actor_kind):
        raise NotImplementedError

    async def get_pending_rules(self, session):
        raise NotImplementedError

    async def apply_rule(self, session, rule_id):
        raise NotImplementedError

    async def discard_rule(self, session, rule_id, actor_kind):
        raise NotImplementedError

    async def list_rules(self, session, api_id=None):
        raise NotImplementedError

    async def simulate_rule(self, session, draft):
        raise NotImplementedError

    async def stage_profile(self, session, draft, actor_kind):
        raise NotImplementedError

    async def get_pending_profiles(self, session):
        raise NotImplementedError

    async def apply_profile(self, session, profile_id):
        raise NotImplementedError

    async def discard_profile(self, session, profile_id, actor_kind):
        raise NotImplementedError

    async def list_profiles(self, session, job_id=None):
        raise NotImplementedError

    async def get_parity_report(self, session, job_id):
        raise NotImplementedError

    async def find_apis_with_param(self, session, param):
        raise NotImplementedError

    async def recommend_rules_for_api(self, session, api_id):
        raise NotImplementedError

    async def get_format(self, session, name):
        raise NotImplementedError

    async def list_formats(self, session):
        raise NotImplementedError

    async def save_format(self, session, defn):
        raise NotImplementedError

    async def stage_format_batch(self, session, draft, actor_kind):
        raise NotImplementedError

    async def get_pending_format_batches(self, session):
        raise NotImplementedError

    async def apply_format_batch(self, session, batch_id):
        raise NotImplementedError

    async def discard_format_batch(self, session, batch_id, actor_kind):
        raise NotImplementedError

    async def execute_job_once(self, session, job_id, schedule_index=None):
        self.calls.append("execute_job_once")
        self.job = self.job.model_copy(update={"run_ids": [self.run.run_id], "executions": 1})
        return [self.run]

    async def get_job(self, session, job_id):
        self.calls.append("get_job")
        return self.job

    async def applied_jobs(self, session):
        self.calls.append("applied_jobs")
        return [self.job]

    async def all_jobs(self, session):
        self.calls.append("all_jobs")
        return [self.job]

    async def runs_by_ids(self, session, run_ids):
        self.calls.append("runs_by_ids")
        return [self.run] if run_ids else []

    async def record_execution(self, session, job_id, run_ids, schedule_index):
        self.calls.append("record_execution")
        return self.job

    async def add_guardrail_note(self, session, job_id, note):
        self.calls.append("add_guardrail_note")
        return self.job


async def test_two_schedules_on_one_job_run_independently(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="daily 3 + once", api_ids=["api-001"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=3),
                   JobSchedule(kind="once", at="14:00", tz="Asia/Seoul", from_date="2026-09-08", count=1)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 5, 8, 59, tzinfo=KST)) == []
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 5, 9, 30, tzinfo=KST)) == []   # 같은 회차는 두 번 안 돈다
    assert await sched.tick(datetime(2026, 9, 6, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 7, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 8, 14, 0, tzinfo=KST)) == [job.job_id]

    after = backend.ledger.get(job.job_id)
    assert [s.done for s in after.schedules] == [3, 1]
    assert after.executions == 4 and after.remaining_executions == 0
    assert await sched.tick(datetime(2026, 9, 9, 9, 0, tzinfo=KST)) == []


async def test_two_schedules_due_in_the_same_tick_run_twice(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="morning + evening", api_ids=["api-001"], target_envs=["dev"],
        schedules=[JobSchedule(kind="once", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=1),
                   JobSchedule(kind="once", at="18:00", tz="Asia/Seoul", from_date="2026-09-05", count=1)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    executed = await sched.tick(datetime(2026, 9, 5, 20, 0, tzinfo=KST))

    assert executed == [job.job_id, job.job_id]     # two schedules due, two executions
    after = backend.ledger.get(job.job_id)
    assert after.executions == 2 and [s.done for s in after.schedules] == [1, 1]
    assert len(after.run_ids) == 2                  # one api × one env × no data, twice


async def _matrix_report(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="dev/stg 비교", api_ids=["api-001", "api-007"],
        target_envs=["dev", "stg"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    return backend, reports, job


async def test_report_matrix_rows_flag_env_differences(tmp_path):
    _, _, job = await _matrix_report(tmp_path)
    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))

    assert data["matrix"]["envs"] == ["dev", "stg"]
    assert data["matrix"]["differs_count"] == 1
    rows = {r["api_id"]: r for r in data["matrix"]["rows"]}
    assert rows["api-007"]["differs"] is True
    assert rows["api-007"]["cells"]["dev"]["status"] == "pass"
    assert rows["api-007"]["cells"]["stg"]["status"] == "error"
    assert rows["api-007"]["cells"]["stg"]["run_id"].startswith("run-")
    assert rows["api-001"]["differs"] is False
    assert rows["api-001"]["test_data_label"] is None
    assert data["summary"]["total"] == 4
    assert data["summary"]["by_env"]["dev"] == {"total": 2, "pass": 2, "fail": 0, "error": 0}
    assert data["summary"]["by_env"]["stg"] == {"total": 2, "pass": 1, "fail": 0, "error": 1}


async def test_report_matrix_uses_the_latest_run_per_cell(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="dev/stg 비교 반복", api_ids=["api-001", "api-007"],
        target_envs=["dev", "stg"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=2)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    first = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    first_stg_run_id = {r["api_id"]: r for r in first["matrix"]["rows"]}["api-007"]["cells"]["stg"]["run_id"]

    assert await sched.tick(datetime(2026, 9, 6, 9, 0, tzinfo=KST)) == [job.job_id]

    second = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in second["matrix"]["rows"]}
    # Latest-run semantics: the matrix reflects the second execution, not a merge of both.
    assert len(second["runs"]) == 8
    assert rows["api-007"]["cells"]["stg"]["run_id"] != first_stg_run_id
    assert rows["api-007"]["differs"] is True


async def test_report_template_renders_the_comparison_table(tmp_path):
    _, reports, job = await _matrix_report(tmp_path)
    html = reports.read_html(job.job_id)

    assert '"envs": ["dev", "stg"]' in html and '"differs_count": 1' in html
    template_source = TEMPLATE.read_text(encoding="utf-8")
    assert "compare-rows" in template_source and "차이" in template_source
    assert "d.job.target_envs.join" in template_source
    assert "esc(" in template_source


async def test_report_matrix_rows_carry_both_test_data_labels(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="two data sets", api_ids=["api-001"], target_envs=["dev"],
        test_data=[TestDataSet(label="S1 정상", values={"amount": "1000"}),
                   TestDataSet(label="S2 음수", values={"amount": "-1"})]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    labels = {r["test_data_label"] for r in data["matrix"]["rows"]}
    assert labels == {"S1 정상", "S2 음수"}
    for row in data["matrix"]["rows"]:
        assert row["test_data_label"] in {"S1 정상", "S2 음수"}


async def test_report_by_env_shows_zeros_for_an_env_that_produced_no_runs(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="two envs, one silent", api_ids=["api-001"],
        target_envs=["dev", "stg"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    produced = await backend.execute_job_once(SESSION, job.job_id)
    dev_only = [r for r in produced if r.target_env == "dev"]
    applied = backend.ledger.get(job.job_id)

    reports.write(applied, dev_only)

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_env"]["stg"] == {"total": 0, "pass": 0, "fail": 0, "error": 0}
    assert data["summary"]["by_env"]["dev"]["total"] == len(dev_only)


def test_matrix_row_missing_one_envs_cell_is_not_flagged_as_differing():
    job = JobSpec(job_id="job-1", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"],
                 target_envs=["dev", "stg"], created_at=datetime.now(UTC), created_by="op")
    run = RunResult(run_id="run-1", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                    status=RunStatus.PASS, job_id="job-1")

    result = _matrix(job, [run])

    row = result["rows"][0]
    assert row["differs"] is False
    assert "stg" not in row["cells"]


async def _parity_report(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="legacy/renewed 값 비교",
        api_ids=["api-002", "api-004", "api-008"], target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    return backend, reports, job


async def test_parity_block_has_per_row_verdicts(tmp_path):
    _, _, job = await _parity_report(tmp_path)
    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))

    parity = data["parity"]
    assert parity is not None
    assert parity["targets"] == ["legacy", "renewed"]
    rows = {r["api_id"]: r for r in parity["rows"]}
    assert set(rows) == {"api-002", "api-004", "api-008"}
    for row in rows.values():
        assert row["verdict"] in {"equal", "status_diff", "value_diff"}

    api004 = rows["api-004"]
    assert api004["verdict"] == "value_diff"
    assert "$.limit" in api004["diff_paths"]
    assert "$.serverTime" in api004["diff_paths"]


async def test_parity_clusters_group_the_servertime_only_rows(tmp_path):
    _, _, job = await _parity_report(tmp_path)
    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))

    clusters = data["parity"]["clusters"]
    servertime_only = next(c for c in clusters if c["paths"] == ["$.serverTime"])
    assert servertime_only["count"] == 2
    assert set(servertime_only["row_keys"]) == {"api-002|", "api-008|"}
    assert not any(c["paths"] == ["$.limit", "$.serverTime"] and c["count"] > 1 for c in clusters)


async def test_rediff_reapplies_ignore_paths_without_touching_the_backend(tmp_path):
    backend, reports, job = await _parity_report(tmp_path)
    runs_before = len(backend.runs)

    path = reports.rediff(job.job_id, ["$.serverTime"])

    assert len(backend.runs) == runs_before   # no new runs, no backend call at all
    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    parity = data["parity"]
    assert parity["ignore_paths"] == ["$.serverTime"]
    rows = {r["api_id"]: r for r in parity["rows"]}
    assert rows["api-004"]["verdict"] == "value_diff"
    assert rows["api-004"]["diff_paths"] == ["$.limit"]
    assert rows["api-002"]["verdict"] == "equal"
    assert rows["api-008"]["verdict"] == "equal"
    assert path == tmp_path / job.job_id / "index.html"
    html = path.read_text(encoding="utf-8")
    assert "\\u003c" in html or "parity" in html


async def test_report_single_env_job_has_no_comparison_table_data(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="dev only", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["matrix"]["envs"] == ["dev"]
    assert data["matrix"]["differs_count"] == 0
    assert all(r["differs"] is False for r in data["matrix"]["rows"])
    assert data["parity"] is None                       # single-target job: no parity block, no crash

    template_source = TEMPLATE.read_text(encoding="utf-8")
    assert "envs.length>1" in template_source or "envs.length > 1" in template_source


async def test_tick_survives_a_scheduling_failure_and_still_runs_the_good_job(tmp_path):
    now = datetime.now(UTC)
    bad_schedule = JobSchedule.model_construct(
        kind="daily", at="09:00", tz="Nowhere/Bogus", from_date="2026-09-04", count=1, done=0
    )
    bad_job = JobSpec.model_construct(
        job_id="job-bad", kind=JobKind.SCHEDULED_RUN, status=JobStatus.APPLIED, summary="bad tz",
        api_ids=["api-1"], select_where=None, binding="FROZEN", target_envs=["dev"],
        schedules=[bad_schedule], test_data=[], report=False, confidence={}, assumptions=[],
        guardrail_notes=[], created_at=now, created_by="op", created_by_kind="operator",
        applied_at=now, applied_by="op", discarded_at=None, discarded_by=None,
        discarded_by_kind=None, run_ids=[], executions=0,
    )
    good_job = JobSpec(job_id="job-good", kind=JobKind.RUN_NOW, status=JobStatus.APPLIED,
                       summary="good", api_ids=["api-1"], target_envs=["dev"], report=False,
                       created_at=now, created_by="op", executions=0)
    run = RunResult(run_id="run-x", api_id="api-1", executed_at=now, target_env="dev",
                    status=RunStatus.PASS, job_id="job-good")

    class TwoJobBackend(RecordingBackend):
        def __init__(self):
            super().__init__(good_job, run)
            self.notes: list[tuple[str, str]] = []
            self.executed_job_ids: list[str] = []

        async def applied_jobs(self, session):
            self.calls.append("applied_jobs")
            return [bad_job, good_job]

        async def execute_job_once(self, session, job_id, schedule_index=None):
            self.executed_job_ids.append(job_id)
            return await super().execute_job_once(session, job_id, schedule_index)

        async def add_guardrail_note(self, session, job_id, note):
            self.notes.append((job_id, note))
            return await super().add_guardrail_note(session, job_id, note)

    backend = TwoJobBackend()
    sched = Scheduler(backend, Reports(tmp_path), SESSION)

    executed = await sched.tick(now)

    assert executed == ["job-good"]
    assert backend.executed_job_ids == ["job-good"]     # the bad job never reaches execute_job_once
    assert backend.notes == [("job-bad", "scheduling failed: ZoneInfoNotFoundError")]
    assert "record_execution" in backend.calls


async def test_report_rows_link_back_to_the_portal_attach_url(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path, portal_origin="http://portal.local:3110")
    job = await backend.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="now", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    produced = await backend.execute_job_once(SESSION, job.job_id)
    html = reports.write(backend.ledger.get(job.job_id), produced).read_text(encoding="utf-8")
    assert "?attach=run:" in html   # template builds the link from data.portal_origin
    assert "portal.local:3110" in html.replace("\\u003c", "<")


async def test_scheduler_uses_only_the_backend_abc(tmp_path):
    now = datetime.now(UTC)
    job = JobSpec(job_id="job-01", kind=JobKind.RUN_NOW, status=JobStatus.APPLIED, summary="s",
                 api_ids=["api-1"], target_envs=["dev"], report=True, created_at=now, created_by="op",
                 executions=0)
    run = RunResult(run_id="run-x", api_id="api-1", executed_at=now, target_env="dev",
                    status=RunStatus.PASS, job_id="job-01")
    backend = RecordingBackend(job, run)
    assert not hasattr(backend, "ledger")
    assert not hasattr(backend, "runs")

    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    executed = await sched.tick(now)

    assert executed == ["job-01"]
    assert "execute_job_once" in backend.calls
    assert "applied_jobs" in backend.calls
    assert not hasattr(backend, "ledger")
    assert not hasattr(backend, "runs")
