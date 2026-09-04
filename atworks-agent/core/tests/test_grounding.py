from commerce_common.grounding import first_forced_tool

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.grounding import GROUNDING_RULES, job_requested, matches_any_ko
from atworks_agent.types import AtworksSessionState

CFG = AtworksAgentConfig(model="m")


def test_korean_needle_matches_inside_word():
    assert matches_any_ko("최근 실패한 api들 중 risk 있는 것 가져와봐", ["실패"])
    assert not matches_any_ko("성공한 것만", ["실패"])


def test_english_needle_keeps_whole_word():
    assert matches_any_ko("show failed runs", ["fail", "failed"])
    assert not matches_any_ko("unfailing service", ["fail"])


def test_runs_rule_fires_first_for_failure_question():
    tool = first_forced_tool(GROUNDING_RULES, CFG, "최근 실패한 api들 중 risk 있는 것 가져와봐", AtworksSessionState())
    assert tool == "list_runs"


def test_queue_rule_fires_for_apply_with_nothing_seen():
    tool = first_forced_tool(GROUNDING_RULES, CFG, "아까 그 스케줄 실행 승인해줘", AtworksSessionState())
    assert tool == "get_pending_jobs"


def test_queue_rule_silent_when_job_already_seen():
    state = AtworksSessionState()
    state.seen_jobs["job-0001"] = object()  # type: ignore[assignment]
    assert first_forced_tool(GROUNDING_RULES, CFG, "승인해줘 실행", state) is None


def test_job_requested_detector():
    assert job_requested(CFG, "지난 1주일간 업데이트된 api 매일 9시에 실행해줘")
    assert not job_requested(CFG, "이 API 스펙이 뭐야?")
