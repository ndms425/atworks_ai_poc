"""값 동등성 비교 엔진. 두 JSON 응답 본문(dict, 중첩 가능)을 리프 경로 단위로 비교하고,
ignore_paths를 뺀 뒤 남은 차이를 보고한다. 백엔드·세션에 의존하지 않는 순수 함수 모음:
같은 입력 → 같은 출력. `cluster_diffs`는 여러 행(row)을 diff_paths의 정확한 조합으로 묶어
"N rows differ only in [...]" 같은 노이즈 클러스터를 만든다 — 판정은 여기서 내리지 않는다."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

_MISSING = object()
"""compare_bodies에서 '누락'과 '값이 None'을 구별하기 위한 센티널. dict.get()의 기본값
None은 실제 JSON null 값과 구별되지 않으므로, 존재하지 않는 경로의 기본값으로 이 객체를
쓴다 (어느 쪽 dict에도 절대 값으로 나타나지 않는다)."""


class BodyDiff(BaseModel):
    equal: bool
    diff_paths: list[str]


class DiffCluster(BaseModel):
    paths: list[str]
    count: int
    row_keys: list[str]


def _paths(obj: Any, prefix: str = "$") -> Iterator[tuple[str, Any]]:
    """obj를 순회하며 (리프 경로, 값)을 낸다. 배열은 [i], 객체는 .key로 이어붙인다."""
    if isinstance(obj, Mapping):
        if not obj:
            yield prefix, obj
            return
        for key, value in obj.items():
            yield from _paths(value, f"{prefix}.{key}")
    elif isinstance(obj, list):
        if not obj:
            yield prefix, obj
            return
        for i, value in enumerate(obj):
            yield from _paths(value, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _is_ignored(path: str, ignore_paths: Sequence[str]) -> bool:
    for entry in ignore_paths:
        if path == entry or path.startswith(entry + ".") or path.startswith(entry + "["):
            return True
    return False


def compare_bodies(a: Any, b: Any, ignore_paths: Sequence[str]) -> BodyDiff:
    ap = {p: v for p, v in _paths(a) if not _is_ignored(p, ignore_paths)}
    bp = {p: v for p, v in _paths(b) if not _is_ignored(p, ignore_paths)}
    diff = sorted({p for p in set(ap) | set(bp) if ap.get(p, _MISSING) != bp.get(p, _MISSING)})
    return BodyDiff(equal=not diff, diff_paths=diff)


def _prune(obj: Any, prefix: str, ignore_paths: Sequence[str]) -> Any:
    """obj를 top-down으로 훑어, 경로가 ignore_paths에 걸리는 노드를 지운다. dict 키는
    제거하지만, 배열 원소는 제거 시 뒤 원소들이 앞으로 당겨지며 인덱스-경로 대응이 깨지므로
    (예: [10,20,30]에서 인덱스 1을 지우면 30이 인덱스 1로 밀려남) None 자리표시자로 바꿔
    길이와 인덱스를 그대로 유지한다."""
    if isinstance(obj, Mapping):
        return {
            key: _prune(value, f"{prefix}.{key}", ignore_paths)
            for key, value in obj.items()
            if not _is_ignored(f"{prefix}.{key}", ignore_paths)
        }
    if isinstance(obj, list):
        result = []
        for i, value in enumerate(obj):
            path = f"{prefix}[{i}]"
            if _is_ignored(path, ignore_paths):
                result.append(None)
            else:
                result.append(_prune(value, path, ignore_paths))
        return result
    return obj


def apply_ignore(body: dict, ignore_paths: Sequence[str]) -> dict:
    """body에서 ignore_paths에 해당하는 서브트리(리프 포함)를 제거한 복사본을 돌려준다."""
    return _prune(body, "$", ignore_paths)


def cluster_diffs(rows: Sequence[Mapping[str, Any]]) -> list[DiffCluster]:
    buckets: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for r in rows:
        buckets[tuple(r["diff_paths"])].append(r["row_key"])
    return sorted(
        (DiffCluster(paths=list(k), count=len(v), row_keys=v) for k, v in buckets.items()),
        key=lambda c: -c.count,
    )
