"""Task 9 (scale spec §9): the report is three incremental files instead of one growing
``data.json``, the scheduler iterates ``active_jobs`` and hands the report only THIS
occurrence's runs, and ``execute_job_once`` chunks its write as well as its matrix loop."""
import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    JobSchedule,
    RunResult,
    RunStatus,
    TestDataSet,
)
from atworks_host import mock_backend
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import TEMPLATE, Reports
from atworks_host.scheduler import Scheduler

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="sched", project_id="mes", operator="scheduler")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


def _body_loader(backend):
    """What the scheduler hands ``Reports.write``: bodies come from the store one run at a time,
    only for the cells parity compares -- never carried inside the run records themselves."""
    async def load(run_id: str):
        return await backend.get_body(SESSION, run_id)
    return load


async def _scheduled_two_env_job(tmp_path, *, count: int = 2):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path, capture_disabled=backend.capture_disabled)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="legacy/renewed 반복", api_ids=["api-002", "api-004"],
        target_envs=["legacy", "renewed"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05",
                               count=count)]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    return backend, reports, sched, job


# -- report file layout -------------------------------------------------------------------------


async def test_report_writes_runs_jsonl_parity_json_and_a_runless_data_json(tmp_path):
    backend, reports, sched, job = await _scheduled_two_env_job(tmp_path)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    folder = tmp_path / job.job_id

    lines = [json.loads(x) for x in (folder / "runs.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 4                                   # 2 apis × 2 envs × 1 occurrence
    assert all("response_body" not in row for row in lines)   # bodies never land in the artifact

    data = json.loads((folder / "data.json").read_text(encoding="utf-8"))
    assert "runs" not in data                                 # THE cut: no runs array any more
    assert data["runs_total"] == 4
    assert set(data) == {"job", "summary", "matrix", "parity", "runs_total", "provenance", "portal_origin"}

    parity = json.loads((folder / "parity.json").read_text(encoding="utf-8"))
    assert parity == data["parity"]
    for row in parity["rows"]:                                # the block carries its own inputs
        assert row["a_run_id"] and row["b_run_id"] and row["a_status"] and row["b_status"]
    assert reports.parity(job.job_id) == parity


async def test_report_html_embeds_only_the_latest_rows_and_links_to_the_portal(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="many runs", api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    base = datetime(2026, 9, 5, tzinfo=UTC)
    many = [RunResult(run_id=f"run-8{i:03d}", api_id="api-001", executed_at=base + timedelta(minutes=i),
                      target_env="dev", status=RunStatus.PASS, http_status=200, job_id=job.job_id)
            for i in range(250)]

    path = await reports.write(job, many, body_loader=_body_loader(backend))

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["runs_total"] == 250
    html = path.read_text(encoding="utf-8")
    embedded = json.loads(html.split('type="application/json">')[1].split("</script>")[0].replace("\\u003c", "<"))
    assert embedded["runs_shown"] == 200 and len(embedded["runs"]) == 200
    assert embedded["runs"][0]["run_id"] == "run-8249"        # newest first
    assert "run-8000" not in {r["run_id"] for r in embedded["runs"]}
    assert "전체 run은 포털에서" in TEMPLATE.read_text(encoding="utf-8")


async def test_report_counts_both_occurrences_of_a_scheduled_job(tmp_path):
    backend, reports, sched, job = await _scheduled_two_env_job(tmp_path)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 6, 9, 0, tzinfo=KST)) == [job.job_id]

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["runs_total"] == 8 and data["summary"]["total"] == 8
    assert len(reports.all_runs(job.job_id)) == 8
    # ...while parity still judges the LATEST run per cell, one row per (api, data) pair.
    assert len(data["parity"]["rows"]) == 2


async def test_rediff_works_with_no_runs_array_in_data_json(tmp_path):
    backend, reports, sched, job = await _scheduled_two_env_job(tmp_path, count=1)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert "runs" not in data
    runs_before = len(backend.runs)

    await reports.rediff(job.job_id, ["$.serverTime"], body_loader=_body_loader(backend))

    assert len(backend.runs) == runs_before                   # no new run, no re-execution
    after = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in after["parity"]["rows"]}
    assert rows["api-002"]["verdict"] == "equal"              # only the volatile field differed
    assert rows["api-004"]["diff_paths"] == ["$.limit"]       # the real one survives
    assert after["runs_total"] == 4 and "runs" not in after    # rediff touched nothing else


