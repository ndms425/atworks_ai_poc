"""Opt-in scale harness test (spec 2026-09-06 §12, Task 7). Skipped by default -- ``pytest.ini``
carries ``-m "not scale"`` in addopts, so ``pytest -q`` never pays for it. Run it with::

    .venv/Scripts/python.exe -m pytest -m scale host/tests/test_scale.py -s

By default it generates a REDUCED dataset (5,000 APIs × 30 days × 2,000 runs/day = 60,000 runs,
50 operators, no peak day) into a tmp dir; set ``ATWORKS_SCALE_FULL=1`` for the spec's full
numbers (50,000 APIs, 180 days, 10,000/day with a 50,000-run peak day, 500 operators ≈ 2M runs),
which takes minutes, not seconds, and is CI-optional.

What this asserts today:

1. **Plumbing** -- the dataset really went through ``Store.ingest``: table sizes match what was
   asked for, ``rollup_day`` sums back to the run count, ``current_state`` has exactly one row per
   distinct cell, every ``api_watermark`` row names a real API, and ``bodies`` covers exactly the
   runs inside ``retention_body_days``.
2. **Determinism** -- two generations with the same seed and the same ``now`` agree on the per-day
   run counts and on the first/last run id.
3. **The bench runs end-to-end** and its table is printed into the captured output.

Which SLOs it asserts: whatever ``SLO_ASSERT`` names True. Tasks 8 and 9 rewired the host reads and
the write path; each row turned green there, and flipping ``SLO_ASSERT[path] = True`` is the
one-line change that locks it in. The baseline (Task 7 report) was that ``aggregate_runs`` breached
at every size, and ``get_context``, ``insights.build`` and the SSE latency probe breached from a
few hundred thousand runs upward.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atworks_agent import AtworksAgentConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "scale"))

import bench as bench_mod  # noqa: E402
import generate as gen  # noqa: E402

pytestmark = pytest.mark.scale

FULL = os.environ.get("ATWORKS_SCALE_FULL") == "1"
# One anchor shared by every generation in this module, so the determinism twin is comparable and
# so the windows the bench measures (30 days, 90 days) actually contain data.
NOW = datetime.now(UTC).replace(microsecond=0)

SIZES = (
    {"apis": 50_000, "days": 180, "per_day": 10_000, "operators": 500, "peak_days": {120: 50_000}}
    if FULL else
    {"apis": 5_000, "days": 30, "per_day": 2_000, "operators": 50, "peak_days": {}}
)

# Tasks 8/9 flip these to True one at a time as each path is rewired and turns green. Every
# measured bench row must appear here (test_bench_rows_are_all_declared enforces that), so a new
# or renamed path can never slip past the switch unnoticed.
#
# Task 8 turned the aggregate/context/insight/briefing reads on: they read rollup_day,
# api_watermark, current_state and operator_api instead of scanning (or sampling) runs, and every
# one of them is green on the reduced set AND on the 450k-run mid set.
#
# Task 9 turned on the last two: `retention day job` (the whole set ages out in one pass -- an
# upper bound on any real day: reduced 986ms, mid 8.0s, limit 30s) and `chat SSE latency during
# 400-cell execute`, which the chunked WRITE fixed -- the unbroken 400-run `store.ingest` measured
# 162ms on the mid set in Task 7 and now measures 18ms (reduced 12ms, limit 100ms).
#
# The final fix wave flipped the last four (I10). The list/count/simulate reads were never rewired
# by any of these tasks -- they were inside their budget from the start -- but "measured green and
# never asserted" is not a guarantee: a row nobody asserts is a row that can rot silently. Every
# row the bench measures is now asserted, so there is no unguarded path left in the table.
SLO_ASSERT: dict[str, bool] = {
    "get_context": True,
    "list_runs page 1 (limit 50)": True,
    "list_runs page 2 (cursor)": True,
    "count_runs (fail, 30d)": True,
    "aggregate_runs group_by=api": True,
    "aggregate_runs group_by=env": True,
    "aggregate_runs group_by=http_status": True,
    "aggregate_runs group_by=failed_rule": True,
    "aggregate_runs group_by=api_env_data": True,
    "aggregate_runs (max of 5)": True,
    # Task 10 (self-growth §12): one row per arm `select_source` can pick. Two are green and
    # asserted; three are NOT, and the limit is not moved to make them so.
    #
    # What the bench found is that the ARM is not what costs: measured on the 60k-run demo set,
    # aggregation alone is 0.6ms (key arm), 28ms (rollup_day), 53ms (runs arm) -- every one of
    # them comfortably inside 300ms. It is `include_samples` (the default, and what the model
    # actually sends) that adds 30-464ms: `_fill_query_samples` runs TWO statements per returned
    # row, and on the key axis each of those is a `json_each` EXISTS over the whole window with
    # no index to drive off -- 40 window scans for one card. The runs arm pays the same toll for
    # a different reason: `executed_by IS ?` and `api_id IS ?` are both usable indexes and,
    # without table statistics, SQLite picks the far less selective one.
    #
    # Named follow-ups, in the order they should be tried: (1) a bounded `PRAGMA analysis_limit`
    # + `ANALYZE` step in the daily retention job -- measured on a copy of the demo set it takes
    # the runs-arm row from 790ms to 166ms with no schema change; (2) fill the evidence samples
    # for the whole page in ONE pair of statements keyed on the returned group keys, instead of
    # one pair per row. Documented with its per-read profile in the Task 10 report, never by
    # widening `slo_query_ms`.
    "query_runs path_segment_2 (non_pass)": True,              # 164 / 177ms over two runs
    "query_runs method x target_env": False,                   # 264 / 294ms -- inside, but only just
    "query_runs failed_rule (key arm)": False,                 # 295 / 566ms
    "query_runs executed_by x api (runs arm)": False,          # 630 / 734ms
    "query_runs compare_previous_window (rollup arm)": True,   # 27 / 40ms
    "simulate_rule (30d, amount)": True,
    "insights.build (deterministic)": True,
    "briefing.generate": True,
    "retention (one day partition)": True,
    "chat SSE latency during 400-cell execute": True,
}


def _generate(out: Path, seed: int = 42) -> dict:
    return gen.generate(out=out, now=NOW, seed=seed, quiet=True, progress_every=0, **SIZES)


def _conn(dataset: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(dataset / "scale.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("scale") / "primary"
    summary = _generate(out)
    print(f"\n[scale] generated {summary['ingested']:,} runs in {summary['elapsed_s']}s -> {out}")
    return out


@pytest.fixture(scope="module")
def twin(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("scale") / "twin"
    _generate(out)
    return out


def test_table_sizes_match_the_requested_dataset(dataset: Path) -> None:
    conn = _conn(dataset)
    expected_runs = SIZES["days"] * SIZES["per_day"] + sum(
        count - SIZES["per_day"] for count in SIZES["peak_days"].values()
    )
    assert conn.execute("SELECT COUNT(*) FROM apis").fetchone()[0] == SIZES["apis"]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == expected_runs
    operators = json.loads((dataset / "operators.json").read_text(encoding="utf-8"))
    assert len(operators) == SIZES["operators"]
    assert {row["role"] for row in operators} == {"developer", "qa", "pm"}
    # the fixtures trio makes the directory openable by MockAtworks with no constructor change
    assert json.loads((dataset / "runs.json").read_text(encoding="utf-8")) == []
    assert len(json.loads((dataset / "apis.json").read_text(encoding="utf-8"))) == SIZES["apis"]


def test_rollups_and_cells_were_materialized_at_ingest(dataset: Path) -> None:
    conn = _conn(dataset)
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    rollup = conn.execute(
        'SELECT SUM("count") AS c, SUM("pass") AS p, SUM(fail) AS f, SUM(error) AS e FROM rollup_day'
    ).fetchone()
    assert rollup["c"] == runs
    assert rollup["p"] + rollup["f"] + rollup["e"] == runs
    # rollup_day is a fold, not a copy: the generator's API-major days put a cell in a local date
    # more than once, so there must be strictly fewer rollup rows than runs.
    assert conn.execute("SELECT COUNT(*) FROM rollup_day").fetchone()[0] < runs

    distinct_cells = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT api_id, target_env, test_data_label FROM runs)"
    ).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM current_state").fetchone()[0] == distinct_cells

    orphans = conn.execute(
        "SELECT COUNT(*) FROM api_watermark WHERE api_id NOT IN (SELECT api_id FROM apis)"
    ).fetchone()[0]
    assert orphans == 0
    assert conn.execute("SELECT COUNT(*) FROM api_watermark").fetchone()[0] == conn.execute(
        "SELECT COUNT(DISTINCT api_id) FROM runs").fetchone()[0]

    orphan_ops = conn.execute(
        "SELECT COUNT(*) FROM operator_api WHERE api_id NOT IN (SELECT api_id FROM apis)"
    ).fetchone()[0]
    assert orphan_ops == 0


def test_bodies_cover_exactly_the_retention_body_window(dataset: Path) -> None:
    cutoff = (NOW - timedelta(days=AtworksAgentConfig(model="m").retention_body_days))
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    conn = _conn(dataset)
    too_old = conn.execute(
        "SELECT COUNT(*) FROM bodies JOIN runs ON runs.run_id = bodies.run_id WHERE runs.executed_at < ?",
        (cutoff_iso,),
    ).fetchone()[0]
    assert too_old == 0, "a body was stored for a run outside retention_body_days"
    missing = conn.execute(
        "SELECT COUNT(*) FROM runs LEFT JOIN bodies ON bodies.run_id = runs.run_id "
        "WHERE runs.executed_at >= ? AND bodies.run_id IS NULL",
        (cutoff_iso,),
    ).fetchone()[0]
    assert missing == 0, "an in-window run has no body"


def test_masking_ran_at_capture(dataset: Path) -> None:
    """The ~1% PII-shaped bodies must have been scrubbed on the way into `bodies` -- the point of
    routing the generator through `Store.ingest(mask=...)` rather than raw INSERTs."""
    conn = _conn(dataset)
    raw = conn.execute(
        "SELECT COUNT(*) FROM bodies WHERE body LIKE '%example.com%' OR body LIKE '%900101-1234567%' "
        "OR body LIKE '%4111 1111%' OR body LIKE '%010-1234-5678%' OR body LIKE '%110-234-567890%'"
    ).fetchone()[0]
    assert raw == 0
    assert conn.execute("SELECT COUNT(*) FROM bodies WHERE body LIKE '%***%'").fetchone()[0] > 0


def test_injected_patterns_are_present(dataset: Path) -> None:
    meta = json.loads((dataset / "meta.json").read_text(encoding="utf-8"))
    conn = _conn(dataset)
    # These are the GENERATOR's counters, not detection counts, and that is deliberate for
    # `regression_apis`: on the reduced set 5,000 APIs share 30 days x 2,000 runs at 8 runs per
    # api-day, i.e. ~1.5 ACTIVE DAYS PER API, so most injected regressions never get both a pass
    # day before their flip instant and a fail day after it -- the `regression_suspect` predicate
    # (last_pass_at < updated_at <= first_non_pass_at) simply has nothing to fire on, and a
    # detection-count assertion here would be flaky by construction. The full set (180 days,
    # ~4-5 active days per API) does surface them; the detection rule itself is proven exactly by
    # the aggregation/insights unit tests, not by this dataset.
    assert meta["regression_apis"] > 0 and meta["flaky_cells"] > 0
    # flaky cells alternate, so they cross the flaky_v1 threshold in the materialized counter
    threshold = AtworksAgentConfig(model="m").flaky_min_transitions
    flaky = conn.execute(
        "SELECT COUNT(*) FROM current_state WHERE transitions_total >= ?", (threshold,)
    ).fetchone()[0]
    assert flaky > 0
    # the group distribution is skewed, not uniform
    groups = [r[0] for r in conn.execute('SELECT COUNT(*) FROM apis GROUP BY "group" ORDER BY 1 DESC')]
    assert len(groups) == len(gen.GROUP_WEIGHTS) and groups[0] > 2 * groups[-1]


def test_generation_is_deterministic(dataset: Path, twin: Path) -> None:
    left = json.loads((dataset / "meta.json").read_text(encoding="utf-8"))
    right = json.loads((twin / "meta.json").read_text(encoding="utf-8"))
    assert left["runs_per_day"] == right["runs_per_day"]
    assert (left["first_run_id"], left["last_run_id"]) == (right["first_run_id"], right["last_run_id"])
    assert left["status_counts"] == right["status_counts"]
    assert left["rows"] == right["rows"]

    def per_day(path: Path) -> list[tuple[str, int, str, str]]:
        return [
            (r["day"], r["n"], r["lo"], r["hi"]) for r in _conn(path).execute(
                "SELECT day, COUNT(*) AS n, MIN(run_id) AS lo, MAX(run_id) AS hi FROM runs "
                "GROUP BY day ORDER BY day"
            )
        ]

    assert per_day(dataset) == per_day(twin)


@pytest.fixture(scope="module")
def bench_rows(dataset: Path, tmp_path_factory) -> list:
    # The bench's SSE probe executes a real 400-cell job, which WRITES 400 runs -- bench a copy so
    # the plumbing assertions above keep describing the dataset as generated, whatever order
    # pytest runs these in. One bench run serves every assertion below.
    work = tmp_path_factory.mktemp("bench")
    copy = work / "dataset"
    shutil.copytree(dataset, copy)
    return asyncio.run(bench_mod.run_bench(copy, work_dir=work, quiet=True))


def test_bench_runs_end_to_end_and_records_its_table(bench_rows: list) -> None:
    rows = bench_rows
    bench_mod.print_table(rows)

    names = [row.name for row in rows]
    assert len(names) == len(set(names))
    assert sum(1 for n in names if n.startswith("aggregate_runs group_by=")) == 5
    retention = next(r for r in rows if r.name == "retention (one day partition)")
    # ONE day partition, on a copy (final review): ageing the WHOLE set out measured an upper
    # bound nobody runs, and it destroyed the dataset it measured.
    assert retention.ms is not None and "on a copy" in retention.note
    measured = [r for r in rows if r.counts]
    assert len(measured) >= 13 and all(r.ms is not None and r.ms >= 0 for r in measured)

    breaches = [f"{r.name} {r.ms:,.1f}ms > {r.limit_ms}ms" for r in rows if r.counts and r.verdict == "FAIL"]
    print(f"[scale] baseline breaches ({len(breaches)}): {breaches}")
    for row in rows:
        if row.ms is not None and SLO_ASSERT.get(row.name):
            assert row.verdict == "PASS", f"{row.name}: {row.ms:,.1f}ms > {row.limit_ms}ms"


def test_bench_rows_are_all_declared(bench_rows: list) -> None:
    """A path the bench measures but ``SLO_ASSERT`` never names would be permanently unassertable
    without anyone noticing -- so the switch table must cover every row the bench prints."""
    undeclared = sorted({row.name for row in bench_rows} - set(SLO_ASSERT))
    assert not undeclared, f"add these bench paths to SLO_ASSERT: {undeclared}"
