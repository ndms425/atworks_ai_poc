"""select_where 해석 — stage 시점(executor)과 LATE 재해석(백엔드)이 같은 함수를 쓴다. 결정론이고
근거 문장(selection_basis)도 여기서 만든다; 모델 문장이 아니다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .backend import AtworksBackend
from .config import AtworksAgentConfig
from .jobs import SelectWhere
from .types import ApiSpec, AtworksSessionContext

CATALOGUE_SCAN_LIMIT = 1000


@dataclass
class Resolution:
    api_ids: list[str]
    apis: dict[str, ApiSpec] = field(default_factory=dict)
    basis: str | None = None


def path_prefix(path: str, segments: int) -> str:
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts[:segments])


async def resolve_select_where(
    backend: AtworksBackend, session: AtworksSessionContext, where: SelectWhere, config: AtworksAgentConfig, *, now: datetime
) -> Resolution:
    del now  # reserved for relative windows; kept in the signature so callers pass the execution clock
    base = await backend.search_apis(
        session, query=where.query, group=where.group, updated_after=where.updated_after,
        limit=CATALOGUE_SCAN_LIMIT if (where.related_to or where.failed_since) else config.max_apis_per_job + 1,
    )
    apis = {a.api_id: a for a in base}
    basis: list[str] = []
    if where.related_to:
        anchor = await backend.get_api(session, where.related_to)
        if anchor is None:
            return Resolution(api_ids=[])
        prefix = path_prefix(anchor.path, config.impact_path_segments)
        apis = {
            i: a for i, a in apis.items()
            if (anchor.group is not None and a.group == anchor.group) or path_prefix(a.path, config.impact_path_segments) == prefix
        }
        group_text = f"같은 group({anchor.group})" if anchor.group else "같은 group"
        basis.append(f"{anchor.api_id}과 {group_text} 또는 {prefix}/*")
    if where.failed_since:
        failed = await backend.list_runs(session, since=where.failed_since, status="non_pass", api_id=None, limit=config.max_aggregate_runs)
        failed_ids = {r.api_id for r in failed}
        apis = {i: a for i, a in apis.items() if i in failed_ids}
        basis.append(f"{where.failed_since.date().isoformat()} 이후 실패·에러가 있던 API")
    ordered = sorted(apis.values(), key=lambda a: a.updated_at, reverse=True)
    ids = [a.api_id for a in ordered]
    text = f"{' · '.join(basis)} — {len(ids)}개" if basis else None
    return Resolution(api_ids=ids, apis={a.api_id: a for a in ordered}, basis=text[:160] if text else None)
