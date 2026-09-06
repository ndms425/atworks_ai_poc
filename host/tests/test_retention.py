"""Task 9 (scale spec §6): the daily retention job. Hot runs move to a cold partition (never
deleted), bodies are the one tier that is really deleted, the archive is only purged once
``retention_cold_until`` is set AND past, the insight narration cache rotates, idle sessions are
swept -- and the whole thing runs once per local day at the scheduler tick's tail."""
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    RunResult,
    RunsQuery,
    RunStatus,
)
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.retention import Retention, TimestampedSessionStore
from atworks_host.scheduler import Scheduler
from atworks_host.store import Store

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="ret", project_id="mes", operator="operator")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _State(BaseModel):
    note: str = ""


def _backend(**config_kwargs) -> MockAtworks:
    return MockAtworks(AtworksAgentConfig(model="m", **config_kwargs), FIXTURES)


def _seed(store: Store, *, age_days: int, count: int, prefix: str) -> list[str]:
    """``count`` runs aged ``age_days``, ingested through the real write path so the rollups,
    watermarks and cell state are materialized exactly as production would have them."""
    runs = [
        RunResult(run_id=f"{prefix}-{i:03d}", api_id="api-001", executed_at=NOW - timedelta(days=age_days),
                  target_env="dev", status=RunStatus.PASS, http_status=200,
                  response_body={"path": "/x", "seq": i}, job_id="job-x", executed_by="operator")
        for i in range(count)
    ]
    store.ingest(runs)
    return [r.run_id for r in runs]


def _rollup_totals(store: Store) -> list[tuple]:
    return [tuple(r) for r in store.conn().execute(
        'SELECT day, api_id, "count", "pass", fail, error FROM rollup_day ORDER BY day, api_id')]


# -- 핫 -> 콜드 이관 -----------------------------------------------------------------------------


def test_runs_past_the_hot_window_are_archived_not_deleted(tmp_path):
    backend = _backend()
    store = backend.store
    old_ids = _seed(store, age_days=181, count=5, prefix="old")
    fresh_ids = _seed(store, age_days=10, count=3, prefix="new")
    total_before = store.run_count()
    rollups_before = _rollup_totals(store)

    counts = Retention(store, backend._config, tmp_path).run(NOW)

    assert counts["runs_archived"] == 5
    archived = store.count_runs(archived=True)
    assert store.run_count() + archived == total_before          # not one row lost
    assert store.run_count() == total_before - 5
    # the hot partition no longer answers for them...
    assert store.list_runs(RunsQuery(limit=200)).total == total_before - 5
    for run_id in old_ids:
        assert store.get_run(run_id) is None
    # ...and the explicit archived read does, with the same envelope.
    cold = store.list_runs(RunsQuery(limit=200, archived=True))
    assert {r.run_id for r in cold.items} == set(old_ids)
    assert cold.total == 5
    assert {r.run_id for r in store.list_runs(RunsQuery(limit=200)).items} >= set(fresh_ids)
    # 집계·롤업은 영구 — a cold move must not change a single aggregate answer.
    assert _rollup_totals(store) == rollups_before


def test_the_archive_is_only_purged_once_retention_cold_until_is_set_and_past(tmp_path):
    store = _backend().store
    _seed(store, age_days=181, count=4, prefix="old")

    # 1. no cutoff at all (the default) = 영구 보관.
    forever = _backend()
    Retention(store, forever._config, tmp_path).run(NOW)
    assert store.count_runs(archived=True) == 4

    # 2. a cutoff that has NOT passed yet still keeps everything.
    future = _backend(retention_cold_until=date(2027, 1, 1))
    assert Retention(store, future._config, tmp_path).run(NOW)["archive_purged"] == 0
    assert store.count_runs(archived=True) == 4

    # 3. only a cutoff in the past deletes -- the one place a run record ever goes away.
    past = _backend(retention_cold_until=date(2026, 1, 1))
    assert Retention(store, past._config, tmp_path).run(NOW)["archive_purged"] == 4
    assert store.count_runs(archived=True) == 0


# -- 본문 삭제 ------------------------------------------------------------------------------------


def test_bodies_past_the_body_window_are_deleted_and_fresh_ones_are_not(tmp_path):
    backend = _backend()
    store = backend.store
    old_ids = _seed(store, age_days=91, count=3, prefix="oldbody")
    fresh_ids = _seed(store, age_days=5, count=2, prefix="newbody")
    assert all(store.get_body(run_id) is not None for run_id in old_ids + fresh_ids)

    counts = Retention(store, backend._config, tmp_path).run(NOW)

    assert counts["bodies_deleted"] == 3
    assert all(store.get_body(run_id) is None for run_id in old_ids)
    assert all(store.get_body(run_id) is not None for run_id in fresh_ids)
    # the run records themselves are untouched -- only the body went.
    assert all(store.get_run(run_id) is not None for run_id in old_ids)


