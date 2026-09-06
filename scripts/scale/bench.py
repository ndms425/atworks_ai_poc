"""SLO bench for the scale harness (spec 2026-09-06 §12, Task 7).

Opens a dataset directory written by ``generate.py`` through the ordinary Mock -- no constructor
change, no special mode::

    MockAtworks(config, dataset_dir, store=Store(dataset_dir / "scale.sqlite"))

and times every path §12 puts an SLO on, printing one row per path with the measured milliseconds,
the ``config.slo_*`` limit, and PASS/FAIL. A path that cannot be measured on this dataset (no API
declares the probe's param, say) prints ``n/a`` and does not count toward the exit status; exit is
1 iff a **measured** row breached its limit, and the full table is always printed first.

Timing rule: best of 3 timed calls after 1 warm-up, except ``insights.build`` and
``briefing.generate``, which run **once** (they write files and are seconds-scale; a best-of-3
would only measure a warm page cache).

Measurement order matters, and the last two rows both MUTATE the dataset: the retention probe moves
the whole run set into the cold partition and drops every body, and the SSE-latency probe then
executes a real 400-cell job that writes 400 fresh runs. Both run after every read row, so those
see the dataset exactly as generated. Run the bench on a throwaway copy -- ``test_scale`` does.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AggregateQuery,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    RuleDraft,
    RunsQuery,
    TestDataSet,
)
from atworks_agent.aggregation import GROUP_BY
from atworks_host.briefing import Briefings
from atworks_host.insights import InsightPanels
from atworks_host.mock_backend import MockAtworks
from atworks_host.retention import Retention
from atworks_host.store import Store, _parse_iso

DEFAULT_DB = Path(__file__).resolve().parent / "out"


@dataclass
class Row:
    """One SLO line. ``ms is None`` means the path does not exist yet (Task 9) -- printed as
    ``n/a`` and excluded from the exit status. ``derived`` rows restate another row's number
    (the aggregate max) and are likewise excluded so one breach is never counted twice."""
    name: str
    limit_ms: int
    ms: float | None = None
    note: str = ""
    derived: bool = False
    samples: list[float] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.ms is None:
            return "n/a"
        return "PASS" if self.ms <= self.limit_ms else "FAIL"

    @property
    def counts(self) -> bool:
        return self.ms is not None and not self.derived


async def _best_of(fn, *, repeats: int = 3, warmup: int = 1) -> tuple[float, list[float]]:
    for _ in range(warmup):
        await fn()
    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - start) * 1000)
    return min(samples), samples


def _pick_operator(store: Store) -> str:
    row = store.conn().execute(
        "SELECT operator_id, SUM(run_count) AS n FROM operator_api GROUP BY operator_id "
        "ORDER BY n DESC LIMIT 1"
    ).fetchone()
    return row["operator_id"] if row is not None else "op-0000"


def _busiest_apis(store: Store, since: datetime, limit: int = 400) -> list[str]:
    rows = store.conn().execute(
        "SELECT api_id, COUNT(*) AS n FROM runs WHERE executed_at >= ? GROUP BY api_id "
        "ORDER BY n DESC LIMIT ?",
        (since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z", limit),
    ).fetchall()
    return [r["api_id"] for r in rows]


async def run_bench(dataset: Path, *, config: AtworksAgentConfig | None = None,
                    work_dir: Path | None = None, quiet: bool = False) -> list[Row]:
    config = config or AtworksAgentConfig(model="scale")
    meta_path = dataset / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    now = datetime.fromisoformat(meta["now"]) if meta.get("now") else datetime.now(UTC)

    setup_started = time.perf_counter()
    store = Store(dataset / "scale.sqlite")
    backend = MockAtworks(config, dataset, store=store)
    operator = _pick_operator(store)
    session = AtworksSessionContext(session_id="bench", project_id="scale", operator=operator,
                                    role="developer", now=now)
    setup_ms = (time.perf_counter() - setup_started) * 1000
    if not quiet:
        print(f"dataset {dataset}  runs={store.run_count():,}  apis={len(backend.apis):,}  "
              f"operator={operator}  open+load {setup_ms:,.0f}ms")

    window = now - timedelta(days=config.max_aggregate_window_days)
    rows: list[Row] = []

    # -- get_context -----------------------------------------------------------------------
    ms, samples = await _best_of(lambda: backend.get_context(session))
    rows.append(Row("get_context", config.slo_get_context_ms, ms, samples=samples))

    # -- list_runs: first page, then one cursor-follow page ---------------------------------
    first_page = await backend.list_runs(session, RunsQuery(limit=50))
    ms, samples = await _best_of(lambda: backend.list_runs(session, RunsQuery(limit=50)))
    rows.append(Row("list_runs page 1 (limit 50)", config.slo_list_runs_ms, ms,
                    note=f"total={first_page.total:,}", samples=samples))
    cursor = first_page.next_cursor
    if cursor is None:
        rows.append(Row("list_runs page 2 (cursor)", config.slo_list_runs_ms, note="no second page"))
    else:
        ms, samples = await _best_of(lambda: backend.list_runs(session, RunsQuery(limit=50, cursor=cursor)))
        rows.append(Row("list_runs page 2 (cursor)", config.slo_list_runs_ms, ms, samples=samples))

    # -- count_runs. §12 gives no separate limit; get_context IS two count_runs plus the
    #    operator scope, so its budget is the honest ceiling for one of them.
    ms, samples = await _best_of(lambda: backend.count_runs(session, since=window, status="fail"))
    rows.append(Row("count_runs (fail, 30d)", config.slo_get_context_ms, ms,
                    note="budget borrowed from get_context", samples=samples))

    # -- aggregate_runs × 5 group_bys over max_aggregate_window_days -------------------------
    aggregate_ms: list[float] = []
    for group_by in GROUP_BY:
        query = AggregateQuery(since=window, group_by=group_by, limit=50)
        ms, samples = await _best_of(lambda q=query: backend.aggregate_runs(session, q))
        aggregate_ms.append(ms)
        rows.append(Row(f"aggregate_runs group_by={group_by}", config.slo_aggregate_ms, ms, samples=samples))
    rows.append(Row("aggregate_runs (max of 5)", config.slo_aggregate_ms, max(aggregate_ms),
                    note="restates the worst row above", derived=True))

    # -- simulate_rule 30d on a numeric-compare draft over a widely shared param -------------
    amount_apis = {a.api_id for a in backend.apis.values() if "amount" in a.params}
    target = next((api_id for api_id in _busiest_apis(store, window) if api_id in amount_apis), None)
    if target is None:
        rows.append(Row("simulate_rule (30d, amount)", config.slo_simulate_rule_ms,
                        note="no api declares 'amount'"))
    else:
        draft = RuleDraft(api_id=target, param="amount", kind="compare", op=">=", value="0")
        ms, samples = await _best_of(lambda: backend.simulate_rule(session, draft, 30))
        rows.append(Row("simulate_rule (30d, amount)", config.slo_simulate_rule_ms, ms,
                        note=f"api={target}", samples=samples))

    # -- InsightPanels.build, deterministic (a narrator that never calls a model) ------------
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        async def _no_narration(candidates, role, *, notes=None):
            del candidates, role, notes
            return []

        panels = InsightPanels(Path(tmp) / "insights", config.model_copy(
            update={"enable_insight_narration": False}), _no_narration)
        start = time.perf_counter()
        panel = await panels.build(backend, session, now)
        rows.append(Row("insights.build (deterministic)", config.slo_insights_ms,
                        (time.perf_counter() - start) * 1000,
                        note=f"{len(panel.items)} candidates, scope={panel.scope_size}"))

        briefings = Briefings(Path(tmp) / "briefings", config)
        start = time.perf_counter()
        await briefings.generate(backend, session, now)
        rows.append(Row("briefing.generate", config.slo_briefing_ms, (time.perf_counter() - start) * 1000))

    # -- retention day job (Task 9) -------------------------------------------------------
    #    `now` is pushed past the newest run by the whole hot window, so the ENTIRE dataset ages
    #    out in this one pass -- an upper bound on any real day's work, not a typical one.
    rows.append(_retention_probe(store, config, work_dir))

    # -- SSE latency while a 400-cell job executes. Writes 400 runs -- keep it last. Its runs
    #    are stamped "now", so the retention pass above (which archived everything older) left
    #    the write path exactly as a production one: an empty-ish hot partition it appends to.
    rows.append(await _sse_probe(backend, session, config, amount_apis))
    return rows


def _retention_probe(store: Store, config: AtworksAgentConfig, work_dir: Path | None) -> Row:
    newest = store.conn().execute("SELECT MAX(executed_at) FROM runs").fetchone()[0]
    if newest is None:
        return Row("retention day job", config.slo_retention_ms, note="empty dataset")
    when = _parse_iso(newest) + timedelta(days=config.retention_hot_days + 1)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        retention = Retention(store, config, Path(tmp) / "insights")
        start = time.perf_counter()
        counts = retention.run(when)
        ms = (time.perf_counter() - start) * 1000
    return Row("retention day job", config.slo_retention_ms, ms,
               note=f"archived {counts['runs_archived']:,}, bodies {counts['bodies_deleted']:,} "
                    f"(whole set ages out)")


async def _sse_probe(backend: MockAtworks, session: AtworksSessionContext,
                     config: AtworksAgentConfig, amount_apis: set[str]) -> Row:
    """The event-loop non-blocking proof: while ``execute_job_once`` grinds a full 400-cell
    matrix as a task, a probe coroutine repeatedly does ``await asyncio.sleep(0)`` followed by
    one ``get_context`` -- exactly what an SSE turn does between chunks. The reported number is
    the WORST such round trip; if execution hogged the loop, this is where it shows."""
    api_ids = sorted(amount_apis)[:config.max_apis_per_job]
    if len(api_ids) < config.max_apis_per_job:
        api_ids = (api_ids + sorted(backend.apis)[:config.max_apis_per_job])[:config.max_apis_per_job]
    draft = JobDraft(
        kind=JobKind.RUN_NOW, summary="scale bench 400-cell matrix", api_ids=api_ids,
        target_envs=["dev", "stg"],
        test_data=[TestDataSet(label="basic", values={"amount": "10"}),
                   TestDataSet(label="edge", values={"amount": "-1"})],
    )
    try:
        job = await backend.stage_job(session, draft, ActorKind.OPERATOR)
        await backend.apply_job(session, job.job_id)
    except Exception as exc:                                   # noqa: BLE001 -- reported, not raised
        return Row("chat SSE latency during 400-cell execute", config.slo_sse_latency_ms,
                   note=f"could not stage the job: {exc}")

    task = asyncio.create_task(backend.execute_job_once(session, job.job_id))
    gaps: list[float] = []
    while not task.done():
        start = time.perf_counter()
        await asyncio.sleep(0)
        await backend.get_context(session)
        gaps.append((time.perf_counter() - start) * 1000)
    produced = await task
    if not gaps:
        return Row("chat SSE latency during 400-cell execute", config.slo_sse_latency_ms,
                   note="execution finished before the first probe")
    return Row("chat SSE latency during 400-cell execute", config.slo_sse_latency_ms, max(gaps),
               note=f"{len(produced)} runs, {len(gaps)} probes, median "
                    f"{sorted(gaps)[len(gaps) // 2]:.1f}ms")


def print_table(rows: list[Row]) -> None:
    width = max(len(r.name) for r in rows) + 2
    print()
    print(f"{'path':<{width}}{'measured':>12}{'limit':>10}  {'verdict':<8}note")
    print("-" * (width + 32 + 40))
    for row in rows:
        measured = "n/a" if row.ms is None else f"{row.ms:,.1f}ms"
        print(f"{row.name:<{width}}{measured:>12}{row.limit_ms:>8}ms  {row.verdict:<8}{row.note}")
    print("-" * (width + 32 + 40))
    failed = [r for r in rows if r.counts and r.verdict == "FAIL"]
    measured = [r for r in rows if r.counts]
    print(f"{len(measured) - len(failed)}/{len(measured)} measured paths within SLO"
          + (f"; FAIL: {', '.join(r.name for r in failed)}" if failed else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="dataset directory written by generate.py (or the scale.sqlite inside it)")
    parser.add_argument("--json", type=Path, default=None, help="also dump the rows as JSON here")
    args = parser.parse_args(argv)
    dataset = Path(args.db)
    if dataset.is_file():
        dataset = dataset.parent
    rows = asyncio.run(run_bench(dataset))
    print_table(rows)
    if args.json:
        args.json.write_text(json.dumps([asdict(r) | {"verdict": r.verdict} for r in rows],
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(r.counts and r.verdict == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
