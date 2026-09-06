"""Deterministic synthetic dataset generator for the scale harness (spec 2026-09-06 §12, Task 7).

Writes a whole *dataset directory* -- not just a database file:

    <out>/scale.sqlite     the Store, filled through ``Store.ingest`` (so rollup_day /
                           current_state / api_watermark / operator_api are REAL, materialized by
                           the same code path execution takes)
    <out>/apis.json        every generated ApiSpec
    <out>/operators.json   every generated OperatorProfile (same row shape as the real fixture)
    <out>/runs.json        ``[]`` -- runs are already in the sqlite file
    <out>/meta.json        the generation parameters, the ``now`` anchor and the row-count summary

The trio of json files makes the directory a self-contained *fixtures dir*, so a bench or a test
opens the dataset with no constructor change at all::

    MockAtworks(config, out_dir, store=Store(out_dir / "scale.sqlite"))

``MockAtworks.__init__`` calls ``store.load_fixtures(out_dir)``: the empty ``runs.json`` is a
no-op ingest and ``apis.json`` re-mirrors idempotently (INSERT OR REPLACE of identical rows).

Determinism: one ``random.Random(seed)`` drives every draw, and every draw happens in a fixed
order, so the same ``--seed``/``--now``/size flags produce byte-identical run ids, statuses and
operators. ``--now`` defaults to the wall clock (the dataset is anchored so its newest run is
"just now"); pass it explicitly when two generations must be compared.

Injected patterns (all deterministic):

* **groups** -- 12 names on a skewed weight table, so ``group_by`` buckets are lopsided like real
  catalogues rather than uniform.
* **status mix** -- pass 88 / fail 9 / error 3 for ordinary cells.
* **flaky cells** -- 5% of (api, env, data) cells alternate pass/fail on their own sequence index,
  so they cross ``flaky_min_transitions`` regardless of how the days are sliced.
* **regressions** -- 2% of APIs pass until a per-API *flip instant* and fail after it; the API's
  ``updated_at`` **is** that instant, which puts it strictly between the last pass and the first
  failure -- exactly the ``regression_suspect`` predicate in ``aggregation._build``.
* **operator skew** -- a Zipf-ish weight table over the operators, so a few run most of the traffic.
* **bodies** -- only for runs inside ``retention_body_days`` (90) of ``now``; a small 3-5 key dict,
  and roughly 1% carry a PII-shaped string leaf so capture-time masking
  (``mask_body``/``policy_from_config``) is actually exercised on the way into ``bodies``.

Runs are emitted in strict chronological order and ingested in chronological batches -- the
in-order obligation Task 5's materialization documents.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from atworks_agent import (
    ApiSpec,
    AtworksAgentConfig,
    RunResult,
    RunStatus,
    mask_body,
    policy_from_config,
)
from atworks_host.store import Store

DEFAULT_OUT = Path(__file__).resolve().parent / "out"

# 12 group names on a skewed distribution (weights sum to 100) -- a real catalogue is never flat.
GROUP_WEIGHTS: dict[str, int] = {
    "payment": 18, "order": 15, "contract": 12, "member": 10, "settlement": 8, "product": 7,
    "shipping": 6, "notify": 6, "auth": 5, "report": 5, "batch": 4, "legacy": 4,
}
METHODS = ("GET", "POST", "PUT", "DELETE")
METHOD_WEIGHTS = (50, 30, 12, 8)
# `amount` is forced onto ~40% of APIs (see _build_apis) so a numeric-compare rule has a param
# many APIs share -- the simulate_rule bench path needs one.
PARAM_POOL = (
    "contractNo", "customerId", "orderId", "productId", "currency", "status", "requestedAt",
    "channel", "memberId", "quantity", "reason", "traceId",
)
RESOURCES = ("items", "detail", "summary", "search", "history", "status", "cancel", "confirm")
ENVS = ("dev", "stg")
LABELS = ("basic", "edge")
CELLS = [(env, label) for env in ENVS for label in LABELS]          # 4 cells per API
FAILED_RULE_POOL = (
    "amount >= 0", "responseCode == '0000'", "status in [OK, ACCEPTED]", "customerId required",
    "issuedAt matches iso8601", "totalCount >= 1", "refundAmount >= 0", "currency in [KRW, USD]",
)
ERROR_HTTP = (500, 502, 503, 504)
# PII-shaped samples for the ~1% of bodies that carry one. Every shape here is covered by a
# DEFAULT_MASKING_RULES pattern, so `bodies` must never contain the raw value.
PII_SAMPLES = (
    "문의: hong@example.com 으로 회신",
    "주민등록번호 900101-1234567 확인 완료",
    "카드 4111 1111 1111 1111 승인",
    "연락처 010-1234-5678",
    "계좌 110-234-567890 입금",
)
ROW_TABLES = ("apis", "runs", "bodies", "rollup_day", "current_state", "api_watermark", "operator_api")


def _weighted_pool(weights: list[float], size: int = 8192) -> list[int]:
    """A flat lookup table of indices repeated in proportion to ``weights`` -- one ``randrange``
    picks a weighted index, which is far cheaper per draw than ``random.choices`` in a 2M-run loop
    and just as deterministic."""
    total = sum(weights)
    pool: list[int] = []
    for index, weight in enumerate(weights):
        pool.extend([index] * max(1, round(size * weight / total)))
    return pool


def _day_boundary(day_start: datetime, tz: ZoneInfo) -> tuple[datetime, str, str]:
    """A 24h UTC bucket crosses exactly one local midnight. Returns that instant plus the local
    date string on either side, so the per-run ``day`` partition key is one comparison instead of
    a per-run timezone conversion (2M of those are not free)."""
    offset = tz.utcoffset(day_start) or timedelta(0)
    local_start = day_start + offset
    next_local_midnight = datetime.combine(
        local_start.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )
    return next_local_midnight - offset, local_start.date().isoformat(), (local_start.date() + timedelta(days=1)).isoformat()


def _parse_peak_days(values: list[str]) -> dict[int, int]:
    peaks: dict[int, int] = {}
    for raw in values or []:
        if raw.strip().lower() in ("", "off", "none"):
            continue
        day_str, _, count_str = raw.partition(":")
        peaks[int(day_str)] = int(count_str)
    return peaks


def _build_operators(count: int) -> list[dict]:
    roles = ("developer", "qa", "pm")
    return [
        {"operator_id": f"op-{i:04d}", "name": f"운영자 {i:04d}", "role": roles[i % 3]}
        for i in range(count)
    ]


def _build_apis(rng: random.Random, count: int, now: datetime, days: int) -> tuple[list[dict], list[bool], list[int]]:
    """Returns (api rows, is_regression flags, flaky-cell bitmasks). ``updated_at`` is provisional
    here; a regression API's is overwritten with its flip instant once the day schedule is known."""
    names = list(GROUP_WEIGHTS)
    group_pool = _weighted_pool([float(GROUP_WEIGHTS[g]) for g in names])
    method_pool = _weighted_pool([float(w) for w in METHOD_WEIGHTS])
    rows: list[dict] = []
    regressions: list[bool] = []
    flaky_masks: list[int] = []
    window = max(days, 1)
    for i in range(count):
        group = names[group_pool[rng.randrange(len(group_pool))]]
        method = METHODS[method_pool[rng.randrange(len(method_pool))]]
        resource = RESOURCES[i % len(RESOURCES)]
        params = rng.sample(PARAM_POOL, rng.randint(2, 4))
        if rng.random() < 0.40:
            params = ["amount", *params]
        is_regression = rng.random() < 0.02
        mask = 0
        if not is_regression:
            for cell_index in range(len(CELLS)):
                if rng.random() < 0.05:
                    mask |= 1 << cell_index
        rows.append({
            "api_id": f"api-{i:06d}",
            "method": method,
            "path": f"/v1/{group}/{resource}/{i:06d}",
            "name": f"{group} {resource} {i:06d}",
            "group": group,
            "updated_at": (now - timedelta(days=rng.uniform(0, window))).isoformat(),
            "has_rules": rng.random() < 0.25,
            "params": params,
        })
        regressions.append(is_regression)
        flaky_masks.append(mask)
    return rows, regressions, flaky_masks