async def test_parity_row_says_expired_when_the_body_is_gone(tmp_path):
    """The second of the two missing-body causes (the first is `masking_disabled_groups`, see
    test_masking_capture): the body WAS captured but has aged past `retention_body_days`."""
    backend, reports, sched, job = await _scheduled_two_env_job(tmp_path, count=1)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert all(r["basis"] == "body" for r in reports.parity(job.job_id)["rows"])

    backend.store.conn().execute("DELETE FROM bodies")        # what Retention does at 90 days
    backend.store.conn().commit()
    await reports.rediff(job.job_id, [], body_loader=_body_loader(backend))

    for row in reports.parity(job.job_id)["rows"]:
        assert row["basis"] == "status_only"
        assert row["note"] == "본문 만료"
        assert row["diff_paths"] == []
    # api-002/api-004 both pass on both targets, so a status-only verdict reads "equal" --
    # never a fabricated body diff.
    assert {r["verdict"] for r in reports.parity(job.job_id)["rows"]} == {"equal"}


# -- scheduler ----------------------------------------------------------------------------------


async def test_tick_reads_active_jobs_and_never_touches_an_exhausted_one(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="one shot", api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert backend.ledger.get(job.job_id).remaining_executions == 0

    listed: list[str] = []
    original = backend.active_jobs

    async def counting_active_jobs(session):
        rows = await original(session)
        listed.extend(j.job_id for j in rows)
        return rows

    backend.active_jobs = counting_active_jobs
    statements: list[str] = []
    backend.store.conn().set_trace_callback(statements.append)
    try:
        assert await sched.tick(datetime(2026, 9, 6, 9, 0, tzinfo=KST)) == []
    finally:
        backend.store.conn().set_trace_callback(None)

    assert listed == []                                       # the exhausted job is not even listed
    assert statements == []                                   # ...and no SQL was issued for it


async def test_tick_runs_retention_at_the_tail_after_the_briefing(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    order: list[str] = []

    class RecordingBriefings:
        async def maybe_generate(self, backend_, session, now):
            order.append("briefing")

    class RecordingRetention:
        async def maybe_run(self, now):
            order.append("retention")
            return {}

    sched = Scheduler(backend, Reports(tmp_path), SESSION, briefings=RecordingBriefings(),
                      retention=RecordingRetention())
    await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST))

    assert order == ["briefing", "retention"]


async def test_a_failing_retention_job_never_breaks_the_tick(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)

    class BoomRetention:
        async def maybe_run(self, now):
            raise RuntimeError("disk full")

    sched = Scheduler(backend, Reports(tmp_path), SESSION, retention=BoomRetention())
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="now", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]


