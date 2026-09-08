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

Measurement order matters, and the last two rows MUTATE. ``--no-mutate`` (the DEFAULT) copies
``scale.sqlite`` to a temp file for the retention probe, because that probe moves runs into the
cold partition and deleting a 2M-run dataset's hot partition to measure one row is not a trade
worth making twice -- the Task 11 full-set bench did exactly that and left the dataset unusable.
Pass ``--mutate`` to run it in place. The SSE-latency probe still appends its own 400 runs to the
dataset (it executes a real 400-cell job); that is an append, not a destruction, and it now runs
against a FULL hot partition rather than the emptied one the in-place retention probe left behind.

The retention row measures **one day partition** ageing out (spec §6/§12 "하루치"), not the whole
set: ``now`` is pinned to the oldest day partition's local midnight plus ``retention_hot_days + 1``,
so exactly that one partition crosses the cutoff. Ageing the whole dataset out in one pass was an
upper bound on a real day's work, and at 2M runs it read as 128 seconds against a 30-second limit
for a job that really does about a second.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from atworks_agent import (
    ActorKind,
    AggregateQuery,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    QueryFilters,
    QuerySpec,
    RuleDraft,
    RunsQuery,
    TestDataSet,
)
from atworks_agent.aggregation import GROUP_BY
from atworks_host.briefing import Briefings
from atworks_host.insights import InsightPanels
from atworks_host.mock_backend import MockAtworks
from atworks_host.query_sql import select_source
from atworks_host.retention import Retention
from atworks_host.store import Store

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
                    work_dir: Path | None = None, quiet: bool = False,
                    mutate: bool = False) -> list[Row]:
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

    # -- query_runs: one row per SOURCE the compiler can pick (self-growth spec §4/§12) ------
    #    Source selection is the design, so a bench that only ever hit `rollup_day` would prove
    #    nothing about the other three arms. Each spec below is named for the arm it lands on,
    #    asserted by `select_source` rather than assumed, and every one of them is a shape the
    #    Query catalogue actually invites the model to ask for.
    for name, spec in _query_specs(config):
        source = select_source(spec)
        ms, samples = await _best_of(lambda s=spec: backend.query_runs(session, s))
        rows.append(Row(f"query_runs {name}", config.slo_query_ms, ms,
                        note=f"source={source}", samples=samples))

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

    # -- retention: ONE day partition ages out (Task 9 + final review) ----------------------
    rows.append(await _retention_probe(dataset, store, config, work_dir, mutate))

    # -- SSE latency while a 400-cell job executes. Writes 400 runs -- keep it last. With the
    #    retention probe on a copy, this now runs against the FULL hot partition, which is the
    #    honest shape: a production write appends to a store that already holds its history.
    rows.append(await _sse_probe(backend, session, config, amount_apis))
    return rows


def _query_specs(config: AtworksAgentConfig) -> list[tuple[str, QuerySpec]]:
    """The five `query_runs` shapes the bench times, one per compiled arm plus the compare join.

    The window is `max_aggregate_window_days` throughout -- the same 30 days every other
    aggregate row on this table uses, so the numbers are comparable -- and it is expressed as
    `window_days` rather than since/until so the spec is what the model would actually emit."""
    window = config.max_aggregate_window_days
    return [
        # rollup_day, `apis` join: the endpoint-family question the smoke asks in Korean.
        ("path_segment_2 (non_pass)", QuerySpec(
            filters=QueryFilters(status="non_pass", window_days=window),
            dimensions=["path_segment_2"], measures=["runs", "non_pass"], order_by="non_pass")),
        # rollup_day, two dimensions, one of them off the catalogue and one off the cell.
        ("method x target_env", QuerySpec(
            filters=QueryFilters(window_days=window), dimensions=["method", "target_env"],
            measures=["runs", "non_pass", "fail_rate"], order_by="runs")),
        # rollup_key_day: the transposed key axis, unscoped, which is the arm that exists so a
        # key group does not `json_each` every cell row in the window.
        ("failed_rule (key arm)", QuerySpec(
            filters=QueryFilters(window_days=window), dimensions=["failed_rule"],
            measures=["runs", "non_pass"], order_by="non_pass")),
        # runs: an `executed_by` dimension PAIRED with `api` has no rollup that carries both, so
        # this is the raw-scan arm -- the slowest thing the catalogue can ask for.
        ("executed_by x api (runs arm)", QuerySpec(
            filters=QueryFilters(status="non_pass", window_days=window),
            dimensions=["executed_by", "api"], measures=["runs", "non_pass"], order_by="non_pass")),
        # rollup_day + the previous-window join: two windows, two top lists, one row set.
        ("compare_previous_window (rollup arm)", QuerySpec(
            filters=QueryFilters(window_days=window), dimensions=["api"],
            measures=["runs", "non_pass", "apis"], order_by="non_pass",
            compare_previous_window=True)),
    ]