def _day_schedule(n_apis: int, days: int, per_day: int, peaks: dict[int, int],
                  runs_per_api_day: int) -> list[tuple[int, int, int]]:
    """(day index, run count, active-API count) per day. The active set is a contiguous window
    over a fixed permutation of API positions, advanced every day -- so an API is exercised in
    bursts a few weeks apart (as a scheduled job matrix would) instead of one run everywhere."""
    schedule: list[tuple[int, int, int]] = []
    for day in range(days):
        count = peaks.get(day, per_day)
        active = max(1, min(n_apis, math.ceil(count / runs_per_api_day)))
        schedule.append((day, count, active))
    return schedule


def _day_cells(window: list[int], active: int, count: int):
    """(time slot, API position, cell index) for one day, **API-major**: one API's runs for the
    day are adjacent in time, the way a scheduled job's matrix actually executes. That is also
    what lets a cell be hit more than once inside a single local date, so ``rollup_day`` folds
    several runs into one row instead of degenerating into a copy of ``runs``."""
    base, extra = divmod(count, active)
    slot = 0
    for index in range(active):
        position = window[index]
        for occurrence_in_day in range(base + (1 if index < extra else 0)):
            yield slot, position, (occurrence_in_day + position) % len(CELLS)
            slot += 1


def generate(
    *, out: Path, apis: int, days: int, per_day: int, operators: int, now: datetime,
    peak_days: dict[int, int] | None = None, seed: int = 42, batch_size: int = 20_000,
    runs_per_api_day: int = 8, briefing_tz: str = "Asia/Seoul", body_days: int = 90,
    progress_every: int = 1, quiet: bool = False,
) -> dict:
    peak_days = peak_days or {}
    started = time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    tz = ZoneInfo(briefing_tz)

    api_rows, regressions, flaky_masks = _build_apis(rng, apis, now, days)
    operator_rows = _build_operators(operators)
    operator_pool = _weighted_pool([1.0 / (i + 1) ** 1.1 for i in range(operators)])

    # A fixed permutation of API positions; each day takes a contiguous, advancing window of it.
    order = list(range(apis))
    rng.shuffle(order)
    schedule = _day_schedule(apis, days, per_day, peak_days, runs_per_api_day)

    # Which days each API is active on -- needed to place a regression's flip instant on a day the
    # API actually runs, so it has passes before and failures after.
    active_days: dict[int, list[int]] = {}
    cursor = 0
    day_windows: list[list[int]] = []
    for day, _count, active in schedule:
        window = [order[(cursor + k) % apis] for k in range(active)]
        cursor = (cursor + active) % apis
        day_windows.append(window)
        for position in window:
            active_days.setdefault(position, []).append(day)

    day_bounds: list[tuple[datetime, datetime]] = []
    for day in range(days):
        day_end = now - timedelta(days=days - 1 - day)
        day_bounds.append((day_end - timedelta(days=1), day_end))

    flip_at: dict[int, datetime] = {}
    for position, is_regression in enumerate(regressions):
        if not is_regression:
            continue
        candidates = active_days.get(position, [])
        if len(candidates) < 2:
            regressions[position] = False   # nowhere to put a before/after boundary
            continue
        flip_day = candidates[max(1, len(candidates) // 2)] if len(candidates) > 2 else candidates[1]
        start, end = day_bounds[flip_day]
        instant = start + (end - start) / 2
        flip_at[position] = instant
        api_rows[position]["updated_at"] = instant.isoformat()

    apis_by_position = [ApiSpec(**row) for row in api_rows]
    body_cutoff = now - timedelta(days=body_days)
    policy = policy_from_config(AtworksAgentConfig(model="scale"))

    store = Store(out / "scale.sqlite")
    store.load_apis(apis_by_position)

    cell_seq = [0] * (apis * len(CELLS))
    batch: list[RunResult] = []
    ingested = 0
    seq = 0
    batches_done = 0
    per_day_counts: dict[str, int] = {}
    status_counts = {"pass": 0, "fail": 0, "error": 0}

    def flush() -> None:
        nonlocal batch, ingested, batches_done
        if not batch:
            return
        ingested += store.ingest(batch, briefing_tz, mask=lambda b: mask_body(b, policy))
        batches_done += 1
        batch = []
        if not quiet and progress_every and batches_done % progress_every == 0:
            print(f"  ... {ingested:,} runs ingested ({time.perf_counter() - started:.1f}s)", flush=True)

    for day, count, active in schedule:
        day_start, day_end = day_bounds[day]
        boundary, day_a, day_b = _day_boundary(day_start, tz)
        window = day_windows[day]
        step = (day_end - day_start) / (count + 1)
        for k, position, cell_index in _day_cells(window, active, count):
            api = apis_by_position[position]
            env, label = CELLS[cell_index]
            slot = position * len(CELLS) + cell_index
            occurrence = cell_seq[slot]
            cell_seq[slot] = occurrence + 1
            executed_at = day_start + step * (k + 1)

            if regressions[position]:
                status = RunStatus.PASS if executed_at < flip_at[position] else RunStatus.FAIL
            elif flaky_masks[position] & (1 << cell_index):
                status = RunStatus.PASS if occurrence % 2 == 0 else RunStatus.FAIL
            else:
                draw = rng.random()
                status = RunStatus.PASS if draw < 0.88 else (RunStatus.FAIL if draw < 0.97 else RunStatus.ERROR)

            if status is RunStatus.FAIL:
                failed_rules = [FAILED_RULE_POOL[rng.randrange(len(FAILED_RULE_POOL))]]
                http_status: int | None = 200
            elif status is RunStatus.ERROR:
                failed_rules = []
                http_status = ERROR_HTTP[rng.randrange(len(ERROR_HTTP))]
            else:
                failed_rules = []
                http_status = 200
            duration = rng.randint(25, 400) if rng.random() < 0.95 else rng.randint(400, 2500)
            operator = operator_rows[operator_pool[rng.randrange(len(operator_pool))]]["operator_id"]

            body = None
            if executed_at >= body_cutoff:
                body = {
                    "path": api.path,
                    "resultCode": "0000" if status is RunStatus.PASS else "E" + str(http_status),
                    "elapsedMs": duration,
                    "echo": {"env": env, "dataset": label},
                }
                if rng.random() < 0.01:
                    body["note"] = PII_SAMPLES[rng.randrange(len(PII_SAMPLES))]

            seq += 1
            day_key = day_a if executed_at < boundary else day_b
            per_day_counts[day_key] = per_day_counts.get(day_key, 0) + 1
            status_counts[status.value] += 1
            batch.append(RunResult(
                run_id=f"run-{seq:08d}", api_id=api.api_id, executed_at=executed_at,
                target_env=env, test_data_label=label, status=status, failed_rules=failed_rules,
                http_status=http_status, duration_ms=duration, response_body=body,
                job_id=f"job-{day:05d}-{operator}", executed_by=operator, day=day_key,
            ))
            if len(batch) >= batch_size:
                flush()
    flush()

    (out / "apis.json").write_text(json.dumps(api_rows, ensure_ascii=False), encoding="utf-8")
    (out / "operators.json").write_text(json.dumps(operator_rows, ensure_ascii=False), encoding="utf-8")
    (out / "runs.json").write_text("[]", encoding="utf-8")

    conn = store.conn()
    # Fold the WAL back into the main file so the dataset directory can be copied (a bench runs
    # a real job and writes; tests bench a copy) without losing the last transactions.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rows = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ROW_TABLES}
    elapsed = time.perf_counter() - started
    summary = {
        "out": str(out), "db": str(out / "scale.sqlite"), "seed": seed, "now": now.isoformat(),
        "apis": apis, "days": days, "per_day": per_day, "peak_days": {str(k): v for k, v in peak_days.items()},
        "operators": operators, "runs_per_api_day": runs_per_api_day, "briefing_tz": briefing_tz,
        "body_days": body_days, "batch_size": batch_size,
        "rows": rows, "status_counts": status_counts,
        "regression_apis": sum(1 for r in regressions if r),
        "flaky_cells": sum(bin(m).count("1") for m in flaky_masks),
        "runs_per_day": per_day_counts,
        "first_run_id": f"run-{1:08d}" if seq else None,
        "last_run_id": f"run-{seq:08d}" if seq else None,
        "elapsed_s": round(elapsed, 2),
        "ingested": ingested,
    }
    (out / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not quiet:
        print(f"\ngenerated {ingested:,} runs into {out} in {elapsed:.1f}s "
              f"({ingested / max(elapsed, 1e-9):,.0f} runs/s)")
        for table in ROW_TABLES:
            print(f"  {table:<16} {rows[table]:>12,}")
        print(f"  status mix       pass {status_counts['pass']:,} / fail {status_counts['fail']:,} "
              f"/ error {status_counts['error']:,}")
        print(f"  regression apis  {summary['regression_apis']:,}   flaky cells {summary['flaky_cells']:,}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apis", type=int, default=50_000)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--per-day", type=int, default=10_000)
    parser.add_argument("--peak-day", action="append", default=[],
                        metavar="DAY:COUNT", help="override one day's volume, e.g. 120:50000 (repeatable)")
    parser.add_argument("--operators", type=int, default=500)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--now", default=None, help="ISO instant the dataset ends at (default: wall clock)")
    parser.add_argument("--batch", type=int, default=20_000, help="runs per Store.ingest transaction")
    parser.add_argument("--runs-per-api-day", type=int, default=8)
    parser.add_argument("--body-days", type=int, default=90)
    parser.add_argument("--progress-every", type=int, default=1, help="print progress every N batches")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    generate(
        out=Path(args.out), apis=args.apis, days=args.days, per_day=args.per_day,
        peak_days=_parse_peak_days(args.peak_day), operators=args.operators, seed=args.seed,
        now=now, batch_size=args.batch, runs_per_api_day=args.runs_per_api_day,
        briefing_tz=AtworksAgentConfig(model="scale").briefing_tz, body_days=args.body_days,
        progress_every=args.progress_every, quiet=args.quiet,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
