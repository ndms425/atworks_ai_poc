"""``QuerySpec`` -> SQL, over the materialized tables (self-growth spec 2026-09-07 §3/§4).

This module is the whole of the "instant combination" promise: the model picks a shape out of a
fixed catalogue (``atworks_agent.catalog``) and the HOST turns it into one indexed statement. No
SQL string ever comes from the model, and no number is ever computed anywhere but here and in the
tables this reads.

**Source selection is the design.** Four sources answer the same algebra at wildly different
costs, and §4's table says which one a spec lands on:

===============================================  =====================  =========================
dimensions / filters                             source                 why
===============================================  =====================  =========================
default (api / path_* / method / group / env /   ``rollup_day ⋈ apis``  window is the indexed
data / day / week, or none at all)                                      ``day`` column
``failed_rule``/``http_status`` ALONE, with no   ``rollup_key_day``     one row IS one (day, key)
api-scoped filter
``failed_rule``/``http_status`` otherwise        ``rollup_day`` +       exact under a scope, and
                                                 ``json_each``          merely slower
``executed_by`` alone or with ``day``/``week``   ``rollup_operator_day``the only table that knows
                                                                        who ran what, per day
``executed_by`` x anything else, or ANY          ``runs``               the one run-table path
``executed_by`` FILTER
===============================================  =====================  =========================

The last row is the documented concession: ``rollup_day`` does not carry ``executed_by`` at all,
so "그 사람이 돌린 run만" cannot be expressed as a predicate over it -- there is nothing to filter.
The same reasoning generalizes to every source: a source is only chosen when EVERY filter of the
spec is exactly expressible over its columns (a filter on ``target_env`` cannot ride
``rollup_key_day``; a filter on ``http_status`` while grouping by ``failed_rule`` cannot ride
either map, because the two maps are independent projections of the same runs). Anything left
over falls back to ``runs``, which carries every column and is exact by construction.

**The window is day-granular, in ``briefing_tz``, for every source.** `since`/`until` are rounded
UP to the enclosing local midnight and the window is then a range of whole local days -- so
``day``/``week`` grouping, the compare-previous window and the four sources all count exactly the
same set of runs. A ragged window (09:00 -> 09:00) would make the rollup arms and the run arm
answer different totals for one spec, which is the one thing a query engine may not do; the
briefing keeps its own exact-edge path (``Store.aggregate_rollups``), which is a different read.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from atworks_agent import QuerySpec
from atworks_agent.types import QueryFilters

#: Filters that scope the query to a SET OF APIs. Their presence is what takes the two "fast"
#: sources off the table: ``rollup_key_day`` has no api column left to filter (its counters are
#: already summed across every API that touched the key) and ``rollup_operator_day`` never had one.
API_SCOPED_FILTERS = ("api_ids", "path_contains", "path_prefix", "method", "api_group", "scope_operator")

#: Filters that live on a rollup CELL: only ``rollup_day`` and ``runs`` carry these columns.
CELL_FILTERS = ("target_env", "test_data_label")

#: The two dimensions whose key lives inside a JSON map rather than in a column.
KEY_AXES = ("failed_rule", "http_status")

#: Dimensions that are read off the ``apis`` catalogue rather than off the run/rollup row.
APIS_DIMENSIONS = ("path_segment_1", "path_segment_2", "path_prefix_2", "method", "api_group")

#: JSON map column per key axis, on ``rollup_day``.
MAP_COLUMN = {"failed_rule": "failed_rule_counts", "http_status": "http_status_counts"}

#: ``QueryRow.api_ids`` / ``QueryRow.run_ids`` schema caps (spec §4 "표본 채움").
API_SAMPLE_CAP = 20
RUN_SAMPLE_CAP = 5


def path_segments(path: str) -> tuple[str | None, str | None, str | None]:
    """``("/v1/items/{id}")`` -> ``("v1", "items", "/v1/items")``.

    Segments are the ``/``-separated pieces with empty ones dropped, kept VERBATIM: ``{id}`` and a
    bare number are what the operator wrote in the spec, and normalizing them here would invent a
    grouping nobody asked for (catalog.py says exactly this to the model). A path with fewer than
    two segments gets ``None`` for what is missing and a prefix of whatever exists; a path with no
    segment at all is three ``None``s."""
    parts = [segment for segment in path.split("/") if segment]
    if not parts:
        return None, None, None
    seg1 = parts[0]
    seg2 = parts[1] if len(parts) > 1 else None
    return seg1, seg2, "/" + "/".join(parts[:2])


def resolve_window(spec: QuerySpec, now: datetime, default_days: int) -> tuple[datetime, datetime]:
    """Spec §3's window rule, verbatim and timezone-free: ``window_days`` means
    ``[now - window_days, now]``; an explicit ``since``/``until`` wins over it (they cannot be
    combined -- ``QueryFilters`` rejects that) and whichever half is missing falls back to
    ``now - default_days`` / ``now``; nothing at all means ``default_days``.

    The result is the RAW pair. Day alignment (``align_days``) needs a timezone and happens in
    ``compile_query``; keeping the two apart is what lets the alignment rule be stated -- and
    tested -- in one place."""
    filters = spec.filters
    if filters.window_days is not None:
        return now - timedelta(days=filters.window_days), now
    since = filters.since if filters.since is not None else now - timedelta(days=default_days)
    until = filters.until if filters.until is not None else now
    return since, until


def _ceil_local_day(moment: datetime, tz: ZoneInfo) -> date:
    """The local date the window boundary opens on: the moment's own local date when it falls
    exactly on local midnight, the next one otherwise. Both boundaries round the SAME way (up), so
    a window of N whole days stays N whole days and a half-open ``[midnight, midnight)`` pair is
    preserved exactly."""
    local = moment.astimezone(tz)
    if local.timetz() == time(0, 0, tzinfo=local.tzinfo):
        return local.date()
    return local.date() + timedelta(days=1)


def align_days(since: datetime, until: datetime, tz: str) -> tuple[str, str]:
    """``(day_from, day_to)``, inclusive local dates -- the days a rollup range predicate reads.
    ``day_to`` is one day before the (rounded-up) end boundary, so the pair is the closed-interval
    form of the half-open ``[since, until)`` window. An empty window comes back with
    ``day_to < day_from``, which every arm turns into "no rows" rather than into a silent full
    scan."""
    zone = ZoneInfo(tz)
    first = _ceil_local_day(since, zone)
    end_exclusive = _ceil_local_day(until, zone)
    return first.isoformat(), (end_exclusive - timedelta(days=1)).isoformat()


def shift_days(day_from: str, day_to: str) -> tuple[str, str]:
    """The window immediately before ``[day_from, day_to]``, of the same length in local days --
    ``compare_previous_window``'s other half. Computed in DAY space, never by subtracting a
    timedelta from a timestamp: the day window is what both queries actually read, so shifting it
    is what guarantees the two windows are the same length and share no day."""
    first, last = date.fromisoformat(day_from), date.fromisoformat(day_to)
    length = (last - first).days + 1
    return (first - timedelta(days=length)).isoformat(), (first - timedelta(days=1)).isoformat()


def day_bounds(day_from: str, day_to: str, tz: str) -> tuple[datetime, datetime]:
    """The exact instants the day window covers: ``[local midnight of day_from, local midnight of
    the day after day_to)``. This is what the ``runs`` source filters ``executed_at`` with (so it
    keeps ``idx_runs_executed_at`` / ``idx_runs_executed_by_executed_at``) and what
    ``QueryResult.window`` reports -- the same window, expressed twice."""
    zone = ZoneInfo(tz)
    first = datetime.combine(date.fromisoformat(day_from), time(0, 0), tzinfo=zone)
    end = datetime.combine(date.fromisoformat(day_to) + timedelta(days=1), time(0, 0), tzinfo=zone)
    return first, end


def week_key(day: str) -> str:
    """``"2026-09-07"`` -> ``"2026-W37"`` -- ISO year and ISO week, exactly Python's
    ``date.isocalendar()``. Written here as the one definition; ``WEEK_SQL`` below is the same
    function in SQLite, and a test holds the two together over several years of dates."""
    iso = date.fromisoformat(day).isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def week_days(key: str) -> tuple[str, str]:
    """The Monday and Sunday of an ISO week key -- the day range a ``week`` group covers, used to
    turn a returned row back into an indexed predicate for the sample fill."""
    year, week = key.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def week_sql(day_column: str) -> str:
    """``week_key`` as a SQLite expression over a ``YYYY-MM-DD`` TEXT column.

    ``strftime('%W')`` is NOT the ISO week (it counts Mondays from Jan 1 and has no ISO year), so
    the standard ISO reduction is used instead: the Thursday of a date's ISO week is
    ``date(d, '-3 days', 'weekday 4')`` -- back up three days, then step forward to the next
    Thursday -- and that Thursday's calendar year IS the ISO year while
    ``(dayofyear - 1) / 7 + 1`` IS the ISO week number. Both halves are integer arithmetic in
    SQLite, so the whole thing stays a scalar expression the GROUP BY can use."""
    thursday = f"date({day_column}, '-3 days', 'weekday 4')"
    return (f"strftime('%Y', {thursday}) || '-W' || "
            f"substr('0' || ((CAST(strftime('%j', {thursday}) AS INTEGER) - 1) / 7 + 1), -2, 2)")


def _label_in(label: str | None) -> str:
    """``test_data_label`` into the query's own domain: the materialized tables store ``''`` for
    "no data set bound", and the ``runs`` arm is COALESCEd to the same, so one filter value list
    and one key vocabulary serve every source (see ``Store``'s NULL-sentinel note)."""
    return label if label is not None else ""


@dataclass(frozen=True)
class CompiledQuery:
    """One spec, compiled. ``sql``/``params`` is the ranked, limited statement; ``totals_sql``
    answers ``total_groups`` and ``population`` over the SAME grouped subquery with no ORDER BY
    and no LIMIT, so the two can never disagree about which groups exist."""
    sql: str
    params: list
    source: str
    group_columns: list[str]
    #: measure aliases in ``spec.measures`` order (``m_fail_rate``, ...)
    measure_columns: list[str] = field(default_factory=list)
    totals_sql: str = ""
    totals_params: list = field(default_factory=list)
    day_from: str = ""
    day_to: str = ""
    #: ``rollup_key_day`` cannot COUNT(DISTINCT api_id) -- the ``apis`` measure is filled in
    #: python from the rows' stored ``api_ids`` union instead (exact; see ``Store.query``).
    apis_from_key_rows: bool = False
    #: the ``runs`` source's EXACT p95, as its own grouped statement (see ``_runs_p95``); empty
    #: on every other source, whose p95 is the rollups' documented max-merge level.
    p95_sql: str = ""
    p95_params: list = field(default_factory=list)


def _empty(value: object) -> bool:
    return value is None or (isinstance(value, list | tuple) and not value)


def _api_scoped(filters: QueryFilters) -> bool:
    return any(not _empty(getattr(filters, name)) for name in API_SCOPED_FILTERS)


def _cell_scoped(filters: QueryFilters) -> bool:
    return any(not _empty(getattr(filters, name)) for name in CELL_FILTERS)


def _key_filter_axes(filters: QueryFilters) -> set[str]:
    return {axis for axis in KEY_AXES if not _empty(getattr(filters, axis))}


def select_source(spec: QuerySpec) -> str:
    """Spec §4's table, as code. Every branch that ends in ``"runs"`` does so because a filter or
    a dimension of this spec is NOT exactly expressible over the cheaper table -- never because
    the cheaper table would merely be awkward."""
    dims = set(spec.dimensions)
    filters = spec.filters
    key_dims = dims & set(KEY_AXES)
    key_filters = _key_filter_axes(filters)

    # A run filter on the executor: no rollup carries `executed_by` as a filterable column
    # (`rollup_operator_day` carries it as the KEY, but the rule stays uniform -- one shape of
    # question, one source -- and `runs` is exact under every other filter too).
    if not _empty(filters.executed_by):
        return "runs"
    if "executed_by" in dims:
        if (dims <= {"executed_by", "day", "week"} and not _api_scoped(filters)
                and not _cell_scoped(filters) and not key_filters):
            return "rollup_operator_day"
        return "runs"
    # A key filter on an axis this query does NOT group by cannot be applied to either rollup: the
    # two JSON maps are independent projections of the same runs, so "cells whose http_status was
    # 500" is not derivable from a failed_rule map (nor from the cell counters).
    if key_filters - key_dims:
        return "runs"
    if len(key_dims) == 2:
        # ...and the cross product of the two maps is not derivable from them either.
        return "runs"
    if key_dims:
        if len(dims) == 1 and not _api_scoped(filters) and not _cell_scoped(filters):
            if filters.status is not None and filters.status != "all" and "apis" in spec.measures:
                # `rollup_key_day.api_ids` is the set of APIs behind a (day, key) with NO verdict
                # split, so a status-filtered COUNT(DISTINCT api_id) cannot come off it. The
                # `json_each` arm splits exactly (each map value carries its own count/fail/error).
                return "rollup_day"
            return "rollup_key_day"
        return "rollup_day"
    return "rollup_day"


def _needs_apis(spec: QuerySpec) -> bool:
    filters = spec.filters
    return (any(d in APIS_DIMENSIONS for d in spec.dimensions)
            or any(not _empty(getattr(filters, name))
                   for name in ("path_contains", "path_prefix", "method", "api_group")))


def _in_clause(column: str, values, params: list, cast=str) -> str:
    placeholders = ",".join("?" for _ in values)
    params.extend(cast(v) for v in values)
    return f"{column} IN ({placeholders})"


def _apis_predicates(filters: QueryFilters, params: list) -> list[str]:
    """The predicates that read the catalogue (alias ``a``). ``path_contains`` is a lower-cased
    OR of substrings, ``path_prefix`` is anchored (``= p OR LIKE p/%``) exactly as
    ``search_apis`` anchors it -- a prefix pushed through a substring match would also match the
    middle of a path."""
    clauses: list[str] = []
    if not _empty(filters.path_contains):
        parts = []
        for needle in filters.path_contains:
            parts.append("LOWER(a.path) LIKE ?")
            params.append(f"%{needle.lower()}%")
        clauses.append("(" + " OR ".join(parts) + ")")
    if filters.path_prefix is not None:
        clauses.append("(a.path = ? OR a.path LIKE ?)")
        params += [filters.path_prefix, f"{filters.path_prefix}/%"]
    if not _empty(filters.method):
        clauses.append(_in_clause("a.method", filters.method, params))
    if not _empty(filters.api_group):
        clauses.append(_in_clause('a."group"', filters.api_group, params))
    return clauses


def _scope_operator_clause(operator_id: str | None, since: datetime | None, column: str,
                           params: list) -> list[str]:
    """The server-side operator scope, byte-for-byte the shape ``Store._operator_scope_clause``
    uses (one covering range scan of ``idx_operator_api_window``), including the unary ``+`` that
    tells SQLite not to drive the OUTER query off this term -- the outer query needs its own
    day/executed_at index far more than it needs a seek per scoped API."""
    if operator_id is None:
        return []
    params.append(operator_id)
    sql = f"+{column} IN (SELECT api_id FROM operator_api WHERE operator_id = ?"
    if since is not None:
        sql += " AND last_executed_at >= ?"
        params.append(since.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z")
    return [sql + ")"]


def _counter_exprs(status: str | None, c: str, p: str, f: str, e: str) -> tuple[str, str, str, str]:
    """``(runs, pass, fail, error)`` as SUM expressions under ``status``. A rollup row keeps the
    three verdict counters side by side, so a status filter is a CHOICE OF COUNTERS and never a
    scan -- the same rule ``Store._counts`` applies to ``aggregate_runs``."""
    if status == "pass":
        return f"SUM({p})", f"SUM({p})", "0", "0"
    if status == "fail":
        return f"SUM({f})", "0", f"SUM({f})", "0"
    if status == "error":
        return f"SUM({e})", "0", "0", f"SUM({e})"
    if status == "non_pass":
        return f"SUM({f}) + SUM({e})", "0", f"SUM({f})", f"SUM({e})"
    return f"SUM({c})", f"SUM({p})", f"SUM({f})", f"SUM({e})"


def _row_hit_expr(status: str | None, c: str, p: str, f: str, e: str) -> str:
    """The per-SOURCE-ROW count under ``status`` -- what ``COUNT(DISTINCT CASE WHEN ... > 0 THEN
    api_id END)`` tests, so that an API whose rows in this window hold no run of the filtered
    status does not inflate the ``apis`` measure."""
    if status == "pass":
        return p
    if status == "fail":
        return f
    if status == "error":
        return e
    if status == "non_pass":
        return f"({f}) + ({e})"
    return c


def _status_of(spec: QuerySpec) -> str | None:
    status = spec.filters.status
    return None if status in (None, "all") else status


def compile_query(spec: QuerySpec, *, now: datetime, tz: str, previous: bool = False,
                  default_days: int = 30) -> CompiledQuery:
    """Compile ``spec`` into one ranked, limited statement plus its totals statement.

    ``previous=True`` compiles the SAME spec over the window immediately before it (same length,
    no shared day) -- ``compare_previous_window`` runs the two and joins them in python.
    """
    since, until = resolve_window(spec, now, default_days)
    day_from, day_to = align_days(since, until, tz)
    if previous:
        day_from, day_to = shift_days(day_from, day_to)
    source = select_source(spec)
    status = _status_of(spec)

    builder = _BUILDERS[source]
    return builder(spec, status, day_from, day_to, tz)


# -- per-source builders -------------------------------------------------------------------------


def _finish(spec: QuerySpec, source: str, *, from_sql: str, from_params: list,
            where: list[str], where_params: list, keys: list[str],
            c: str, p: str, f: str, e: str, t: str | None, q95: str | None, api: str | None,
            status: str | None, day_from: str, day_to: str,
            apis_from_key_rows: bool = False) -> CompiledQuery:
    """Assemble the grouped SELECT every source shares: key columns, measure columns, the
    zero-group HAVING, the rank and the cut -- and the totals query over the same subquery.

    The HAVING drops groups whose FILTERED count is zero (a cell with no ``error`` run under
    ``status=error``). Those are not groups: the oracle, which buckets filtered runs, never
    creates them, and leaving them in would spend limit slots on empty rows."""
    runs_e, pass_e, fail_e, error_e = _counter_exprs(status, c, p, f, e)
    non_pass_e = f"({fail_e}) + ({error_e})"
    if api is None:
        apis_e = "NULL"
    else:
        apis_e = f"COUNT(DISTINCT CASE WHEN ({_row_hit_expr(status, c, p, f, e)}) > 0 THEN {api} END)"
    measure_sql = {
        "runs": runs_e,
        "pass": pass_e,
        "fail": fail_e,
        "error": error_e,
        "non_pass": non_pass_e,
        "fail_rate": f"ROUND(1.0 * ({non_pass_e}) / NULLIF({runs_e}, 0), 4)",
        "apis": apis_e,
        "transitions": f"SUM({t})" if t is not None else "NULL",
        "p95_duration_ms": f"MAX({q95})" if q95 is not None else "NULL",
    }

    select_parts = [f"{expr} AS k{i}" for i, expr in enumerate(keys)]
    measure_columns = [f"m_{name}" for name in spec.measures]
    select_parts += [f"{measure_sql[name]} AS m_{name}" for name in spec.measures]
    if "runs" not in spec.measures:
        # `population` is the sum of the `runs` measure over every group, so the column exists
        # whether or not the model asked to SEE it.
        select_parts.append(f"{runs_e} AS m_runs")
    group_columns = [f"k{i}" for i in range(len(keys))]

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    group_sql = f" GROUP BY {', '.join(group_columns)}" if group_columns else ""
    grouped = (f"SELECT {', '.join(select_parts)} FROM {from_sql}{where_sql}{group_sql} "
               f"HAVING ({runs_e}) > 0")
    params = [*from_params, *where_params]

    if spec.order_by == "key":
        # spec §3: the key order is ASCENDING, `descending` does not apply to it (it is the
        # "browse the axis" order, not a rank).
        order_terms = [f"{col} ASC" for col in group_columns] or ["1"]
    else:
        direction = "DESC" if spec.descending else "ASC"
        order_terms = [f"m_{spec.order_by} {direction}"] + [f"{col} ASC" for col in group_columns]
    sql = f"{grouped} ORDER BY {', '.join(order_terms)} LIMIT ?"
    totals_sql = f"SELECT COUNT(*), COALESCE(SUM(m_runs), 0) FROM ({grouped})"
    return CompiledQuery(
        sql=sql, params=[*params, spec.limit], source=source, group_columns=group_columns,
        measure_columns=measure_columns, totals_sql=totals_sql, totals_params=list(params),
        day_from=day_from, day_to=day_to, apis_from_key_rows=apis_from_key_rows,
    )


def _rollup_day(spec: QuerySpec, status: str | None, day_from: str, day_to: str,
                tz: str) -> CompiledQuery:
    """The default source: one row per (day, api, env, data), joined to the catalogue for the
    path/method/group dimensions. The two key axes reach it through ``json_each`` over the row's
    own map -- exact under any scope, and the only arm that can group a key axis TOGETHER with
    another dimension."""
    filters = spec.filters
    key_axis = next((d for d in spec.dimensions if d in KEY_AXES), None)
    needs_apis = _needs_apis(spec)
    from_sql = "rollup_day AS r"
    if needs_apis:
        from_sql += " LEFT JOIN apis AS a ON a.api_id = r.api_id"
    if key_axis is not None:
        from_sql += f" JOIN json_each(r.{MAP_COLUMN[key_axis]}) AS je"

    where: list[str] = ["r.day >= ?", "r.day <= ?"]
    params: list = [day_from, day_to]
    if not _empty(filters.api_ids):
        where.append(_in_clause("r.api_id", filters.api_ids, params))
    if not _empty(filters.target_env):
        where.append(_in_clause("r.target_env", filters.target_env, params))
    if not _empty(filters.test_data_label):
        where.append(_in_clause("r.test_data_label", filters.test_data_label, params, _label_in))
    if key_axis is not None and not _empty(getattr(filters, key_axis)):
        where.append(_in_clause("je.key", getattr(filters, key_axis), params))
    where += _apis_predicates(filters, params)
    since_dt, _ = day_bounds(day_from, day_to, tz)
    where += _scope_operator_clause(filters.scope_operator, since_dt, "r.api_id", params)

    if key_axis is not None:
        c = "json_extract(je.value, '$.count')"
        f_ = "json_extract(je.value, '$.fail')"
        e = "json_extract(je.value, '$.error')"
        p = f"({c} - {f_} - {e})"
        t = None            # the maps carry no per-key transition counter, and never did
    else:
        c, p, f_, e, t = 'r."count"', 'r."pass"', "r.fail", "r.error", "r.transitions"

    keys = [_ROLLUP_DAY_KEY[d](key_axis) for d in spec.dimensions]
    return _finish(spec, "rollup_day", from_sql=from_sql, from_params=[], where=where,
                   where_params=params, keys=keys, c=c, p=p, f=f_, e=e, t=t,
                   q95="r.p95_duration_ms", api="r.api_id", status=status,
                   day_from=day_from, day_to=day_to)


#: dimension -> key expression on the ``rollup_day`` arm (``_`` is the query's key axis, so the
#: two map dimensions resolve to ``je.key`` and everything else ignores it).
_ROLLUP_DAY_KEY = {
    "api": lambda _axis: "r.api_id",
    "path_segment_1": lambda _axis: "a.path_segment_1",
    "path_segment_2": lambda _axis: "a.path_segment_2",
    "path_prefix_2": lambda _axis: "a.path_prefix_2",
    "method": lambda _axis: "a.method",
    "api_group": lambda _axis: 'a."group"',
    "target_env": lambda _axis: "r.target_env",
    "test_data_label": lambda _axis: "r.test_data_label",
    "failed_rule": lambda _axis: "je.key",
    "http_status": lambda _axis: "je.key",
    "day": lambda _axis: "r.day",
    "week": lambda _axis: week_sql("r.day"),
}


def _rollup_key_day(spec: QuerySpec, status: str | None, day_from: str, day_to: str,
                    tz: str) -> CompiledQuery:
    """The transposed key axis: one stored row IS one (day, axis, key) group, so a window costs
    (days x keys) rows instead of ``json_each`` over every cell row in it. Reachable only when the
    key axis is the ONLY dimension and nothing scopes the query to a set of APIs or cells."""
    filters = spec.filters
    axis = next(d for d in spec.dimensions if d in KEY_AXES)
    where = ["k.axis = ?", "k.day >= ?", "k.day <= ?"]
    params: list = [axis, day_from, day_to]
    if not _empty(getattr(filters, axis)):
        where.append(_in_clause('k."key"', getattr(filters, axis), params))
    keys = ['k."key"' if d in KEY_AXES else ("k.day" if d == "day" else week_sql("k.day"))
            for d in spec.dimensions]
    return _finish(spec, "rollup_key_day", from_sql="rollup_key_day AS k", from_params=[],
                   where=where, where_params=params, keys=keys,
                   c='k."count"', p='(k."count" - k.fail - k.error)', f="k.fail", e="k.error",
                   t=None, q95="k.p95_duration_ms", api=None, status=status,
                   day_from=day_from, day_to=day_to, apis_from_key_rows="apis" in spec.measures)


def _rollup_operator_day(spec: QuerySpec, status: str | None, day_from: str, day_to: str,
                         tz: str) -> CompiledQuery:
    """"실행자별" without a run scan. The table has no api column and no duration, so ``apis`` and
    ``p95_duration_ms`` come back NULL here -- honestly NULL, rather than a number derived from a
    different population."""
    keys = [("o.operator_id" if d == "executed_by" else
             ("o.day" if d == "day" else week_sql("o.day"))) for d in spec.dimensions]
    return _finish(spec, "rollup_operator_day", from_sql="rollup_operator_day AS o",
                   from_params=[], where=["o.day >= ?", "o.day <= ?"],
                   where_params=[day_from, day_to], keys=keys,
                   c='o."count"', p='o."pass"', f="o.fail", e="o.error", t=None, q95=None,
                   api=None, status=status, day_from=day_from, day_to=day_to)


def _runs(spec: QuerySpec, status: str | None, day_from: str, day_to: str,
          tz: str) -> CompiledQuery:
    """The one run-table path (spec §4's last two rows). Every column exists here, so this arm is
    exact under any combination -- it is chosen only when no rollup can express the spec.

    The status filter rides the WHERE clause rather than the counter choice (both give the same
    numbers; the predicate additionally makes the ``apis`` count and the sample fill read the
    filtered population). ``failed_rule`` as a DIMENSION wraps the table in a ``SELECT DISTINCT``
    over ``json_each`` -- the same shape ``Store._edge_arm`` uses -- so a run listing one rule
    twice is one row and a non-pass run naming no rule gets ``aggregation._keys``' synthetic
    ``(error) HTTP nnn`` key."""
    filters = spec.filters
    since_dt, until_dt = day_bounds(day_from, day_to, tz)
    fr_dim = "failed_rule" in spec.dimensions

    run_where: list[str] = ["runs.executed_at >= ?", "runs.executed_at < ?"]
    run_params: list = [_sql_iso(since_dt), _sql_iso(until_dt)]
    if status is not None:
        if status == "non_pass":
            run_where.append("runs.status != 'pass'")
        else:
            run_where.append("runs.status = ?")
            run_params.append(status)
    if not _empty(filters.api_ids):
        run_where.append(_in_clause("runs.api_id", filters.api_ids, run_params))
    if not _empty(filters.target_env):
        run_where.append(_in_clause("runs.target_env", filters.target_env, run_params))
    if not _empty(filters.test_data_label):
        run_where.append(_in_clause("COALESCE(runs.test_data_label, '')", filters.test_data_label,
                                    run_params, _label_in))
    if not _empty(filters.executed_by):
        run_where.append(_in_clause("runs.executed_by", filters.executed_by, run_params))
    if not _empty(filters.http_status):
        run_where.append(_in_clause("runs.http_status", filters.http_status, run_params, int))
    if not _empty(filters.failed_rule) and not fr_dim:
        run_where.append(_failed_rule_predicate(filters.failed_rule, run_params))
    run_where += _scope_operator_clause(filters.scope_operator, since_dt, "runs.api_id", run_params)
    if fr_dim:
        run_where.append("runs.status != 'pass'")   # a pass run yields no failed_rule key
    if "executed_by" in spec.dimensions:
        # "실행자별" means the runs that HAVE an executor. An unattributed run belongs to no
        # operator (`rollup_operator_day` skips it at ingest, as `operator_api` always has), so
        # the run arm must not invent a NULL operator group the rollup arm cannot produce -- one
        # spec, two sources, one answer.
        run_where.append("runs.executed_by IS NOT NULL")

    from_params: list = []
    outer_where: list[str] = []
    outer_params: list = []
    if fr_dim:
        from_sql = (
            "(SELECT DISTINCT COALESCE(je.value, '(error) HTTP ' || "
            "COALESCE(CAST(runs.http_status AS TEXT), '(none)')) AS fr_key, runs.* "
            "FROM runs LEFT JOIN json_each(runs.failed_rules) je "
            f"WHERE {' AND '.join(run_where)}) AS runs"
        )
        from_params = run_params
    else:
        from_sql = "runs"
        outer_where, outer_params = list(run_where), list(run_params)
    if fr_dim and not _empty(filters.failed_rule):
        # A ``failed_rule`` filter alongside the ``failed_rule`` DIMENSION restricts the KEYS, not
        # the runs -- exactly what ``je.key IN (...)`` does on the rollup arms. Applied at run
        # level instead, a run naming both a wanted and an unwanted rule would drag the unwanted
        # rule's group in with it, and the two sources would disagree about the same spec.
        outer_where.append(_in_clause("runs.fr_key", filters.failed_rule, outer_params))
    if _needs_apis(spec):
        from_sql += " LEFT JOIN apis AS a ON a.api_id = runs.api_id"
        outer_where += _apis_predicates(filters, outer_params)

    keys = [_RUNS_KEY[d] for d in spec.dimensions]
    compiled = _finish(spec, "runs", from_sql=from_sql, from_params=from_params, where=outer_where,
                       where_params=outer_params, keys=keys,
                       c="1", p="(runs.status = 'pass')", f="(runs.status = 'fail')",
                       e="(runs.status = 'error')", t=None, q95=None, api="runs.api_id",
                       status=None, day_from=day_from, day_to=day_to)
    if "p95_duration_ms" in spec.measures:
        sql, params = _runs_p95(keys, from_sql, from_params, outer_where, outer_params)
        compiled = replace(compiled, p95_sql=sql, p95_params=params)
    return compiled


def _runs_p95(keys: list[str], from_sql: str, from_params: list, where: list[str],
              where_params: list) -> tuple[str, list]:
    """The ``runs`` source's p95, computed EXACTLY -- ``aggregation._p95``'s definition
    (``sorted[ceil(0.95*n) - 1]``) as a window function rather than as a rollup level.

    The rollup arms report the documented max-merge approximation because ``rollup_day`` never
    kept raw durations; here the durations are right there, so reporting an approximation would be
    a worse answer for no reason. ``(95 * n + 99) / 100`` is ``ceil(0.95 * n)`` in SQLite's
    integer arithmetic, and it is the 1-based rank of the value ``_p95`` picks."""
    partition = f"PARTITION BY {', '.join(keys)} " if keys else ""
    select = [f"{expr} AS k{i}" for i, expr in enumerate(keys)]
    group = [f"k{i}" for i in range(len(keys))]
    where_sql = f" WHERE {' AND '.join([*where, 'runs.duration_ms IS NOT NULL'])}"
    inner = (
        f"SELECT {', '.join([*select, 'runs.duration_ms AS d'])}, "
        f"ROW_NUMBER() OVER ({partition}ORDER BY runs.duration_ms) AS rn, "
        f"(95 * COUNT(*) OVER ({partition.rstrip()}) + 99) / 100 AS target "
        f"FROM {from_sql}{where_sql}"
    )
    group_sql = f" GROUP BY {', '.join(group)}" if group else ""
    columns = ", ".join([*group, "MAX(CASE WHEN rn = target THEN d END) AS p95"])
    return f"SELECT {columns} FROM ({inner}){group_sql}", [*from_params, *where_params]


def _failed_rule_predicate(values, params: list) -> str:
    """"a run whose ``aggregation._keys(run, 'failed_rule')`` contains one of these" -- both
    halves of that definition: a named rule inside the JSON array, and the synthetic
    ``(error) HTTP nnn`` key a non-pass run with no named rule produces."""
    named = _in_clause("je2.value", values, params)
    synthetic = _in_clause("('(error) HTTP ' || COALESCE(CAST(runs.http_status AS TEXT), '(none)'))",
                           values, params)
    return (f"(EXISTS (SELECT 1 FROM json_each(runs.failed_rules) je2 WHERE {named}) "
            f"OR (runs.status != 'pass' "
            f"AND json_array_length(COALESCE(runs.failed_rules, '[]')) = 0 AND {synthetic}))")


def sample_predicates(spec: QuerySpec, day_from: str, day_to: str,
                      tz: str) -> tuple[list[str], list]:
    """The spec's whole filter set, expressed over ``runs`` ALONE (alias ``runs``, no join) --
    what the evidence-sample fill runs under. The api-scoped filters become one
    ``api_id IN (SELECT ... FROM apis)`` subquery instead of a join, so the outer statement keeps
    driving off a ``runs`` index; the run-level filters are the identical clauses the ``runs``
    source builds, so a sample can never come from a population the row did not count."""
    filters = spec.filters
    since_dt, until_dt = day_bounds(day_from, day_to, tz)
    status = _status_of(spec)
    where: list[str] = ["runs.executed_at >= ?", "runs.executed_at < ?"]
    params: list = [_sql_iso(since_dt), _sql_iso(until_dt)]
    if status is not None:
        if status == "non_pass":
            where.append("runs.status != 'pass'")
        else:
            where.append("runs.status = ?")
            params.append(status)
    if not _empty(filters.api_ids):
        where.append(_in_clause("runs.api_id", filters.api_ids, params))
    if not _empty(filters.target_env):
        where.append(_in_clause("runs.target_env", filters.target_env, params))
    if not _empty(filters.test_data_label):
        where.append(_in_clause("COALESCE(runs.test_data_label, '')", filters.test_data_label,
                                params, _label_in))
    if not _empty(filters.executed_by):
        where.append(_in_clause("runs.executed_by", filters.executed_by, params))
    if not _empty(filters.http_status):
        where.append(_in_clause("runs.http_status", filters.http_status, params, int))
    if not _empty(filters.failed_rule):
        where.append(_failed_rule_predicate(filters.failed_rule, params))
    api_clauses: list[str] = []
    api_params: list = []
    api_clauses += _apis_predicates(filters, api_params)
    if api_clauses:
        where.append(f"runs.api_id IN (SELECT a.api_id FROM apis AS a WHERE {' AND '.join(api_clauses)})")
        params += api_params
    where += _scope_operator_clause(filters.scope_operator, since_dt, "runs.api_id", params)
    return where, params


def row_key_predicates(dimension: str, value: str | None, params: list) -> list[str]:
    """"the runs behind THIS returned row", as an indexed predicate over ``runs``. ``IS`` rather
    than ``=`` throughout: a group key can legitimately be NULL (an API with no group, a run with
    no executor) and ``= NULL`` matches nothing."""
    if dimension in APIS_DIMENSIONS:
        column = 'a."group"' if dimension == "api_group" else f"a.{dimension}"
        params.append(value)
        return [f"runs.api_id IN (SELECT a.api_id FROM apis AS a WHERE {column} IS ?)"]
    if dimension == "failed_rule":
        return ["runs.status != 'pass'", _failed_rule_predicate([value], params)]
    if dimension == "week":
        first, last = week_days(value or "")
        params += [first, last]
        return ["runs.day >= ?", "runs.day <= ?"]
    column = {
        "api": "runs.api_id",
        "target_env": "runs.target_env",
        "test_data_label": "COALESCE(runs.test_data_label, '')",
        "http_status": "COALESCE(CAST(runs.http_status AS TEXT), '(none)')",
        "executed_by": "runs.executed_by",
        "day": "runs.day",
    }[dimension]
    params.append(_label_in(value) if dimension == "test_data_label" else value)
    return [f"{column} IS ?"]


_RUNS_KEY = {
    "api": "runs.api_id",
    "path_segment_1": "a.path_segment_1",
    "path_segment_2": "a.path_segment_2",
    "path_prefix_2": "a.path_prefix_2",
    "method": "a.method",
    "api_group": 'a."group"',
    "target_env": "runs.target_env",
    "test_data_label": "COALESCE(runs.test_data_label, '')",
    "failed_rule": "runs.fr_key",
    "http_status": "COALESCE(CAST(runs.http_status AS TEXT), '(none)')",
    "executed_by": "runs.executed_by",
    "day": "runs.day",
    "week": week_sql("runs.day"),
}


def _sql_iso(moment: datetime) -> str:
    """``Store._iso``'s format, duplicated here rather than imported so this module stays free of
    a store import (the store imports IT). Fixed-width UTC with six fractional digits: the only
    reason plain string comparison on ``executed_at`` is chronological comparison."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


_BUILDERS = {
    "rollup_day": _rollup_day,
    "rollup_key_day": _rollup_key_day,
    "rollup_operator_day": _rollup_operator_day,
    "runs": _runs,
}