async def test_an_old_reports_parity_rediffs_to_status_only_after_the_bodies_expire(tmp_path):
    """A parity report older than the body window still re-diffs -- it just falls back to a
    status-only verdict with the "본문 만료" note instead of inventing a body comparison."""
    backend = _backend()
    reports = Reports(tmp_path / "reports", capture_disabled=backend.capture_disabled)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="parity", api_ids=["api-002", "api-004"],
        target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert all(r["basis"] == "body" for r in reports.parity(job.job_id)["rows"])

    # age every captured body past the window, then run retention for real.
    backend.store.conn().execute("UPDATE bodies SET captured_at = ?", ("2026-01-01T00:00:00.000000Z",))
    backend.store.conn().commit()
    assert Retention(backend.store, backend._config, tmp_path).run(NOW)["bodies_deleted"] > 0

    async def body_loader(run_id):
        return await backend.get_body(SESSION, run_id)

    await reports.rediff(job.job_id, [], body_loader=body_loader)

    for row in reports.parity(job.job_id)["rows"]:
        assert row["basis"] == "status_only"
        assert row["note"] == "본문 만료"


# -- 파생: 인사이트 서술 캐시 회전 ------------------------------------------------------------------


def test_insight_cache_folders_older_than_the_window_are_rotated(tmp_path):
    backend = _backend()
    insights_dir = tmp_path / "insights_out"
    local_today = NOW.astimezone(timezone(timedelta(hours=9))).date()
    stale = local_today - timedelta(days=31)
    fresh = local_today - timedelta(days=3)
    for operator in ("minseong", "jihye"):
        for day in (stale, fresh):
            folder = insights_dir / operator / day.isoformat()
            folder.mkdir(parents=True)
            (folder / "narrative.json").write_text("{}", encoding="utf-8")
    (insights_dir / "minseong" / "not-a-date").mkdir()        # never touched: not identifiable

    counts = Retention(backend.store, backend._config, insights_dir).run(NOW)

    assert counts["insight_cache_rotated"] == 2
    for operator in ("minseong", "jihye"):
        assert not (insights_dir / operator / stale.isoformat()).exists()
        assert (insights_dir / operator / fresh.isoformat() / "narrative.json").exists()
    assert (insights_dir / "minseong" / "not-a-date").exists()


# -- 세션 TTL --------------------------------------------------------------------------------------


def test_idle_sessions_are_swept_and_active_ones_are_kept(tmp_path):
    backend = _backend()
    clock = {"now": NOW - timedelta(hours=48)}
    sessions = TimestampedSessionStore(_State, clock=lambda: clock["now"])
    idle = sessions.start("minseong")
    clock["now"] = NOW - timedelta(minutes=5)
    active = sessions.start("jihye")

    counts = Retention(backend.store, backend._config, tmp_path, sessions).run(NOW)

    assert counts["sessions_swept"] == 1
    assert sessions.read_state(active.session_id) is not None
    assert sessions.read_state(idle.session_id) is None


def test_reading_a_session_keeps_it_alive(tmp_path):
    backend = _backend()
    clock = {"now": NOW - timedelta(hours=48)}
    sessions = TimestampedSessionStore(_State, clock=lambda: clock["now"])
    record = sessions.start("minseong")
    clock["now"] = NOW                                        # the operator comes back
    sessions.require(record.session_id)

    assert Retention(backend.store, backend._config, tmp_path, sessions).run(NOW)["sessions_swept"] == 0
    assert sessions.read_state(record.session_id) is not None


# -- 하루 1회 가드 ----------------------------------------------------------------------------------


def test_maybe_run_runs_once_per_local_day(tmp_path):
    backend = _backend()
    store = backend.store
    retention = Retention(store, backend._config, tmp_path)

    first = retention.maybe_run(NOW)
    assert first is not None
    # NOW is 21:00 in briefing_tz (Asia/Seoul), so +1h/+2h are still the same LOCAL day --
    # which is the day the guard is keyed on, not the UTC one.
    assert retention.maybe_run(NOW + timedelta(hours=1)) is None
    assert retention.maybe_run(NOW + timedelta(hours=2)) is None

    tomorrow = retention.maybe_run(NOW + timedelta(days=1))
    assert tomorrow is not None

    state = store.retention_state()
    day = retention.local_date(NOW).isoformat()
    assert f"daily:{day}" in state
    # every step writes its own row too, so what ran on which day is itself evidence
    assert {key.split(":")[0] for key in state} >= {
        "daily", "runs_archived", "bodies_deleted", "archive_purged",
        "insight_cache_rotated", "sessions_swept"}


async def test_the_scheduler_tick_tail_runs_retention_once_a_day(tmp_path):
    backend = _backend()
    _seed(backend.store, age_days=181, count=2, prefix="old")
    retention = Retention(backend.store, backend._config, tmp_path)
    sched = Scheduler(backend, Reports(tmp_path / "r"), SESSION, retention=retention)

    await sched.tick(NOW)
    assert backend.store.count_runs(archived=True) == 2

    _seed(backend.store, age_days=181, count=2, prefix="old2")
    await sched.tick(NOW + timedelta(hours=1))                # same local day -> guarded off
    assert backend.store.count_runs(archived=True) == 2


def test_retention_state_survives_the_pre_task9_table_shape(tmp_path):
    """A store written before Task 9 carries `retention_state(key, value)`; the schema migration
    reshapes it (nothing ever wrote it, so there is nothing to lose)."""
    path = tmp_path / "old.sqlite"
    legacy = Store(path)
    legacy.conn().execute("DROP TABLE retention_state")
    legacy.conn().execute("CREATE TABLE retention_state (key TEXT PRIMARY KEY, value TEXT)")
    legacy.conn().commit()
    del legacy

    store = Store(path)
    store.set_retention_state("daily:2026-09-06", NOW)
    assert store.get_retention_state("daily:2026-09-06") is not None
    assert json.dumps(sorted(store.retention_state())) == '["daily:2026-09-06"]'
