# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""자가발전 회귀 러너(spec §9). 두 모드가 있고, 기본은 모델이 없는 쪽이다.

**재생 모드(기본).** 케이스가 고정한 것은 스펙의 **모양**이다 -- 차원, 측정값, 필터 종류. 러너는
그 모양에서 ``QuerySpec`` 하나를 다시 조립해 (1) 지금의 카탈로그가 그것을 받아들이는지(pydantic),
(2) 지금의 Store가 그것을 실행하는지, (3) 결과의 모양이 맞는지(행 ≤ limit, population ≥ 0, 모든
측정값 열이 있는지)를 본다. 모델도, 네트워크도, 요금도 없다 -- CI에서 매 커밋 돌 수 있고, 카탈로그에서
차원 하나를 빼는 변경을 그 자리에서 붉힌다.

필터의 **값**은 케이스에 없다(evals_writer가 종류만 남긴다). 그래서 재생은 아래 자리표시 표로 값을
채운다: 목적은 "이 필터 종류가 아직 컴파일되는가"이지 "그때 그 30일에 몇 건이었나"가 아니다.

**라이브 모드(``--live`` 또는 ``ATWORKS_EVAL_LIVE=1``).** 실제 모델로 턴을 다시 돌려 코드 그레이더로
채점한다: ``calls_tool``(그 도구를 불렀나), ``spec_equals``(차원·측정값·필터 **종류**가 같은가),
``ui_components``(그 카드가 나왔나), ``never_calls``(스테이징·승인에 손대지 않았나),
``max_tool_calls``. 모델은 비결정적이므로 실패한 케이스만 **한 번** 다시 돌린다.

    python -m evals.run_evals                  # 재생
    python -m evals.run_evals --live           # 실제 모델
    python -m evals.run_evals --case query_runs-seed-endpoint-grouping
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atworks_agent import (
    AtworksAgentConfig,
    AtworksSessionContext,
    AtworksSessionState,
    QueryFilters,
    QuerySpec,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "host" / "atworks_host" / "fixtures"
SKILLS = ROOT / "atworks-agent" / "skills"
CASES_DIR = ROOT / "evals" / "cases"

#: 필터 **종류**만 저장된 케이스를 다시 실행 가능한 스펙으로 만들기 위한 자리표시 값. 타입만 맞으면
#: 되고(카탈로그가 그 필드를 아직 받는지가 질문이다), 값이 무엇을 고르는지는 이 모드의 관심이 아니다
#: -- 그래서 fixture에 그 값이 실제로 있든 없든 결과가 0행이어도 통과다(모양만 본다).
#:
#: ===================  ==============================  =======================================
#: 필터 종류             자리표시                          왜 이 값인가
#: ===================  ==============================  =======================================
#: status               "non_pass"                      가장 흔한 트리아지 모집단
#: window_days          30                              기본 창과 같은 길이
#: since / until        now-30d / now                   window_days가 없을 때만 쓰인다(배타)
#: api_ids              ["api-001"]                     fixture의 첫 API; 없는 id여도 컴파일된다
#: path_contains        ["/v1"]                         소문자 부분일치 한 조각
#: path_prefix          "/v1"                           앞고정 한 조각
#: method               ["GET"]                         HttpMethod 리터럴의 첫 값
#: api_group            ["payment"]                     자유 문자열
#: target_env           ["dev"]                         allowed_target_envs의 첫 값
#: test_data_label      ["기본"]                         자유 문자열
#: executed_by          ["minseong"]                    fixture 오퍼레이터 (run 경로를 강제한다)
#: scope_operator       "minseong"                      같은 오퍼레이터의 API 스코프
#: failed_rule          ["rule-001"]                    키 축 롤업을 타게 하는 한 값
#: http_status          [500]                           같은 축의 정수 한 값
#: ===================  ==============================  =======================================
PLACEHOLDERS: dict[str, Any] = {
    "status": "non_pass",
    "window_days": 30,
    "since": None,      # 아래에서 now 기준으로 채운다 (datetime은 상수로 둘 수 없다)
    "until": None,
    "api_ids": ["api-001"],
    "path_contains": ["/v1"],
    "path_prefix": "/v1",
    "method": ["GET"],
    "api_group": ["payment"],
    "target_env": ["dev"],
    "test_data_label": ["기본"],
    "executed_by": ["minseong"],
    "scope_operator": "minseong",
    "failed_rule": ["rule-001"],
    "http_status": [500],
}


@dataclass
class CaseOutcome:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    skipped: bool = False
    retried: bool = False


def load_cases(cases_dir: Path = CASES_DIR, case_id: str | None = None) -> list[dict[str, Any]]:
    """``cases_dir``의 케이스들을 파일 이름 순으로. ``case_id``를 주면 그 하나만."""
    cases: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("id", path.stem)
        case["_path"] = str(path)
        if case_id is None or case["id"] == case_id:
            cases.append(case)
    return cases


def spec_from_expected(expected: dict[str, Any], *, now: datetime | None = None) -> QuerySpec:
    """``expected.spec_equals`` → 실행 가능한 ``QuerySpec``. 알 수 없는 필터 종류는 ``ValueError``다
    -- 카탈로그에서 사라진 필터를 조용히 빼고 통과시키면 이 모드가 지키려던 것이 사라진다."""
    equals = expected.get("spec_equals") or {}
    kinds = list(equals.get("filters_kinds") or [])
    unknown = [kind for kind in kinds if kind not in PLACEHOLDERS]
    if unknown:
        raise ValueError(f"no placeholder for filter kind(s) {unknown!r}")
    moment = now or datetime.now(UTC)
    values: dict[str, Any] = {}
    for kind in kinds:
        if kind == "since":
            values[kind] = moment.replace(microsecond=0) - timedelta(days=30)
        elif kind == "until":
            values[kind] = moment.replace(microsecond=0)
        else:
            values[kind] = PLACEHOLDERS[kind]
    # 배타 규칙 두 가지를 자리표시가 어기지 않게 한다. 케이스에 둘 다 들어 있을 수는 없지만
    # (QuerySpec이 애초에 막는다), 자리표시는 그 검증기를 다시 만나므로 여기서 정리한다.
    if "window_days" in values:
        values.pop("since", None)
        values.pop("until", None)
    measures = list(equals.get("measures") or [])
    if "fail_rate" in measures and values.get("status") not in (None, "all"):
        # fail_rate는 status 필터와 함께 못 쓴다(분모가 필터에 상대적이라 언제나 1.0/0.0).
        # 자리표시가 그 조합을 만들 뿐이므로 여기서 status를 중립값으로 바꾼다.
        values["status"] = "all"
    return QuerySpec(dimensions=list(equals.get("dimensions") or []), measures=measures,
                     filters=QueryFilters(**values))


# -- 재생 모드 -------------------------------------------------------------------------------

async def replay_case(case: dict[str, Any], backend, session: AtworksSessionContext) -> CaseOutcome:
    expected = case.get("expected") or {}
    outcome = CaseOutcome(case_id=case["id"], passed=True)
    try:
        spec = spec_from_expected(expected, now=session.now)
    except (ValidationError, ValueError) as error:
        outcome.passed = False
        outcome.failures.append(f"the current catalogue rejects this case's spec: {error}")
        return outcome
    result = await backend.query_runs(session, spec)
    # Every assertion below must be able to FAIL on a plausible regression. `population >= 0` and
    # `rows <= limit` were the first pass and neither could: population is a COUNT and the LIMIT
    # is in the statement, so both held on a store the engine had stopped reading correctly. What
    # replay can honestly check without a model is that the envelope and the rows AGREE.
    if len(result.rows) > spec.limit:
        outcome.failures.append(f"{len(result.rows)} rows > limit {spec.limit}")
    # `total_groups` is the count BEFORE the cut, so it can never be smaller than what came back
    # -- a source that forgot its totals statement and returned `len(rows)`'s worth of groups
    # from a limited page shows up here.
    if result.total_groups < len(result.rows):
        outcome.failures.append(
            f"total_groups {result.total_groups} < {len(result.rows)} returned rows")
    row_runs = sum(int(row.measures.get("runs") or 0) for row in result.rows)
    if "runs" in spec.measures and result.population < row_runs:
        # The population is the whole filtered set; the returned page is a subset of it. A
        # window/predicate mismatch between the ranked statement and the totals statement (the
        # two are built separately) lands exactly here.
        outcome.failures.append(f"population {result.population} < rows' own runs {row_runs}")
    for index, row in enumerate(result.rows):
        missing = [m for m in spec.measures if m not in row.measures]
        if missing:
            outcome.failures.append(f"row {index} is missing measure column(s) {missing!r}")
            break
    # A case whose spec matches NOTHING is not a passing regression test -- it is a case that
    # stopped testing anything, which is what a renamed dimension key or an emptied fixture store
    # looks like from here. Said out loud rather than counted as green.
    if not result.rows and result.population == 0:
        outcome.failures.append(
            "the current store answers this spec with zero rows AND zero population -- "
            "the case no longer exercises anything (renamed key? empty fixtures?)")
    outcome.passed = not outcome.failures
    return outcome


# -- 라이브 모드 -----------------------------------------------------------------------------

def _live_config() -> AtworksAgentConfig:
    return AtworksAgentConfig(model=os.environ.get("ATWORKS_MODEL", "claude-sonnet-4-5"))


def build_live_agent(config: AtworksAgentConfig, backend):
    """main.py의 부팅과 같은 순서: ``.env`` → 빈 자격증명 제거 → (선택) OS 신뢰 저장소 →
    실제 클라이언트를 가진 ``AtworksAgent``. 여기서 FakeClient는 쓰지 않는다 -- 라이브 모드의
    존재 이유가 진짜 모델의 판단이다."""
    from dotenv import load_dotenv

    from atworks_agent_runtime import AtworksAgent

    load_dotenv(ROOT / ".env", override=True)
    for cred in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(cred) == "":
            del os.environ[cred]
    if os.environ.get("ATWORKS_TRUST_OS_CA", "1") != "0":
        import truststore

        truststore.inject_into_ssl()
    return AtworksAgent(backend=backend, skills_dir=SKILLS, config=config)


@dataclass
class TurnTrace:
    """한 케이스를 돌려 나온 이벤트에서 채점에 필요한 것만."""
    tool_names: list[str] = field(default_factory=list)
    tool_inputs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    error: str | None = None


async def run_live_turns(agent, case: dict[str, Any], backend) -> TurnTrace:
    state = AtworksSessionState()
    stored = case.get("state") or {}
    session = AtworksSessionContext(
        session_id=f"eval-{case['id']}", project_id="mes-demo",
        operator=stored.get("operator") or "minseong", role=stored.get("role"),
        now=datetime.now().astimezone(),
    )
    trace = TurnTrace()
    messages: list[dict[str, Any]] = []
    for turn in case.get("turns") or []:
        messages.append({"role": "user", "content": turn})
        try:
            async for event in agent.stream_turn(messages, session, state):
                if event.type == "tool_call":
                    trace.tool_names.append(event.data["tool"])
                    trace.tool_inputs.append((event.data["tool"], event.data.get("input") or {}))
                elif event.type == "ui":
                    trace.components.append(event.data["component"])
        except Exception as error:                      # noqa: BLE001 - 채점 결과로 옮긴다
            trace.error = f"{type(error).__name__}: {error}"
            break
    del backend
    return trace


def grade(case: dict[str, Any], trace: TurnTrace) -> list[str]:
    """코드 그레이더 다섯 개. 판정은 전부 이벤트에서 나온다 -- 모델의 문장을 읽지 않는다."""
    expected = case.get("expected") or {}
    failures: list[str] = []
    if trace.error:
        return [f"the turn raised: {trace.error}"]
    wanted = expected.get("calls_tool")
    for name in [wanted] if isinstance(wanted, str) else (wanted or []):
        if name not in trace.tool_names:
            failures.append(f"never called {name!r} (called {trace.tool_names!r})")
    for name in expected.get("never_calls") or []:
        if name in trace.tool_names:
            failures.append(f"called the forbidden tool {name!r}")
    for component in expected.get("ui_components") or []:
        if component not in trace.components:
            failures.append(f"no {component!r} card (drew {trace.components!r})")
    cap = expected.get("max_tool_calls")
    if cap is not None and len(trace.tool_names) > cap:
        failures.append(f"{len(trace.tool_names)} tool calls > max {cap}")
    equals = expected.get("spec_equals")
    if equals:
        calls = [args for name, args in trace.tool_inputs if name == "query_runs"]
        if not calls:
            failures.append("no query_runs call to compare the spec against")
        else:
            actual = calls[-1]
            got_dims = list(actual.get("dimensions") or [])
            got_measures = list(actual.get("measures") or [])
            got_kinds = sorted(actual.get("filters") or {})
            if got_dims != list(equals.get("dimensions") or []):
                failures.append(f"dimensions {got_dims!r} != {equals.get('dimensions')!r}")
            if sorted(got_measures) != sorted(equals.get("measures") or []):
                failures.append(f"measures {got_measures!r} != {equals.get('measures')!r}")
            if got_kinds != sorted(equals.get("filters_kinds") or []):
                failures.append(f"filter kinds {got_kinds!r} != {sorted(equals.get('filters_kinds') or [])!r}")
    return failures


async def live_case(agent, case: dict[str, Any], backend) -> CaseOutcome:
    failures = grade(case, await run_live_turns(agent, case, backend))
    retried = False
    if failures:
        # 모델은 비결정적이다 -- 한 번의 실패가 회귀라는 증거는 아니다. 딱 한 번 다시 돌린다.
        retried = True
        failures = grade(case, await run_live_turns(agent, case, backend))
    return CaseOutcome(case_id=case["id"], passed=not failures, failures=failures, retried=retried)


# -- CLI -------------------------------------------------------------------------------------

def _backend(config: AtworksAgentConfig):
    from atworks_host.mock_backend import MockAtworks

    return MockAtworks(config, FIXTURES)


async def run(cases: list[dict[str, Any]], *, live: bool) -> list[CaseOutcome]:
    config = _live_config() if live else AtworksAgentConfig(model="replay")
    backend = _backend(config)
    outcomes: list[CaseOutcome] = []
    agent = build_live_agent(config, backend) if live else None
    for case in cases:
        if case.get("skip"):
            outcomes.append(CaseOutcome(case_id=case["id"], passed=True, skipped=True,
                                        failures=[str(case["skip"])]))
            continue
        if live:
            outcomes.append(await live_case(agent, case, backend))
        else:
            session = AtworksSessionContext(session_id=f"eval-{case['id']}", project_id="mes-demo",
                                            operator="minseong", now=datetime.now().astimezone())
            outcomes.append(await replay_case(case, backend, session))
    return outcomes


def report(outcomes: list[CaseOutcome], *, live: bool) -> str:
    mode = "live" if live else "replay"
    lines = [f"{'case':<44} {'verdict':<8} note", "-" * 92]
    for outcome in outcomes:
        verdict = "SKIP" if outcome.skipped else ("PASS" if outcome.passed else "FAIL")
        note = "; ".join(outcome.failures)
        if outcome.retried and outcome.passed:
            note = "passed on retry"
        lines.append(f"{outcome.case_id:<44} {verdict:<8} {note}")
    failed = sum(1 for o in outcomes if not o.passed and not o.skipped)
    # SKIP is on the tally line, not only in the rows: "12 case(s), 0 failed" reads as a green
    # suite whether one case is skipped or eleven are, and a suite that quietly stopped running
    # is the failure mode this whole file exists to catch.
    skipped = sum(1 for o in outcomes if o.skipped)
    lines.append("-" * 92)
    lines.append(f"{mode}: {len(outcomes)} case(s), {failed} failed, {skipped} skipped")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="자가발전 회귀 스위트 (self-growth spec §9)")
    parser.add_argument("--live", action="store_true",
                        help="실제 모델로 턴을 재생해 채점한다 (ATWORKS_EVAL_LIVE=1과 같다)")
    parser.add_argument("--case", default=None, help="이 id의 케이스 하나만")
    parser.add_argument("--cases-dir", default=str(CASES_DIR))
    args = parser.parse_args(argv)
    live = args.live or os.environ.get("ATWORKS_EVAL_LIVE") == "1"
    cases = load_cases(Path(args.cases_dir), args.case)
    if not cases:
        print(f"no cases in {args.cases_dir}", file=sys.stderr)
        return 1
    outcomes = asyncio.run(run(cases, live=live))
    print(report(outcomes, live=live))
    return 1 if any(not o.passed and not o.skipped for o in outcomes) else 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
