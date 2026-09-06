"""Ingest-time materialization at the SQL level (scale spec 2026-09-06 §4, Task 5).

`Store.ingest` is the single write path for runs: one transaction folds a batch into
`current_state` / `rollup_day` / `api_watermark` / `operator_api`. These tests hold it to two
promises — **equality** with a from-scratch recompute (the `recompute_*` oracles kept on the
Store, plus `aggregation.aggregate`) no matter how the batch is sliced, and **idempotency**
(re-ingesting a batch changes nothing anywhere).

The pure arithmetic has its own property test at `atworks-agent/core/tests/test_materialize.py`.
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    ApiSpec,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    RunResult,
    RunStatus,
    TestDataSet,
)
from atworks_agent.aggregation import aggregate
from atworks_host.mock_backend import MockAtworks
from atworks_host.store import Store

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong",
                                now=datetime(2026, 9, 3, 14, tzinfo=KST))
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
BASE = datetime(2026, 8, 20, tzinfo=UTC)
APIS = {
    f"api-{i:03d}": ApiSpec(api_id=f"api-{i:03d}", method="GET", path=f"/v1/thing/{i}",
                            name=f"thing {i}", updated_at=BASE)
    for i in range(1, 6)
}


def _random_runs(seed: int, count: int = 200) -> list[RunResult]:
    """200 runs over 5 apis × 2 envs × 2 data (one of them the *unbound* None label, which is
    what exercises the '' NULL sentinel) across 10 days. Distinct executed_at throughout."""
    rng = random.Random(seed)
    statuses = [RunStatus.PASS, RunStatus.FAIL, RunStatus.ERROR]
    runs = []
    for i in range(count):
        status = rng.choices(statuses, weights=[6, 3, 1])[0]
        moment = BASE + timedelta(minutes=i * 61)
        runs.append(RunResult(
            run_id=f"run-{i:04d}", api_id=rng.choice(sorted(APIS)), executed_at=moment,
            target_env=rng.choice(["dev", "stg"]), test_data_label=rng.choice(["S1", None]),
            status=status, failed_rules=[] if status is RunStatus.PASS else ["amount >= 0"],
            http_status=200 if status is not RunStatus.ERROR else 503,
            duration_ms=100 + (i % 50), executed_by=rng.choice(["minseong", "jiwon", None]),
        ))
    return runs


def _rollup_rows(store: Store) -> list[dict]:
    return [dict(r) for r in store.conn().execute(
        "SELECT * FROM rollup_day ORDER BY day, api_id, target_env, test_data_label").fetchall()]


def _snapshot(store: Store) -> dict:
    """Every materialized row, as plain data, for the idempotency comparison."""
    conn = store.conn()
    def rows(table: str) -> list[dict]:
        # sorted so the comparison is about values, never about SQLite's row order
        raw = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        return sorted(raw, key=lambda r: json.dumps(r, sort_keys=True, default=str))

    return {t: rows(t) for t in
            ("runs", "bodies", "current_state", "rollup_day", "api_watermark", "operator_api")}


# -- property: incremental ingest == recompute from scratch --------------------------------------


def test_incremental_ingest_matches_the_recompute_oracles():
    runs = _random_runs(seed=20260906)
    store = Store(":memory:")
    rng = random.Random(3)
    ordered = sorted(runs, key=lambda r: (r.executed_at, r.run_id))
    batches, start, inserted = [], 0, 0
    while start < len(ordered):
        batch = ordered[start:start + rng.randint(1, 23)]
        start += len(batch)
        batches.append(batch)
        inserted += store.ingest(batch, briefing_tz="Asia/Seoul")
    assert len(batches) >= 10 and inserted == len(runs)

    # current_state (materialized table) == current_state (scan of runs)
    materialized = {(c.api_id, c.target_env, c.test_data_label): c for c in store.current_state()}
    oracle = {(c.api_id, c.target_env, c.test_data_label): c for c in store.recompute_current_state()}
    assert materialized == oracle
    assert all(c.test_data_label in ("S1", None) for c in materialized.values())   # '' mapped back

    # api_watermark == the scan, including the filtered read
    assert {w.api_id: w for w in store.watermarks()} == {w.api_id: w for w in store.recompute_watermarks()}
    since = BASE + timedelta(days=3)
    assert ({w.api_id: w for w in store.watermarks(first_non_pass_since=since)}
            == {w.api_id: w for w in store.recompute_watermarks(first_non_pass_since=since)})

    # operator_api == the scan, for every operator and a window that spans everything
    now = BASE + timedelta(days=11)
    for operator in ("minseong", "jiwon", "nobody"):
        assert (store.operator_scope_ids(operator, 30, now)
                == store.recompute_operator_scope_ids(operator, 30, now))
    assert store.operator_scope_ids("minseong", 30, now)          # non-empty: the test means something

    # rollup_day sums == aggregation.aggregate, per cell and overall
    groups = {g.key: g for g in aggregate(runs, APIS, "api_env_data", flaky_min_transitions=3)}
    per_cell: dict[tuple, dict] = {}
    for row in _rollup_rows(store):
        label = row["test_data_label"] or None
        bucket = per_cell.setdefault((row["api_id"], row["target_env"], label),
                                     {"count": 0, "pass": 0, "fail": 0, "error": 0, "transitions": 0})
        for field in bucket:
            bucket[field] += row[field]
    assert len(per_cell) == len(groups)
    for (api_id, env, label), bucket in per_cell.items():
        group = groups[f"{api_id}|{env}|{label or '-'}"]
        assert (bucket["count"], bucket["pass"], bucket["fail"], bucket["error"]) == (
            group.count, group.passed, group.fail, group.error)
        assert bucket["transitions"] == group.transitions
        assert materialized[(api_id, env, label)].transitions_total == group.transitions
    assert sum(b["count"] for b in per_cell.values()) == len(runs)
    assert sum(b["fail"] for b in per_cell.values()) == sum(1 for r in runs if r.status is RunStatus.FAIL)

    # per-api rollups agree with the api-level aggregate too (the same rows, summed differently)
    by_api = {g.key: g for g in aggregate(runs, APIS, "api", flaky_min_transitions=3)}
    api_counts: dict[str, int] = {}
    for row in _rollup_rows(store):
        api_counts[row["api_id"]] = api_counts.get(row["api_id"], 0) + row["count"]
    assert api_counts == {k: g.count for k, g in by_api.items()}

    # both key maps survived the merge as {key: {count, fail, error}} and fold back to exactly
    # the failed_rule / http_status groupings (Task 8: group_by="http_status" reads the second
    # map, so no axis falls back to a run scan)
    for column, group_by in (("failed_rule_counts", "failed_rule"), ("http_status_counts", "http_status")):
        folded: dict[str, dict[str, int]] = {}
        for row in _rollup_rows(store):
            for key, counts in json.loads(row[column] or "{}").items():
                bucket = folded.setdefault(key, {"count": 0, "fail": 0, "error": 0})
                for field in bucket:
                    bucket[field] += counts[field]
        expected = {g.key: g for g in aggregate(runs, APIS, group_by, flaky_min_transitions=3)}
        assert set(folded) == set(expected), group_by
        for key, bucket in folded.items():
            group = expected[key]
            assert (bucket["count"], bucket["fail"], bucket["error"]) == (group.count, group.fail, group.error), key
    total_failed = sum(
        sum(v["count"] for v in json.loads(row["failed_rule_counts"] or "{}").values())
        for row in _rollup_rows(store)
    )
    assert total_failed == sum(1 for r in runs if r.status is not RunStatus.PASS)

    # -- idempotency: every batch again, in a different order, changes nothing -------------------
    before = _snapshot(store)
    rng.shuffle(batches)
    for batch in batches:
        assert store.ingest(batch, briefing_tz="Asia/Seoul") == 0
    assert store.ingest(runs, briefing_tz="Asia/Seoul") == 0
    assert _snapshot(store) == before


def test_one_shot_ingest_equals_the_sliced_one():
    runs = _random_runs(seed=77, count=150)
    whole = Store(":memory:")
    whole.ingest(runs)
    sliced = Store(":memory:")
    ordered = sorted(runs, key=lambda r: (r.executed_at, r.run_id))
    for start in range(0, len(ordered), 7):
        sliced.ingest(ordered[start:start + 7])
    a, b = _snapshot(whole), _snapshot(sliced)
    for table in ("current_state", "api_watermark", "operator_api"):
        assert a[table] == b[table], table
    # p95 is §4's documented approximation and is the one field batching may move
    strip = lambda rows: [{k: v for k, v in r.items() if k != "p95_duration_ms"} for r in rows]  # noqa: E731
    assert strip(a["rollup_day"]) == strip(b["rollup_day"])


def test_unbound_test_data_label_does_not_fan_out_into_one_row_per_ingest():
    """The '' sentinel: two ingests into the same label-less cell must upsert one row, not two
    (SQLite's non-rowid PK treats NULL as distinct from NULL, so a NULL label would never
    conflict with itself)."""
    store = Store(":memory:")
    for i in range(2):
        store.ingest([RunResult(
            run_id=f"r{i}", api_id="api-001", executed_at=BASE + timedelta(hours=i),
            target_env="dev", test_data_label=None, status=RunStatus.PASS, day="2026-08-20")])
    rows = _rollup_rows(store)
    assert len(rows) == 1 and rows[0]["count"] == 2
    cells = store.current_state()
    assert len(cells) == 1 and cells[0].test_data_label is None and cells[0].run_id == "r1"


# -- golden parity on the shipped fixtures -------------------------------------------------------


def test_load_fixtures_materializes_exactly_what_a_recompute_would():
    store = Store(":memory:")
    store.load_fixtures(FIXTURES, briefing_tz="Asia/Seoul")
    assert ({(c.api_id, c.target_env, c.test_data_label): c for c in store.current_state()}
            == {(c.api_id, c.target_env, c.test_data_label): c for c in store.recompute_current_state()})
    assert {w.api_id: w for w in store.watermarks()} == {w.api_id: w for w in store.recompute_watermarks()}
    now = datetime(2026, 9, 3, 14, tzinfo=KST)
    for operator in ("minseong", "jiwon", "nobody"):
        assert (store.operator_scope_ids(operator, 30, now)
                == store.recompute_operator_scope_ids(operator, 30, now))
    assert store.current_state()          # the fixtures actually produced rows

    scoped = [c.api_id for c in store.current_state(["api-001"])]
    assert scoped and set(scoped) == {"api-001"}
    assert store.current_state([]) == []


def test_overwriting_an_already_ingested_run_rebuilds_the_materialized_tables():
    """`backend.runs[id] = ...` mutates history under folded deltas — no increment can undo
    that, so upsert_run rebuilds. The result must still equal a from-scratch recompute."""
    store = Store(":memory:")
    store.load_fixtures(FIXTURES, briefing_tz="Asia/Seoul")
    victim = store.fetch_runs()[0]
    flipped = RunStatus.FAIL if victim.status is RunStatus.PASS else RunStatus.PASS
    store.upsert_run(victim.model_copy(update={"status": flipped}), "Asia/Seoul")
    assert store.get_run(victim.run_id).status is flipped
    assert ({(c.api_id, c.target_env, c.test_data_label): c for c in store.current_state()}
            == {(c.api_id, c.target_env, c.test_data_label): c for c in store.recompute_current_state()})
    assert {w.api_id: w for w in store.watermarks()} == {w.api_id: w for w in store.recompute_watermarks()}


def test_masked_bodies_go_through_the_same_call():
    """The Task 6 hook: `ingest(mask=...)` rewrites bodies on the way in. Default stores them
    unchanged, which is what this task ships."""
    store = Store(":memory:")
    run = RunResult(run_id="r1", api_id="api-001", executed_at=BASE, target_env="dev",
                    status=RunStatus.PASS, response_body={"card": "4111-1111", "ok": True})
    store.ingest([run], mask=lambda body: {**body, "card": "****"})
    assert store.get_body("r1") == {"card": "****", "ok": True}

    plain = Store(":memory:")
    plain.ingest([run])
    assert plain.get_body("r1") == {"card": "4111-1111", "ok": True}


# -- the Mock's execution path -------------------------------------------------------------------


async def test_a_four_api_job_execution_materializes_everything_in_one_go():
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="네 개 돌려", target_envs=["dev"],
        api_ids=["api-001", "api-002", "api-003", "api-004"]), ActorKind.AGENT)
    applied = await backend.apply_job(SESSION, job.job_id)
    before = {(r["operator_id"], r["api_id"]): r["run_count"] for r in
              backend.store.conn().execute("SELECT * FROM operator_api").fetchall()}
    produced = await backend.execute_job_once(SESSION, job.job_id, None)
    assert len(produced) == 4

    after = backend.ledger.get(job.job_id)
    assert after.run_count == 4
    assert after.recent_run_ids == [r.run_id for r in reversed(produced)]   # newest first
    assert after.run_ids == []                     # scale spec §4: the old list stopped growing

    conn = backend.store.conn()
    # the execution's own partition -- the fixtures materialized their own days at load time
    day = conn.execute("SELECT day FROM runs WHERE run_id = ?", (produced[0].run_id,)).fetchone()["day"]
    rows = conn.execute(
        'SELECT api_id, target_env, test_data_label, "count" FROM rollup_day WHERE day = ?', (day,)
    ).fetchall()
    assert {r["api_id"] for r in rows} == {"api-001", "api-002", "api-003", "api-004"}
    assert all(r["test_data_label"] == "" for r in rows)          # unbound cells use the sentinel
    assert all(r["target_env"] == "dev" and r["count"] == 1 for r in rows)

    operator_rows = conn.execute(
        "SELECT api_id, run_count FROM operator_api WHERE operator_id = ?", (applied.applied_by,)
    ).fetchall()
    assert applied.applied_by == SESSION.operator
    counts = {r["api_id"]: r["run_count"] for r in operator_rows}
    for api_id in ("api-001", "api-002", "api-003", "api-004"):
        # exactly one more run attributed to the approving operator than before the execution
        assert counts[api_id] == before.get((applied.applied_by, api_id), 0) + 1
    assert backend.store.operator_scope_ids(applied.applied_by, 30, datetime.now(UTC)) >= {"api-001"}

    cells = {(c.api_id, c.target_env, c.test_data_label): c for c in backend.store.current_state()}
    for run in produced:
        assert cells[(run.api_id, run.target_env, run.test_data_label)].run_id == run.run_id


async def test_execute_job_once_yields_to_the_loop_across_a_400_cell_matrix(monkeypatch):
    """SSE-nonblocking proxy: the matrix is walked in `max_concurrency`-sized batches with a
    yield between them, so a 400-run execution hands the event loop back ~100 times instead of
    blocking it for the whole job."""
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    backend.apis = {
        f"api-{i:03d}": ApiSpec(api_id=f"api-{i:03d}", method="GET", path=f"/v1/load/{i}",
                                name=f"load {i}", updated_at=BASE, params=["amount"])
        for i in range(1, 101)
    }
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="400 cells", api_ids=sorted(backend.apis),
        target_envs=["dev", "stg"],
        test_data=[TestDataSet(label="S1", values={"amount": "1"}),
                   TestDataSet(label="S2", values={"amount": "2"})]), ActorKind.AGENT)
    assert job.matrix_size == 400
    await backend.apply_job(SESSION, job.job_id)

    yields = 0
    real_sleep = asyncio.sleep

    async def counting_sleep(delay, *args, **kwargs):
        nonlocal yields
        if delay == 0:
            yields += 1
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    produced = await backend.execute_job_once(SESSION, job.job_id, None)

    assert len(produced) == 400
    assert yields >= -(-400 // config.max_concurrency)      # ceil(400 / max_concurrency) == 100
    assert backend.ledger.get(job.job_id).run_count == 400
    assert len(backend.ledger.get(job.job_id).recent_run_ids) == 50    # bounded window
    # 100 apis × 2 envs × 2 labelled data sets = 400 distinct rollup cells, one run each
    # (the fixture runs materialized at load time carry no test_data_label, so they are excluded)
    counts = backend.store.conn().execute(
        'SELECT COUNT(*), SUM("count") FROM rollup_day WHERE test_data_label IN (?, ?)', ("S1", "S2")
    ).fetchone()
    assert tuple(counts) == (400, 400)
