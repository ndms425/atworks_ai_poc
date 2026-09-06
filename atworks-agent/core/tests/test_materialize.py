"""materialize.rollup_delta (scale spec 2026-09-06 §4) — 순수 계산부의 등가성 property.

배치를 아무렇게나 잘라 순차로 접어도(incremental) 결과가 처음부터 다시 계산한 것(oracle:
aggregation.aggregate)과 같아야 한다. 여기서는 SQL 없이 파이썬 dict 로만 접는다 — 저장소 쪽
트랜잭션·멱등성·NULL 센티널은 host/tests/test_ingest.py 가 본다.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from atworks_agent import CellState, RunStatus
from atworks_agent.aggregation import aggregate
from atworks_agent.materialize import (
    KeyCounts,
    OperatorApiDelta,
    RollupRow,
    cell_key,
    merge_watermark,
    rollup_delta,
)
from atworks_agent.types import ApiSpec, ApiWatermark, RunResult

BASE = datetime(2026, 8, 20, tzinfo=UTC)
APIS = {
    f"api-{i:03d}": ApiSpec(api_id=f"api-{i:03d}", method="GET", path=f"/v1/thing/{i}",
                            name=f"thing {i}", updated_at=BASE)
    for i in range(1, 6)
}
STATUSES = [RunStatus.PASS, RunStatus.FAIL, RunStatus.ERROR]


def make_runs(seed: int, count: int = 200) -> list[RunResult]:
    """5 apis × 2 envs × 2 data over 10 days. Every ``executed_at`` is distinct so the
    ``(executed_at, run_id)`` ordering the oracle and the delta share is unambiguous."""
    rng = random.Random(seed)
    runs: list[RunResult] = []
    for i in range(count):
        api_id = rng.choice(sorted(APIS))
        env = rng.choice(["dev", "stg"])
        label = rng.choice(["S1", None])
        status = rng.choices(STATUSES, weights=[6, 3, 1])[0]
        moment = BASE + timedelta(minutes=i * 61)     # 10 days, strictly increasing, unique
        runs.append(RunResult(
            run_id=f"run-{i:04d}", api_id=api_id, executed_at=moment, target_env=env,
            test_data_label=label, status=status,
            failed_rules=[] if status is RunStatus.PASS else [f"{api_id} rule"],
            http_status=200 if status is not RunStatus.ERROR else 503,
            duration_ms=100 + (i % 50), executed_by=rng.choice(["minseong", "jiwon", None]),
            day=moment.date().isoformat(),
        ))
    rng.shuffle(runs)         # rollup_delta must sort the batch itself
    return runs


def fold(runs: list[RunResult], seed: int):
    """Ingest ``runs`` in random-sized batches, accumulating exactly the way the Store does."""
    rng = random.Random(seed)
    ordered = sorted(runs, key=lambda r: (r.executed_at, r.run_id))
    state: dict[tuple, CellState] = {}
    rollups: dict[tuple, RollupRow] = {}
    marks: dict[str, ApiWatermark] = {}
    operators: dict[tuple[str, str], OperatorApiDelta] = {}
    start = 0
    batches = 0
    while start < len(ordered):
        size = rng.randint(1, 23)
        batch = ordered[start:start + size]
        start += size
        batches += 1
        touched = {c: state[c] for c in {cell_key(r) for r in batch} if c in state}
        rows, cells, watermarks, ops = rollup_delta(batch, touched)
        for row in rows:
            key = (row.day, *row.cell)
            existing = rollups.get(key)
            if existing is None:
                rollups[key] = row
                continue
            existing.count += row.count
            existing.passed += row.passed
            existing.fail += row.fail
            existing.error += row.error
            existing.transitions += row.transitions
            for key_map, delta_map in ((existing.failed_rule_counts, row.failed_rule_counts),
                                       (existing.http_status_counts, row.http_status_counts)):
                for key, counts in delta_map.items():
                    key_map[key] = key_map.get(key, KeyCounts()).add(counts)
            # §4's documented p95 approximation, same rule the Store applies when merging
            existing.p95_duration_ms = max(
                [v for v in (existing.p95_duration_ms, row.p95_duration_ms) if v is not None] or [None])
        for cell in cells:
            state[(cell.api_id, cell.target_env, cell.test_data_label)] = cell
        for mark in watermarks:
            marks[mark.api_id] = merge_watermark(marks.get(mark.api_id), mark)
        for delta in ops:
            key = (delta.operator_id, delta.api_id)
            previous = operators.get(key)
            if previous is None:
                operators[key] = delta
            else:
                previous.run_count += delta.run_count
                previous.last_executed_at = max(previous.last_executed_at, delta.last_executed_at)
    return state, rollups, marks, operators, batches


def test_incremental_rollups_equal_the_recomputed_aggregate():
    runs = make_runs(seed=20260906)
    state, rollups, marks, operators, batches = fold(runs, seed=7)
    assert batches >= 10          # the batching actually happened in many steps

    oracle = {g.key: g for g in aggregate(runs, APIS, "api_env_data", flaky_min_transitions=3)}
    per_cell: dict[tuple, dict] = {}
    for (_day, api_id, env, label), row in rollups.items():
        bucket = per_cell.setdefault((api_id, env, label),
                                     {"count": 0, "pass": 0, "fail": 0, "error": 0, "transitions": 0})
        bucket["count"] += row.count
        bucket["pass"] += row.passed
        bucket["fail"] += row.fail
        bucket["error"] += row.error
        bucket["transitions"] += row.transitions

    assert len(per_cell) == len(oracle)
    for (api_id, env, label), bucket in per_cell.items():
        group = oracle[f"{api_id}|{env}|{label or '-'}"]
        assert bucket["count"] == group.count
        assert bucket["pass"] == group.passed
        assert bucket["fail"] == group.fail
        assert bucket["error"] == group.error
        assert bucket["transitions"] == group.transitions
        cell = state[(api_id, env, label)]
        assert cell.transitions_total == group.transitions
        assert cell.status == group.latest_status

    # the two key maps (Task 8) fold to exactly the failed_rule / http_status groupings
    for group_by, attribute in (("failed_rule", "failed_rule_counts"), ("http_status", "http_status_counts")):
        folded: dict[str, KeyCounts] = {}
        for row in rollups.values():
            for key, counts in getattr(row, attribute).items():
                folded[key] = folded.get(key, KeyCounts()).add(counts)
        expected = {g.key: g for g in aggregate(runs, APIS, group_by, flaky_min_transitions=3)}
        assert set(folded) == set(expected), group_by
        for key, counts in folded.items():
            group = expected[key]
            assert (counts.count, counts.fail, counts.error) == (group.count, group.fail, group.error), key
            assert counts.count - counts.fail - counts.error == group.passed, key

    # totals, not just per cell
    assert sum(b["count"] for b in per_cell.values()) == len(runs)
    assert sum(b["pass"] for b in per_cell.values()) == sum(1 for r in runs if r.status is RunStatus.PASS)

    # current_state's latest run really is the chronologically last one of its cell
    for key, cell in state.items():
        newest = max((r for r in runs if cell_key(r) == key), key=lambda r: (r.executed_at, r.run_id))
        assert cell.run_id == newest.run_id and cell.executed_at == newest.executed_at

    # watermarks
    by_api = {g.key: g for g in aggregate(runs, APIS, "api", flaky_min_transitions=3)}
    assert set(marks) == set(by_api)
    for api_id, mark in marks.items():
        api_runs = sorted((r for r in runs if r.api_id == api_id), key=lambda r: (r.executed_at, r.run_id))
        passes = [r.executed_at for r in api_runs if r.status is RunStatus.PASS]
        non = [r.executed_at for r in api_runs if r.status is not RunStatus.PASS]
        assert mark.last_pass_at == (passes[-1] if passes else None)
        assert mark.first_non_pass_at == (non[0] if non else None)
        assert mark.last_non_pass_at == (non[-1] if non else None)
        assert mark.latest_status == api_runs[-1].status

    # operator_api: an unattributed run contributes nothing
    for (operator_id, api_id), delta in operators.items():
        mine = [r for r in runs if r.executed_by == operator_id and r.api_id == api_id]
        assert delta.run_count == len(mine)
        assert delta.last_executed_at == max(r.executed_at for r in mine)
    assert all(operator_id for operator_id, _ in operators)
    attributed = sum(d.run_count for d in operators.values())
    assert attributed == sum(1 for r in runs if r.executed_by)


def test_batch_slicing_does_not_change_the_outcome():
    runs = make_runs(seed=11, count=120)
    first = fold(runs, seed=1)
    second = fold(runs, seed=99)
    assert first[0] == second[0]                                     # current_state
    # p95 is the one field §4 lets drift with the batching (max of per-batch p95s, never an
    # exact percentile over the union) — everything else must be slicing-invariant.
    def _rows(folded):
        return {k: v.model_dump(exclude={"p95_duration_ms"}) for k, v in folded[1].items()}
    assert _rows(first) == _rows(second)
    assert first[2] == second[2]                                     # watermarks
    assert first[3] == second[3]                                     # operator_api


def test_transitions_are_seeded_by_the_previous_state_not_by_the_batch_edge():
    cell = ("api-001", "dev", None)
    first = RunResult(run_id="r1", api_id="api-001", executed_at=BASE, target_env="dev",
                      status=RunStatus.PASS, day="2026-08-20")
    second = RunResult(run_id="r2", api_id="api-001", executed_at=BASE + timedelta(hours=1),
                       target_env="dev", status=RunStatus.FAIL, day="2026-08-20")

    rows, cells, _marks, _ops = rollup_delta([first], {})
    assert rows[0].transitions == 0 and cells[0].transitions_total == 0   # a cell's first run never flips

    rows2, cells2, _m, _o = rollup_delta([second], {cell: cells[0]})
    assert rows2[0].transitions == 1                       # pass -> fail across the batch boundary
    assert cells2[0].transitions_total == 1                # and it accumulates onto the prior total

    # non-pass -> non-pass is not a transition (fail and error are the same side of the line)
    third = second.model_copy(update={"run_id": "r3", "status": RunStatus.ERROR,
                                      "executed_at": BASE + timedelta(hours=2)})
    rows3, cells3, _m, _o = rollup_delta([third], {cell: cells2[0]})
    assert rows3[0].transitions == 0 and cells3[0].transitions_total == 1


def test_failed_rule_counts_mirror_the_failed_rule_grouping():
    runs = [
        RunResult(run_id="r1", api_id="api-001", executed_at=BASE, target_env="dev",
                  status=RunStatus.FAIL, failed_rules=["amount >= 0", "amount >= 0"], day="2026-08-20"),
        RunResult(run_id="r2", api_id="api-001", executed_at=BASE + timedelta(minutes=1),
                  target_env="dev", status=RunStatus.FAIL, failed_rules=["amount >= 0"], day="2026-08-20"),
        RunResult(run_id="r3", api_id="api-001", executed_at=BASE + timedelta(minutes=2),
                  target_env="dev", status=RunStatus.ERROR, http_status=503, day="2026-08-20"),
        RunResult(run_id="r4", api_id="api-001", executed_at=BASE + timedelta(minutes=3),
                  target_env="dev", status=RunStatus.PASS, day="2026-08-20"),
    ]
    rows, _cells, _marks, _ops = rollup_delta(runs, {})
    assert len(rows) == 1
    # a rule named twice in one run counts once (the same dedup aggregate() does), a pass run
    # contributes nothing, and a rule-less non-pass falls back to the synthetic HTTP bucket
    assert rows[0].failed_rule_counts == {
        "amount >= 0": KeyCounts(count=2, fail=2, error=0),
        "(error) HTTP 503": KeyCounts(count=1, fail=0, error=1),
    }
    assert (rows[0].count, rows[0].passed, rows[0].fail, rows[0].error) == (4, 1, 2, 1)
    # the second map (Task 8) buckets by HTTP status over EVERY run, pass included, and carries
    # the same fail/error split -- group_by="http_status" is answered from it without a run scan
    assert rows[0].http_status_counts == {
        "(none)": KeyCounts(count=3, fail=2, error=0),
        "503": KeyCounts(count=1, fail=0, error=1),
    }


def test_p95_and_operator_deltas_come_from_the_batch_only():
    runs = [
        RunResult(run_id=f"r{i}", api_id="api-001", executed_at=BASE + timedelta(minutes=i),
                  target_env="dev", status=RunStatus.PASS, duration_ms=i * 10,
                  executed_by=None if i == 0 else "minseong", day="2026-08-20")
        for i in range(1, 21)
    ]
    rows, _cells, _marks, ops = rollup_delta(runs, {})
    assert rows[0].p95_duration_ms == 190          # ceil(0.95 * 20) = 19th of 20 -> 190ms
    assert [(o.operator_id, o.run_count) for o in ops] == [("minseong", 20)]


def test_unattributed_runs_produce_no_operator_rows():
    runs = [RunResult(run_id="r1", api_id="api-001", executed_at=BASE, target_env="dev",
                      status=RunStatus.PASS, executed_by=None, day="2026-08-20")]
    _rows, _cells, _marks, ops = rollup_delta(runs, {})
    assert ops == []


def test_merge_watermark_keeps_the_extremes_and_the_later_status():
    early = ApiWatermark(api_id="api-001", last_pass_at=BASE, first_non_pass_at=None,
                         last_non_pass_at=None, latest_status=RunStatus.PASS)
    later = ApiWatermark(api_id="api-001", last_pass_at=None,
                         first_non_pass_at=BASE + timedelta(hours=1),
                         last_non_pass_at=BASE + timedelta(hours=2), latest_status=RunStatus.FAIL)
    merged = merge_watermark(early, later)
    assert merged.last_pass_at == BASE
    assert merged.first_non_pass_at == BASE + timedelta(hours=1)
    assert merged.last_non_pass_at == BASE + timedelta(hours=2)
    assert merged.latest_status is RunStatus.FAIL          # the later execution wins

    # an older batch arriving late must not rewrite latest_status
    stale = ApiWatermark(api_id="api-001", last_pass_at=BASE - timedelta(days=1),
                         first_non_pass_at=None, last_non_pass_at=None, latest_status=RunStatus.PASS)
    assert merge_watermark(merged, stale).latest_status is RunStatus.FAIL
    assert merge_watermark(None, later) == later
