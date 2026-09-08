"""자가발전 회귀 스위트(spec §9) — `pytest -m evals`로만 돈다(기본 스위트에서 제외).

재생 모드에는 모델이 없다: 케이스가 고정한 스펙의 **모양**이 지금의 카탈로그에서 아직 유효하고
지금의 Store에서 아직 실행되는지만 본다. 그래서 이 파일이 증명해야 하는 것은 둘이다 — 커밋된
케이스들이 통과한다는 것, 그리고 카탈로그에서 차원 하나를 빼면 **정말로 붉어진다**는 것. 둘째가
없으면 첫째는 아무것도 지키지 못한다(항상 통과하는 스위트).

라이브 모드는 실제 모델을 부른다 — `ATWORKS_EVAL_LIVE=1`일 때만 돈다.
"""
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

import pytest
from evals import run_evals
from pydantic import Field

from atworks_agent import (
    AtworksAgentConfig,
    AtworksSessionContext,
    Dimension,
    QueryFilters,
    QuerySpec,
)
from atworks_host.evals_writer import write_case
from atworks_host.mock_backend import MockAtworks

pytestmark = pytest.mark.evals

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "host" / "atworks_host" / "fixtures"
CASES = ROOT / "evals" / "cases"
LIVE = os.environ.get("ATWORKS_EVAL_LIVE") == "1"


def _session() -> AtworksSessionContext:
    return AtworksSessionContext(session_id="eval", project_id="mes-demo", operator="minseong",
                                 now=datetime.now(UTC))


def _backend() -> MockAtworks:
    return MockAtworks(AtworksAgentConfig(model="replay"), FIXTURES)


# -- 자리표시 표 ------------------------------------------------------------------------------

def test_every_filter_kind_has_a_placeholder():
    """QueryFilters에 필드가 늘면 이 표도 늘어야 한다 — 아니면 그 필터를 쓴 케이스는 재생될 수
    없고, "자리표시가 없다"는 실패가 회귀처럼 보인다."""
    assert set(run_evals.PLACEHOLDERS) == set(QueryFilters.model_fields)


def test_an_unknown_filter_kind_is_refused_not_dropped():
    with pytest.raises(ValueError, match="no placeholder"):
        run_evals.spec_from_expected(
            {"spec_equals": {"dimensions": [], "measures": ["runs"], "filters_kinds": ["nope"]}})


def test_window_days_wins_over_since_until_in_the_placeholders():
    # QuerySpec이 배타로 막는 조합이라, 자리표시가 둘 다 채우면 재생이 제 자리표시 때문에 실패한다.
    spec = run_evals.spec_from_expected({"spec_equals": {
        "dimensions": [], "measures": ["runs"],
        "filters_kinds": ["since", "until", "window_days"]}})
    assert spec.filters.window_days == 30 and spec.filters.since is None


def test_fail_rate_gets_a_neutral_status_placeholder():
    spec = run_evals.spec_from_expected({"spec_equals": {
        "dimensions": [], "measures": ["fail_rate"], "filters_kinds": ["status", "window_days"]}})
    assert spec.filters.status == "all"


# -- 재생 -------------------------------------------------------------------------------------

def test_the_committed_cases_include_the_seed():
    ids = [case["id"] for case in run_evals.load_cases(CASES)]
    assert "query_runs-seed-endpoint-grouping" in ids


async def test_every_committed_case_replays_green():
    cases = run_evals.load_cases(CASES)
    assert cases, "evals/cases is empty — the seed case must be committed"
    outcomes = await run_evals.run(cases, live=False)
    failed = [(o.case_id, o.failures) for o in outcomes if not o.passed and not o.skipped]
    assert failed == []


async def test_a_case_generated_from_an_upvote_replays_green(tmp_path):
    """👍 → 파일 → 재생. 이 두 조각이 같은 모양을 읽지 않으면 생성된 케이스는 태어나자마자 붉다."""
    from atworks_agent import AskEntry

    entry = AskEntry(
        at=datetime(2026, 9, 8, 9, tzinfo=UTC), session_id="s", operator="minseong", role="qa",
        question="method별 실패율", intent="aggregate",
        spec=QuerySpec(dimensions=["method"], measures=["runs", "fail_rate"],
                       filters=QueryFilters(window_days=14)),
        outcome="answered", wanted=None, tool_calls=2, cards=1, cluster_key="c", turn_id="t-gen",
    )
    write_case(entry, cases_dir=tmp_path)
    outcomes = await run_evals.run(run_evals.load_cases(tmp_path), live=False)
    assert [o.failures for o in outcomes] == [[]]


