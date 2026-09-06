"""select_where 해석 — stage 시점(executor)과 LATE 재해석(백엔드)이 같은 함수를 쓴다. 결정론이고
근거 문장(selection_basis)도 여기서 만든다; 모델 문장이 아니다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .backend import AtworksBackend
from .config import AtworksAgentConfig
from .jobs import SelectWhere
from .types import ApiSpec, AtworksSessionContext, RunResult, RunsQuery

CATALOGUE_SCAN_LIMIT = 1000
_API_PAGE = 200             # one catalogue page; the scan below pages by cursor, never one big read
# Sort floor for a watermark row whose api_updated_at is unknown (an API the catalogue dropped):
# it orders last rather than raising against the aware datetimes beside it.
_EPOCH = datetime(1, 1, 1, tzinfo=UTC)
# Appended ONCE per selection basis (see `truncation_noted` in resolve_select_where).
_TRUNCATED_NOTE = " (표본 상한 도달)"


@dataclass
class Resolution:
    api_ids: list[str]
    apis: dict[str, ApiSpec] = field(default_factory=dict)
    basis: str | None = None


def path_prefix(path: str, segments: int) -> str:
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts[:segments])


async def collect_runs(
    backend: AtworksBackend, session: AtworksSessionContext, *, cap: int,
    since: datetime | None = None, until: datetime | None = None, status: str | None = None,
    api_id: str | None = None, executed_by: str | None = None, job_id: str | None = None,
) -> list[RunResult]:
    """**Test-only** (M14). Gathers up to ``cap`` runs (newest first) by paging ``list_runs``.

    This was the transitional aggregation window between Task 2 and Task 8. Nothing in the agent
    or the host calls it any more — every aggregate read is rollup-fed and covers the whole
    window instead of its newest N runs — and it is deliberately NOT exported from
    ``atworks_agent``: the only importer is `host/tests/test_rewire.py`, which uses it to
    reproduce the old sampled window and show what it MISSED. Left in place because a test that
    proves "the sample was wrong, not merely slow" needs the sample."""
    items: list[RunResult] = []
    cursor: str | None = None
    while len(items) < cap:
        page = await backend.list_runs(session, RunsQuery(
            since=since, until=until, status=status, api_id=api_id, executed_by=executed_by,
            job_id=job_id, cursor=cursor, limit=min(200, cap - len(items)),
        ))
        items.extend(page.items)
        if page.next_cursor is None or not page.items:
            break
        cursor = page.next_cursor
    return items[:cap]


async def scan_apis(
    backend: AtworksBackend, session: AtworksSessionContext, *, cap: int, query: str = "",
    group: str | None = None, updated_after: datetime | None = None, path_prefix: str | None = None,
) -> tuple[dict[str, ApiSpec], bool]:
    """Pages ``search_apis`` by cursor up to ``cap`` specs (newest-updated first) and reports
    whether the catalogue had more. One oversized page is never requested -- paging is the read
    contract a REST adapter implements."""
    found: dict[str, ApiSpec] = {}
    cursor: str | None = None
    while len(found) < cap:
        page = await backend.search_apis(
            session, query=query, group=group, updated_after=updated_after, cursor=cursor,
            limit=min(_API_PAGE, cap - len(found)), path_prefix=path_prefix,
        )
        for api in page.items:
            found[api.api_id] = api
        if page.next_cursor is None or not page.items:
            return found, False
        cursor = page.next_cursor
    return found, True


async def resolve_select_where(
    backend: AtworksBackend, session: AtworksSessionContext, where: SelectWhere, config: AtworksAgentConfig, *, now: datetime
) -> Resolution:
    del now  # reserved for relative windows; kept in the signature so callers pass the execution clock
    scan_cap = CATALOGUE_SCAN_LIMIT if (where.related_to or where.failed_since) else config.max_apis_per_job + 1
    # "Is there any catalogue-side predicate at all?" -- if not, a `failed_since` selection needs
    # no catalogue page to start from and is driven off the watermark set instead (below).
    catalogue_filtered = bool(where.query or where.group or where.updated_after or where.related_to)
    apis: dict[str, ApiSpec] = {}
    truncated = False
    if catalogue_filtered or not where.failed_since:
        apis, truncated = await scan_apis(
            backend, session, cap=scan_cap, query=where.query, group=where.group,
            updated_after=where.updated_after,
        )
    basis: list[str] = []
    # The cap note belongs to the SELECTION, not to each clause. `related_to` and `failed_since`
    # share one catalogue scan (`truncated` is that scan's flag), so with both set the sentence
    # loop would stamp the same cap twice and read like two independent caps were hit.
    truncation_noted = False
    if where.related_to:
        anchor = await backend.get_api(session, where.related_to)
        if anchor is None:
            return Resolution(api_ids=[])
        prefix = path_prefix(anchor.path, config.impact_path_segments)
        # The impact set is "same group OR same path prefix". The prefix half is now its own
        # indexed scan (search_apis(path_prefix=...)) instead of a python filter over whatever
        # the first CATALOGUE_SCAN_LIMIT catalogue rows happened to be -- an API deep in a
        # 50k-row catalogue is found by the index, not by luck.
        by_prefix, prefix_truncated = await scan_apis(
            backend, session, cap=CATALOGUE_SCAN_LIMIT, query=where.query, group=where.group,
            updated_after=where.updated_after, path_prefix=prefix,
        )
        apis = {**apis, **by_prefix}
        truncated = truncated or prefix_truncated
        apis = {
            i: a for i, a in apis.items()
            if (anchor.group is not None and a.group == anchor.group) or path_prefix(a.path, config.impact_path_segments) == prefix
        }
        group_text = f"같은 group({anchor.group})" if anchor.group else "같은 group"
        sentence = f"{anchor.api_id}과 {group_text} 또는 {prefix}/*"
        if truncated and not truncation_noted:
            # The impact scan stopped at CATALOGUE_SCAN_LIMIT (either half of the union) — APIs
            # past it were never considered. Say so here too, not only on the failed_since branch:
            # a related_to-only selection was the one truncation the basis used to swallow.
            sentence += _TRUNCATED_NOTE
            truncation_noted = True
        basis.append(sentence)
    if where.failed_since:
        # The exact set of APIs with a non-pass run at or after the cutoff, straight off the
        # watermark table (last_non_pass_at) -- no run scan, and no 2000-run sample that could
        # miss an API that failed just outside it.
        marks = await backend.watermarks(session, last_non_pass_since=where.failed_since)
        if catalogue_filtered:
            # There IS a catalogue predicate to satisfy, so the watermark set is intersected with
            # the (capped) catalogue page and the cap is reported.
            failed_ids = {w.api_id for w in marks}
            apis = {i: a for i, a in apis.items() if i in failed_ids}
        else:
            # No catalogue predicate: the watermark result IS the answer, and intersecting it with
            # a <=CATALOGUE_SCAN_LIMIT catalogue page would cut an exact set down to whatever the
            # newest-updated 1000 rows happened to be. Drive from the watermarks instead, ordered
            # by the api_updated_at they already carry, and fetch only the specs that survive the
            # per-job cap.
            keep = config.max_apis_per_job + 1
            ordered_marks = sorted(
                marks, key=lambda w: (w.api_updated_at or _EPOCH, w.api_id), reverse=True,
            )[:keep]
            truncated = len(marks) > keep
            # ONE batch read for the specs, not one `get_api` per surviving watermark (M16):
            # this branch keeps up to `max_apis_per_job + 1` of them and used to await that many
            # round trips for ids it already held. Order is irrelevant -- the sort below is by
            # (updated_at, api_id) anyway.
            apis = {a.api_id: a for a in
                    await backend.get_apis(session, [m.api_id for m in ordered_marks])}
        sentence = f"{where.failed_since.date().isoformat()} 이후 실패·에러가 있던 API"
        if truncated and not truncation_noted:
            # The scan stopped at its cap — APIs past it were never considered, so the basis says
            # so rather than silently under-reporting.
            sentence += _TRUNCATED_NOTE
            truncation_noted = True
        basis.append(sentence)
    ordered = sorted(apis.values(), key=lambda a: (a.updated_at, a.api_id), reverse=True)
    # Cap the returned selection so an oversized result still trips the job guardrail with a
    # bounded list rather than the ledger holding (and provenance tracking) every match.
    capped = ordered[: config.max_apis_per_job + 1]
    ids = [a.api_id for a in capped]
    text = f"{' · '.join(basis)} — {len(ids)}개" if basis else None
    return Resolution(api_ids=ids, apis={a.api_id: a for a in capped}, basis=text[:160] if text else None)
