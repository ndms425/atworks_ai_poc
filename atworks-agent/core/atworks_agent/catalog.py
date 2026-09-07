"""자가발전 질의 엔진의 고정 카탈로그 (self-growth, spec 2026-09-07 §3-4). ``DIMENSIONS``/
``MEASURES``는 데이터에서 만들지 않는다 -- ``Dimension``/``Measure`` 리터럴과 같은 값의 집합을
손으로 적은 상수다, 그래서 query_runs 툴 스키마가 config의 순함수(캐시 안정)로 남는다. 이 모듈은
숫자를 하나도 계산하지 않는다: ``title_for_spec``/``cluster_key_for_spec``은 이미 만들어진
``QuerySpec``을 사람이 읽는 문자열/정규화 키로 바꿀 뿐이다."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import get_args

from .types import Dimension, Measure, QuerySpec


@dataclass(frozen=True)
class DimInfo:
    """차원 하나의 메타데이터. ``source``는 이 차원의 값이 사는 물질화 테이블 이름
    (``rollup_day|rollup_key_day|rollup_operator_day|runs|apis``) -- 실제 SQL 소스 선택은
    host의 컴파일러가 필터·다른 차원과 함께 다시 정하지만(spec §4 표), 여기 값은 그 표의
    "기본" 열이다."""
    label_ko: str
    help_ko: str
    source: str


@dataclass(frozen=True)
class MeasureInfo:
    """측정값 하나의 메타데이터. 측정값은 어느 소스에서든 같은 이름으로 계산되므로 source가 없다."""
    label_ko: str
    help_ko: str


# 순서는 Dimension 리터럴 선언 순서와 같다 -- catalog_hint()가 그 순서로 읽는다(딕트 삽입 순서에
# 기대지 않는다).
DIMENSIONS: dict[Dimension, DimInfo] = {
    "api": DimInfo("API별", "api_id로 묶는다.", "rollup_day"),
    "path_segment_1": DimInfo(
        "경로 1조각별", "경로를 '/'로 나눈 첫 조각(1-base, 버전 조각도 포함). {id}·숫자만인 조각은 그대로 둔다.", "apis"
    ),
    "path_segment_2": DimInfo(
        "경로 2조각별", "경로를 '/'로 나눈 두 번째 조각. {id}·숫자만인 조각은 그대로 둔다.", "apis"
    ),
    "path_prefix_2": DimInfo("경로 앞 2조각별", "경로의 앞 두 조각을 합친 접두사로 묶는다.", "apis"),
    "method": DimInfo("HTTP 메서드별", "GET/POST/PUT/PATCH/DELETE로 묶는다.", "apis"),
    "api_group": DimInfo("API 그룹별", "API 스펙의 group 필드로 묶는다.", "apis"),
    "target_env": DimInfo("대상 환경별", "실행이 겨냥한 환경(dev/stg/legacy/renewed 등)으로 묶는다.", "rollup_day"),
    "test_data_label": DimInfo("테스트 데이터별", "바인딩된 TestDataSet의 라벨로 묶는다.", "rollup_day"),
    "failed_rule": DimInfo("실패 규칙별", "실행이 걸린 검증 규칙 이름으로 묶는다(키 축, 전환 근사).", "rollup_key_day"),
    "http_status": DimInfo("HTTP 상태별", "응답 상태 코드로 묶는다(키 축, 전환 근사).", "rollup_key_day"),
    "executed_by": DimInfo("실행자별", "job을 승인해 실행시킨 오퍼레이터 id로 묶는다.", "rollup_operator_day"),
    "day": DimInfo("일별", "briefing_tz 기준 로컬 날짜로 묶는다.", "rollup_day"),
    "week": DimInfo("주별", "briefing_tz 기준 로컬 ISO 주로 묶는다.", "rollup_day"),
}

MEASURES: dict[Measure, MeasureInfo] = {
    "runs": MeasureInfo("실행 수", "그룹에 속한 run의 총 개수."),
    "pass": MeasureInfo("성공 수", "status=pass인 run 수."),
    "fail": MeasureInfo("실패 수", "status=fail인 run 수."),
    "error": MeasureInfo("에러 수", "status=error인 run 수."),
    "non_pass": MeasureInfo("실패+에러 수", "fail + error."),
    "fail_rate": MeasureInfo("실패율", "(fail+error)/runs, 소수 4자리."),
    "apis": MeasureInfo("API 수", "그룹에 속한 서로 다른 api_id 개수(COUNT DISTINCT)."),
    "transitions": MeasureInfo("전환 수", "pass↔non-pass 전환 누계(롤업이 물질화 시점에 센 값)."),
    "p95_duration_ms": MeasureInfo("p95 응답시간(ms)", "셀별 p95의 최대값 병합 근사(롤업 정의 그대로)."),
}


@cache
def catalog_hint() -> str:
    """전체 카탈로그를 한 줄씩 나열한 결정론 힌트 -- 프롬프트/툴 설명에 그대로 실릴 수 있으므로
    호출마다 바이트가 같아야 한다(그래서 캐시한다). 순서는 dict 삽입 순서가 아니라
    Dimension/Measure 리터럴의 선언 순서를 그대로 쓴다."""
    lines: list[str] = []
    for name in get_args(Dimension):
        info = DIMENSIONS[name]
        lines.append(f"{name} — {info.label_ko}: {info.help_ko}")
    for name in get_args(Measure):
        info = MEASURES[name]
        lines.append(f"{name} — {info.label_ko}: {info.help_ko}")
    return "\n".join(lines)


_STATUS_LABEL_KO: dict[str, str] = {
    "all": "전체",
    "pass": "성공",
    "fail": "실패",
    "error": "에러",
    "non_pass": "실패·에러",
}


def title_for_spec(spec: QuerySpec) -> str:
    """카탈로그 라벨로 결정론 생성하는 사람이 읽는 제목(≤120자) -- 예: '실패·에러 · 경로 2조각별 ·
    30일 · 상위 20'. saved_questions.title과 query_table 카드 제목이 여기서 나온다. 숫자는
    spec 필드에서만 온다, 모델이 지어낸 문장이 아니다."""
    status = spec.filters.status or "all"
    status_part = _STATUS_LABEL_KO.get(status, "전체")

    if spec.dimensions:
        dims_part = "×".join(DIMENSIONS[d].label_ko for d in spec.dimensions)
    else:
        dims_part = "전체"

    filters = spec.filters
    if filters.window_days is not None:
        window_part = f"{filters.window_days}일"
    elif filters.since is not None or filters.until is not None:
        since_part = filters.since.date().isoformat() if filters.since else "…"
        until_part = filters.until.date().isoformat() if filters.until else "…"
        window_part = f"{since_part}~{until_part}"
    else:
        window_part = "기본 기간"

    top_part = f"상위 {spec.limit}"

    title = " · ".join((status_part, dims_part, window_part, top_part))
    if len(title) > 120:
        title = title[:119] + "…"
    return title


def cluster_key_for_spec(spec: QuerySpec) -> str:
    """값을 뺀 정규화 스펙 키 -- ask_log/saved_questions가 '같은 질문'을 판정하는 유일한 기준.
    필터 VALUES는 절대 이 문자열에 들어가지 않는다, 필터 종류(필드 이름)만 들어간다 -- 두
    ``path_contains`` 값만 다른 스펙은 같은 키를 공유한다."""
    dims_part = ",".join(sorted(spec.dimensions))
    measures_part = ",".join(sorted(spec.measures))
    filter_kinds = sorted(name for name, value in spec.filters.model_dump(exclude_none=True).items())
    filters_part = ",".join(filter_kinds)
    key = f"dims={dims_part}|measures={measures_part}|filters={filters_part}"
    if spec.compare_previous_window:
        key += "|compare"
    return key