async def test_a_400_cell_execution_writes_in_chunks_not_one_transaction(monkeypatch):
    """The SSE breach the Task 7 bench measured was the unbroken 400-run ``store.ingest`` at the
    end of ``execute_job_once`` -- the matrix loop already yielded, the WRITE did not. The two
    batch sizes are DIFFERENT knobs (Task 9 review): `max_concurrency` spaces the generation
    yields, `ingest_chunk_size` sizes the write transactions."""
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    template = backend.apis["api-001"]
    api_ids = [f"api-9{i:03d}" for i in range(config.max_apis_per_job)]
    for api_id in api_ids:
        backend.apis[api_id] = template.model_copy(update={"api_id": api_id, "params": ["amount"]})
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="400 cells", api_ids=api_ids, target_envs=["dev", "stg"],
        test_data=[TestDataSet(label="a", values={"amount": "1"}),
                   TestDataSet(label="b", values={"amount": "2"})]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    sizes: list[int] = []
    original = backend.store.ingest

    def counting_ingest(runs, *args, **kwargs):
        rows = list(runs)
        sizes.append(len(rows))
        return original(rows, *args, **kwargs)

    backend.store.ingest = counting_ingest

    yields = {"n": 0}
    real_sleep = asyncio.sleep

    async def counting_sleep(delay, *args, **kwargs):
        if delay == 0:
            yields["n"] += 1
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(mock_backend.asyncio, "sleep", counting_sleep)
    produced = await backend.execute_job_once(SESSION, job.job_id)

    generation_yields = -(-400 // config.max_concurrency)      # ceil: one per matrix batch
    ingest_chunks = -(-400 // config.ingest_chunk_size)        # ceil: one transaction per chunk
    assert len(produced) == 400
    assert len(sizes) == ingest_chunks                         # 8, not 100 (max_concurrency=4)
    assert max(sizes) <= config.ingest_chunk_size
    assert sum(sizes) == 400
    # the generation loop still hands the loop back on `max_concurrency`, independently of the
    # (much larger) write chunk: the two yield counts add up, they don't replace each other.
    assert yields["n"] >= generation_yields
    assert yields["n"] == generation_yields + ingest_chunks
    assert backend.ledger.get(job.job_id).run_count == 400     # exactly one record_execution


# -- 인입 중간 실패 (Task 9 리뷰 Important 2) -----------------------------------------------------


def _failing_ingest(backend, fail_on: int):
    """Wrap ``store.ingest`` so the ``fail_on``-th call (1-based) raises. Returns the wrapper."""
    original = backend.store.ingest
    calls = {"n": 0}

    def ingest(runs, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise sqlite3.OperationalError("database is locked")
        return original(list(runs), *args, **kwargs)

    backend.store.ingest = ingest
    return calls


async def _four_cell_job(chunk: int = 2):
    config = AtworksAgentConfig(model="m", ingest_chunk_size=chunk)
    backend = MockAtworks(config, FIXTURES)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="4 cells",
        api_ids=["api-001", "api-002", "api-003", "api-004"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    return config, backend, job


async def test_a_mid_chunk_ingest_failure_records_the_slot_once_and_never_re_raises():
    """The ruling: ``execute_job_once`` OWNS the failure. Re-raising would send the scheduler down
    its exception path, which calls ``record_execution`` a second time -- two slots spent on one
    occurrence. Instead the committed chunks are recorded, the reason becomes a guardrail note,
    and the committed runs come back so the report is built over what actually exists."""
    config, backend, job = await _four_cell_job()
    before = backend.store.run_count()
    _failing_ingest(backend, fail_on=2)

    produced = await backend.execute_job_once(SESSION, job.job_id)      # no exception escapes

    assert len(produced) == config.ingest_chunk_size                    # only the committed chunk
    assert backend.store.run_count() == before + config.ingest_chunk_size
    assert all(run.run_id in backend.runs for run in produced)          # ...and they are really there
    current = backend.ledger.get(job.job_id)
    assert current.executions == 1                                     # exactly one slot spent
    assert current.run_count == config.ingest_chunk_size                # ledger matches the store
    assert any("ingest failed after 1/2 chunks: OperationalError" in note
               for note in current.guardrail_notes)


async def test_the_scheduler_spends_exactly_one_slot_when_ingest_fails_mid_execution(tmp_path):
    config = AtworksAgentConfig(model="m", ingest_chunk_size=2)
    backend = MockAtworks(config, FIXTURES)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="4 cells",
        api_ids=["api-001", "api-002", "api-003", "api-004"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05",
                               count=2)]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    _failing_ingest(backend, fail_on=2)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)

    await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST))

    current = backend.ledger.get(job.job_id)
    assert current.executions == 1                                     # NOT 2 -- one occurrence
    assert current.schedules[0].done == 1
    assert current.run_count == 2


async def test_a_body_loader_that_raises_degrades_one_row_not_the_whole_report(tmp_path):
    """Important 1: ``_load_bodies`` documented an exception fallback it did not implement -- a
    loader raising took the whole report down with it."""
    backend, reports, sched, job = await _scheduled_two_env_job(tmp_path, count=1)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    doomed = reports.parity(job.job_id)["rows"][0]["a_run_id"]

    async def flaky_loader(run_id: str):
        if run_id == doomed:
            raise sqlite3.OperationalError("no such table: bodies")
        return await backend.get_body(SESSION, run_id)

    await reports.rediff(job.job_id, [], body_loader=flaky_loader)      # does not raise

    rows = {r["a_run_id"]: r for r in reports.parity(job.job_id)["rows"]}
    assert rows[doomed]["basis"] == "status_only" and rows[doomed]["note"] == "본문 만료"
    others = [r for a_run_id, r in rows.items() if a_run_id != doomed]
    assert others and all(r["basis"] == "body" for r in others)        # every other row survived