def _retention_now(store: Store, config: AtworksAgentConfig) -> datetime | None:
    """The clock at which EXACTLY the oldest day partition has aged out: that partition's local
    midnight plus ``retention_hot_days`` plus one day, so the archive cutoff
    (``now - hot_days``) lands on the NEXT partition's midnight and nothing newer moves."""
    oldest = store.oldest_run_day()
    if oldest is None:
        return None
    midnight = datetime.combine(date.fromisoformat(oldest), dtime.min,
                                tzinfo=ZoneInfo(config.briefing_tz))
    return midnight + timedelta(days=config.retention_hot_days + 1)


async def _retention_probe(dataset: Path, store: Store, config: AtworksAgentConfig,
                           work_dir: Path | None, mutate: bool) -> Row:
    name = "retention (one day partition)"
    when = _retention_now(store, config)
    if when is None:
        return Row(name, config.slo_retention_ms, note="empty dataset")
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        target, note_suffix = store, ""
        if not mutate:
            # The one destructive probe runs on a COPY: it moves runs out of the hot partition,
            # and a 2M-run dataset takes minutes to regenerate. Copy cost is outside the timing.
            copy = Path(tmp) / "retention.sqlite"
            shutil.copyfile(dataset / "scale.sqlite", copy)
            target = Store(copy)
            note_suffix = ", on a copy"
        retention = Retention(target, config, Path(tmp) / "insights")
        start = time.perf_counter()
        counts = await retention.run(when)
        ms = (time.perf_counter() - start) * 1000
        if target is not store:
            # Windows will not delete a file another handle still holds, and the copy lives
            # inside the TemporaryDirectory this `with` is about to remove.
            target.conn().close()
    return Row(name, config.slo_retention_ms, ms,
               note=f"archived {counts['runs_archived']:,}, bodies {counts['bodies_deleted']:,}"
                    f"{note_suffix}")


async def _sse_probe(backend: MockAtworks, session: AtworksSessionContext,
                     config: AtworksAgentConfig, amount_apis: set[str]) -> Row:
    """The event-loop non-blocking proof: while ``execute_job_once`` grinds a full 400-cell
    matrix as a task, a probe coroutine repeatedly does ``await asyncio.sleep(0)`` followed by
    one ``get_context`` -- exactly what an SSE turn does between chunks. The reported number is
    the WORST such round trip; if execution hogged the loop, this is where it shows."""
    # `amount_apis` are the ones carrying the `amount` param; pad with any other API to fill the
    # matrix. `dict.fromkeys` keeps insertion order AND dedupes -- a plain concat would repeat an
    # id that is in both lists (likely on a small custom dataset). `matrix_size` counts entries,
    # not distinct APIs, so a repeat would still claim 400 cells while executing one cell twice.
    api_ids = list(dict.fromkeys(sorted(amount_apis) + sorted(backend.apis)))[
        :config.max_apis_per_job]
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
    parser.add_argument("--mutate", action="store_true",
                        help="run the retention probe IN PLACE (it moves runs into the cold "
                             "partition). Default --no-mutate copies scale.sqlite for that probe.")
    parser.add_argument("--no-mutate", dest="mutate", action="store_false",
                        help=argparse.SUPPRESS)
    parser.add_argument("--i-know-this-destroys-the-dataset", dest="confirmed",
                        action="store_true",
                        help="required with --mutate. The retention probe ages out a day "
                             "partition IN PLACE, and the file is a generated dataset the other "
                             "18 rows are measured against -- once its hot partition has moved, "
                             "every later run of this bench measures a different set.")
    parser.set_defaults(mutate=False)
    args = parser.parse_args(argv)
    if args.mutate and not args.confirmed:
        # An earlier run emptied `mid` and `reduced` this way and nobody noticed until three
        # bench rows read suspiciously fast on an empty window. The flag is not a warning to
        # read, it is a second flag to type.
        parser.error("--mutate rewrites the dataset in place (the retention probe moves a day "
                     "partition into the cold table). Regenerating it takes minutes. Pass "
                     "--i-know-this-destroys-the-dataset as well if that is really what you want.")
    dataset = Path(args.db)
    if dataset.is_file():
        dataset = dataset.parent
    rows = asyncio.run(run_bench(dataset, mutate=args.mutate))
    print_table(rows)
    if args.json:
        args.json.write_text(json.dumps([asdict(r) | {"verdict": r.verdict} for r in rows],
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(r.counts and r.verdict == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
