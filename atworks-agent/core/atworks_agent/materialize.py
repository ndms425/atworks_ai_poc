"""인입 시점 물질화의 순수 계산부 (scale spec 2026-09-06 §4). 새로 들어온 run 배치 하나와
"직전까지의 셀 상태"를 받아, 저장소가 그대로 upsert 하면 되는 **증분**을 돌려준다:

* ``rollup_day`` 증분 — (day, api_id, target_env, test_data_label) 단위 count/pass/fail/error/
  transitions/p95/failed_rule_counts,
* ``current_state`` 새 행 — 셀별 최신 run + 누적 전환 수,
* ``api_watermark`` 갱신분 — API별 last_pass / first_non_pass / last_non_pass / latest_status,
* ``operator_api`` 증분 — (operator, api) 별 마지막 실행 시각과 건수.

이 모듈에는 SQL도, I/O도, 시계도 없다. 저장소(``host/atworks_host/store.py``)는 트랜잭션과
병합만 맡고, "무엇이 얼마나 늘어나는가"는 전부 여기서 결정된다 — 그래서 이 계산은 aggregation.py
(재계산 오라클)와 나란히 property test로 등가성을 검증할 수 있다.

정렬 계약: ``rollup_delta``는 배치를 ``(executed_at, run_id)`` 오름차순으로 **스스로** 정렬해
처리한다(오라클 ``aggregation.aggregate`` / ``Store.recompute_*``와 같은 순서). ``prev_state``는
그 셀의 "직전 상태"로서 첫 비교의 씨앗이 된다.

한계(문서화된 근사):
* 이미 물질화된 run보다 **과거** 시각의 run을 나중에 인입하면(out-of-order) 전환 카운트는
  from-scratch 재계산과 달라질 수 있다. 셀의 최신 run 자체는 ``(executed_at, run_id)`` 최대값을
  유지하므로 뒤로 밀리지 않는다. 실행 경로(스케줄러 → execute_job_once)는 항상 "지금" 시각의
  run만 만들므로 이 경우는 발생하지 않는다.
* ``p95_duration_ms``는 배치 내부에서만 정확하다(§4 "p95 근사"). 하루치 롤업 행에 배치가 여러 번
  더해지면 저장소가 ``max(기존, 이번 배치)``로 합치며, 이는 등가성 property의 대상이 아니다.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

# failed_rule 버킷팅과 p95 계산은 집계 오라클과 한 몸이어야 한다 — 복사하지 않고 그대로 쓴다.
from .aggregation import _keys as _group_keys
from .aggregation import _p95
from .types import ApiWatermark, CellState, RunResult, RunStatus

# (api_id, target_env, test_data_label) — test_data_label 은 바인딩이 없으면 None.
# SQL 쪽에서는 NULL 대신 '' 센티널로 저장한다(Store 참조): SQLite 의 non-rowid PK 는 NULL 을
# 서로 다른 값으로 취급해서 ON CONFLICT upsert 가 영원히 안 맞기 때문이다.
CellKey = tuple[str, str, str | None]


class RollupRow(BaseModel):
    """``rollup_day`` 한 행의 **증분**. ``passed`` 필드는 SQL 의 ``pass`` 열에 대응한다 —
    ``pass`` 는 파이썬 키워드라 속성명으로 쓸 수 없어서 이름만 다르고, 매핑은 Store 의 upsert
    한 곳에서만 일어난다(별칭을 쓰지 않는다: 모델을 dict 로 덤프하는 경로가 없다)."""
    day: str
    api_id: str
    target_env: str
    test_data_label: str | None = None
    count: int = 0
    passed: int = 0
    fail: int = 0
    error: int = 0
    transitions: int = 0
    p95_duration_ms: int | None = None
    failed_rule_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def cell(self) -> CellKey:
        return (self.api_id, self.target_env, self.test_data_label)


class OperatorApiDelta(BaseModel):
    """``operator_api`` 한 행의 증분. ``executed_by`` 가 없는 run(레거시/미귀속)은 아무것도
    만들지 않는다 — 오퍼레이터 스코프는 "내가 돌린 API"이지 "주인 없는 API"가 아니다."""
    operator_id: str
    api_id: str
    last_executed_at: datetime
    run_count: int = 0


def cell_key(run: RunResult) -> CellKey:
    return (run.api_id, run.target_env, run.test_data_label)


def day_of(run: RunResult) -> str:
    """파티션 키. ``RunResult.day`` 는 Store 가 ``briefing_tz`` 로 채워서 넘긴다 — 이 모듈은
    타임존을 알지 못하므로, 비어 있으면 UTC 날짜로 물러선다(레거시 run 대비)."""
    return run.day or run.executed_at.astimezone(UTC).date().isoformat()


def _is_pass(run: RunResult) -> bool:
    return run.status is RunStatus.PASS


def rollup_delta(
    runs: Sequence[RunResult], prev_state: Mapping[CellKey, CellState]
) -> tuple[list[RollupRow], list[CellState], list[ApiWatermark], list[OperatorApiDelta]]:
    """순수 함수. 배치 하나에서 나오는 네 갈래 증분을 돌려준다(모듈 독스트링의 계약 그대로).

    네 번째 원소(``OperatorApiDelta``)는 브리프의 3-튜플 시그니처에 컨트롤러 룰링으로 덧붙인
    것이다 — 같은 순회에서 공짜로 나오는 값을 굳이 두 번 돌 이유가 없다."""
    ordered = sorted(runs, key=lambda r: (r.executed_at, r.run_id))

    rollups: dict[tuple[str, CellKey], RollupRow] = {}
    durations: dict[tuple[str, CellKey], list[int]] = {}
    latest_run: dict[CellKey, RunResult] = {}
    flips: dict[CellKey, int] = {}
    last_pass_flag: dict[CellKey, bool] = {}
    watermarks: dict[str, dict] = {}
    operators: dict[tuple[str, str], OperatorApiDelta] = {}

    for run in ordered:
        key = cell_key(run)
        day = day_of(run)
        row = rollups.get((day, key))
        if row is None:
            row = RollupRow(day=day, api_id=run.api_id, target_env=run.target_env,
                            test_data_label=run.test_data_label)
            rollups[(day, key)] = row
        row.count += 1
        if run.status is RunStatus.PASS:
            row.passed += 1
        elif run.status is RunStatus.FAIL:
            row.fail += 1
        else:
            row.error += 1
        for rule in _group_keys(run, "failed_rule"):
            row.failed_rule_counts[rule] = row.failed_rule_counts.get(rule, 0) + 1
        if run.duration_ms is not None:
            durations.setdefault((day, key), []).append(run.duration_ms)

        # 전환: 같은 셀의 "직전" 판정과 pass↔non-pass 가 뒤집히면 +1. 배치 안에서는 방금 본 run,
        # 배치의 첫 run 은 prev_state 가 씨앗이다(없으면 전환 없음 — 셀의 최초 run).
        previous = last_pass_flag.get(key)
        if previous is None:
            seed = prev_state.get(key)
            previous = None if seed is None else (seed.status is RunStatus.PASS)
        now_pass = _is_pass(run)
        if previous is not None and now_pass != previous:
            flips[key] = flips.get(key, 0) + 1
            row.transitions += 1
        last_pass_flag[key] = now_pass

        current = latest_run.get(key)
        if current is None or (run.executed_at, run.run_id) > (current.executed_at, current.run_id):
            latest_run[key] = run

        mark = watermarks.setdefault(run.api_id, {
            "last_pass_at": None, "first_non_pass_at": None,
            "last_non_pass_at": None, "latest_status": None,
        })
        if now_pass:
            mark["last_pass_at"] = run.executed_at
        else:
            if mark["first_non_pass_at"] is None:
                mark["first_non_pass_at"] = run.executed_at
            mark["last_non_pass_at"] = run.executed_at
        mark["latest_status"] = run.status

        if run.executed_by:
            op_key = (run.executed_by, run.api_id)
            delta = operators.get(op_key)
            if delta is None:
                operators[op_key] = OperatorApiDelta(
                    operator_id=run.executed_by, api_id=run.api_id,
                    last_executed_at=run.executed_at, run_count=1,
                )
            else:
                delta.run_count += 1
                delta.last_executed_at = max(delta.last_executed_at, run.executed_at)

    for (day, key), values in durations.items():
        rollups[(day, key)].p95_duration_ms = _p95(values)

    cells: list[CellState] = []
    for key, run in latest_run.items():
        seed = prev_state.get(key)
        base = seed.transitions_total if seed is not None else 0
        newest = run
        if seed is not None and (seed.executed_at, seed.run_id) > (run.executed_at, run.run_id):
            # out-of-order 인입: 셀의 최신 run 은 뒤로 밀리지 않는다(모듈 독스트링의 한계 항목).
            cells.append(seed.model_copy(update={"transitions_total": base + flips.get(key, 0)}))
            continue
        cells.append(CellState(
            api_id=newest.api_id, target_env=newest.target_env,
            test_data_label=newest.test_data_label, run_id=newest.run_id, status=newest.status,
            executed_at=newest.executed_at, transitions_total=base + flips.get(key, 0),
        ))

    marks = [
        ApiWatermark(
            api_id=api_id, last_pass_at=v["last_pass_at"], first_non_pass_at=v["first_non_pass_at"],
            last_non_pass_at=v["last_non_pass_at"], latest_status=v["latest_status"],
        )
        for api_id, v in watermarks.items()
    ]
    return list(rollups.values()), cells, marks, list(operators.values())


def merge_watermark(existing: ApiWatermark | None, batch: ApiWatermark) -> ApiWatermark:
    """저장소가 쓰는 병합 규칙(여기 둬서 Mock/REST 어댑터가 같은 규칙을 공유한다):
    ``last_pass_at``/``last_non_pass_at`` 은 max, ``first_non_pass_at`` 은 min,
    ``latest_status`` 는 **더 나중 실행 시각을 가진 쪽**의 값이다. 한 API 의 마지막 실행 시각은
    ``max(last_pass_at, last_non_pass_at)`` 이므로 별도 컬럼 없이 결정된다(동률이면 이번 배치가
    이긴다 — 나중에 인입된 쪽)."""
    if existing is None:
        return batch

    def _max(a: datetime | None, b: datetime | None) -> datetime | None:
        return b if a is None else (a if b is None else max(a, b))

    def _min(a: datetime | None, b: datetime | None) -> datetime | None:
        return b if a is None else (a if b is None else min(a, b))

    def _latest(mark: ApiWatermark) -> datetime | None:
        return _max(mark.last_pass_at, mark.last_non_pass_at)

    old_latest, new_latest = _latest(existing), _latest(batch)
    latest_status = existing.latest_status
    if new_latest is not None and (old_latest is None or new_latest >= old_latest):
        latest_status = batch.latest_status
    return ApiWatermark(
        api_id=existing.api_id,
        last_pass_at=_max(existing.last_pass_at, batch.last_pass_at),
        first_non_pass_at=_min(existing.first_non_pass_at, batch.first_non_pass_at),
        last_non_pass_at=_max(existing.last_non_pass_at, batch.last_non_pass_at),
        latest_status=latest_status,
    )