async def test_removing_a_dimension_from_the_catalogue_makes_the_seed_fail(monkeypatch):
    """가드의 증명. 카탈로그에서 `path_segment_2`를 빼면 시드 케이스의 재생이 **실패해야** 한다 --
    통과하면 이 스위트는 카탈로그를 깨는 변경을 하나도 잡지 못한다는 뜻이다."""
    narrowed = Literal[tuple(d for d in get_args(Dimension) if d != "path_segment_2")]

    class NarrowedSpec(QuerySpec):
        dimensions: list[narrowed] = Field(default_factory=list, max_length=2)

    monkeypatch.setattr(run_evals, "QuerySpec", NarrowedSpec)
    seed = run_evals.load_cases(CASES, "query_runs-seed-endpoint-grouping")
    assert len(seed) == 1
    outcome = await run_evals.replay_case(seed[0], _backend(), _session())
    assert outcome.passed is False
    assert "rejects this case's spec" in outcome.failures[0]


async def test_a_skipped_case_is_not_a_failure(tmp_path):
    (tmp_path / "skipped.json").write_text(
        '{"id": "x", "skip": "waiting on a dimension", "turns": [], "expected": {}}',
        encoding="utf-8", newline="\n")
    outcomes = await run_evals.run(run_evals.load_cases(tmp_path), live=False)
    assert outcomes[0].skipped is True and outcomes[0].passed is True


def test_the_report_names_the_mode_and_the_failure_count():
    text = run_evals.report(
        [run_evals.CaseOutcome("a", True), run_evals.CaseOutcome("b", False, ["nope"])], live=False)
    assert "replay: 2 case(s), 1 failed" in text and "nope" in text


# -- 코드 그레이더 (라이브 모드가 쓰는 판정, 모델 없이) ------------------------------------------

def _case(**expected) -> dict:
    return {"id": "c", "turns": ["q"], "expected": expected}


def test_grade_catches_a_missing_tool_a_missing_card_and_a_forbidden_call():
    trace = run_evals.TurnTrace(tool_names=["stage_job"], components=[])
    failures = run_evals.grade(
        _case(calls_tool="query_runs", ui_components=["query_table"],
              never_calls=["stage_job", "apply_job"]), trace)
    assert any("never called 'query_runs'" in f for f in failures)
    assert any("forbidden tool 'stage_job'" in f for f in failures)
    assert any("query_table" in f for f in failures)


def test_grade_compares_dimensions_measures_and_filter_kinds_only():
    trace = run_evals.TurnTrace(
        tool_names=["query_runs"], components=["query_table"],
        tool_inputs=[("query_runs", {"dimensions": ["path_segment_2"],
                                     "measures": ["runs", "non_pass"],
                                     # 값은 채점하지 않는다 -- 종류만 본다.
                                     "filters": {"window_days": 7, "status": "non_pass"}})])
    case = _case(calls_tool="query_runs", ui_components=["query_table"],
                 spec_equals={"dimensions": ["path_segment_2"],
                              "measures": ["non_pass", "runs"],
                              "filters_kinds": ["status", "window_days"]})
    assert run_evals.grade(case, trace) == []
    case["expected"]["spec_equals"]["dimensions"] = ["api"]
    assert run_evals.grade(case, trace)


def test_grade_counts_tool_calls_against_the_cap():
    trace = run_evals.TurnTrace(tool_names=["a", "b", "c"], components=[])
    assert run_evals.grade(_case(max_tool_calls=2), trace)
    assert run_evals.grade(_case(max_tool_calls=3), trace) == []


def test_a_turn_that_raised_is_one_failure_not_five():
    trace = run_evals.TurnTrace(error="RuntimeError: boom")
    assert run_evals.grade(_case(calls_tool="query_runs"), trace) == ["the turn raised: RuntimeError: boom"]


# -- 라이브 -----------------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="live mode calls the real model; set ATWORKS_EVAL_LIVE=1")
async def test_the_committed_cases_pass_against_the_real_model():
    outcomes = await run_evals.run(run_evals.load_cases(CASES), live=True)
    failed = [(o.case_id, o.failures) for o in outcomes if not o.passed and not o.skipped]
    assert failed == []
