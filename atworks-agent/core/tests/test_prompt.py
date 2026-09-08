from datetime import datetime

from commerce_common.skills import Skill, SkillRegistry

from atworks_agent.catalog import catalog_hint
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.prompt import build_dynamic_context, build_static_system
from atworks_agent.types import AttachedItem, QueryFilters, VocabularyEntry

SKILLS = SkillRegistry([Skill(name="failed-triage", description="실패 triage", body="...")])


def test_static_prompt_is_byte_stable_and_leads_with_safety():
    cfg = AtworksAgentConfig(model="m")
    a, b = build_static_system(cfg, SKILLS), build_static_system(cfg, SKILLS)
    assert a == b
    assert a.index("# Hard lines") < a.index("# How you work") < a.index("# Skills")
    assert "never decide pass or fail" in a
    assert "population" in a
    assert "failed-triage" in a


def test_static_prompt_carries_the_query_catalogue_and_drops_it_when_switched_off():
    cfg = AtworksAgentConfig(model="m", enable_query_runs=True)
    on = build_static_system(cfg, SKILLS)
    assert on == build_static_system(cfg, SKILLS)  # 카탈로그가 붙어도 바이트 안정
    assert "# Query catalogue" in on
    assert catalog_hint() in on                    # 결정론 바이트 그대로
    assert "query_runs" in on and "present_query_table" in on and "note_unmet_ask" in on
    assert on.index("# Hard lines") < on.index("# Query catalogue") < on.index("# How you work")

    off = build_static_system(AtworksAgentConfig(model="m", enable_query_runs=False), SKILLS)
    assert "# Query catalogue" not in off
    assert "query_runs" not in off and "note_unmet_ask" not in off and "path_segment_2" not in off
    # 꺼졌을 때의 바이트는 이 기능 이전과 같아야 한다 (섹션 전체가 사라진다).
    assert off == on.replace(on[on.index("\n\n# Query catalogue"):on.index("\n\n# How you work")], "")


def test_static_prompt_drops_job_rules_when_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_jobs=False), SKILLS)
    assert "stage_job" not in text and "apply_job" not in text and "does not run or schedule" in text


def test_static_prompt_carries_parity_hard_line_when_enabled():
    text = build_static_system(AtworksAgentConfig(model="m", enable_parity=True), SKILLS)
    assert "Value equivalence is judged by the comparison engine, never by you" in text


def test_static_prompt_drops_parity_hard_line_when_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_parity=False), SKILLS)
    assert "Value equivalence is judged by the comparison engine" not in text


def test_dynamic_context_carries_attachments_and_clock():
    item = AttachedItem(order=1, kind="run", ref_id="run-17", label="POST /x", comment="왜 실패?")
    text = build_dynamic_context(atworks_context={"project": "MES"}, attached_items=[item],
                                 now=datetime(2026, 9, 3, 14, 27))
    assert text.startswith("# aTworks context")
    assert "<atworks_data>" in text and '"project": "MES"' in text
    assert "<attached-result-items>" in text and "run-17" in text
    assert "2026-09-03T14:00" in text  # 시 단위 시계 (캐시 안정)


def test_dynamic_context_without_attachments_has_no_block():
    assert "<attached-result-items>" not in build_dynamic_context(atworks_context=None, attached_items=[], now=None)


def test_dynamic_context_carries_matched_vocabulary_and_nothing_when_there_is_none():
    # self-growth §7 step 3: the confirmed terms this message actually used, with the meaning the
    # host derived from catalogue labels and the fragment the model is expected to reuse.
    entry = VocabularyEntry(term="결제 계열", fragment=QueryFilters(path_prefix="/v1/payment"),
                            status="confirmed", proposed_by="minseong",
                            proposed_at=datetime(2026, 9, 8))
    text = build_dynamic_context(atworks_context=None, attached_items=[], vocabulary=[entry])
    assert '"term": "결제 계열"' in text and '"means": "경로 접두사 /v1/payment"' in text
    assert '"path_prefix": "/v1/payment"' in text
    # Byte stability: no matched term, no key at all -- an ordinary turn's context is unchanged.
    assert build_dynamic_context(atworks_context=None, attached_items=[], vocabulary=[]) ==            build_dynamic_context(atworks_context=None, attached_items=[])
    assert "vocabulary" not in build_dynamic_context(atworks_context=None, attached_items=[])


def test_dynamic_context_renders_operator_line_only_when_present():
    without = build_dynamic_context(atworks_context={"project": "p"}, attached_items=[])
    assert "operator_line" not in without and "APIs you ran" not in without

    with_role = build_dynamic_context(
        atworks_context={
            "project": "p", "operator": "minseong", "operator_role": "developer",
            "scope_api_ids": ["api-001", "api-002"],
        },
        attached_items=[],
    )
    assert "operator: minseong (developer)" in with_role
    assert "scope: 2 APIs you ran" in with_role


def test_dynamic_context_operator_line_uses_the_true_scope_count_not_the_capped_sample():
    # scope_api_ids is a bounded (<=20) sample; scope_api_count is the true count, and the
    # operator_line must show that, not len(scope_api_ids) (FIX #2).
    rendered = build_dynamic_context(
        atworks_context={
            "project": "p", "operator": "jihoon", "operator_role": "qa",
            "scope_api_ids": [f"api-{i:03d}" for i in range(20)],
            "scope_api_count": 25,
        },
        attached_items=[],
    )
    assert "scope: 25 APIs you ran" in rendered


def test_dynamic_context_byte_stable_without_operator_role():
    base = build_dynamic_context(atworks_context={"project": "p"}, attached_items=[])
    same = build_dynamic_context(atworks_context={"project": "p"}, attached_items=[])
    assert base == same


def test_static_prompt_states_the_matrix_contract():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "several target environments, several schedules and several test-data sets" in text
    assert "approval covers the whole matrix" in text
    assert "never facts about the system" in text
    assert "Never default target_envs to anything but [dev]" in text


def test_static_prompt_forbids_computing_group_figures():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "aggregate_runs" in text and text.index("aggregate_runs") < text.index("# How you work")


def test_static_prompt_carries_the_rule_hard_line_before_how_you_work():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    hard_line = ("You draft validation rules as structured objects; you never judge a run against a rule "
                 "and you never change a past result. A rule applies only after a person approves it on "
                 "the Rules page, and only to runs executed after that.")
    assert hard_line in text
    assert text.index(hard_line) < text.index("# How you work")


def test_static_prompt_states_the_rule_contract():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "stage_rule" in text and "apply_rule" in text
    assert "param must come from get_api" in text
    assert "prefer a named format over a raw pattern" in text


def test_static_prompt_drops_rule_rules_when_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_rules=False), SKILLS)
    assert "stage_rule" not in text and "apply_rule" not in text
    assert "You draft validation rules as structured objects" not in text


def test_static_prompt_carries_the_format_example_reuse_bulk_hard_line():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    hard_line = ("When you author a format pattern you MUST supply pass and fail examples; the system "
                 "verifies the pattern against them before approval. Reuse a saved format by name instead "
                 "of re-authoring it. Bulk-add formats with stage_format_batch; duplicates are skipped.")
    assert hard_line in text
    assert text.index(hard_line) < text.index("# How you work")


def test_static_prompt_drops_format_hard_line_when_rules_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_rules=False), SKILLS)
    assert "Bulk-add formats with stage_format_batch" not in text


def test_static_prompt_carries_screen_hard_line_when_enabled():
    text = build_static_system(AtworksAgentConfig(model="m", enable_screen_directives=True), SKILLS)
    assert "highlight_screen" in text and "①②③" in text


def test_static_prompt_drops_screen_hard_line_when_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_screen_directives=False), SKILLS)
    assert "①②③" not in text


def test_dynamic_context_carries_screen_state_and_is_byte_stable_without_it():
    from atworks_agent import ScreenState, ScreenTarget
    from atworks_agent.prompt import build_dynamic_context
    base = build_dynamic_context(atworks_context={"project": "p"}, attached_items=[])
    assert "<screen-state>" not in base
    withs = build_dynamic_context(atworks_context={"project": "p"}, attached_items=[],
                                  screen_state=ScreenState(view="apis", visible=[ScreenTarget(kind="api", ref_id="api-001")]))
    assert withs.startswith(base) and "api:api-001" in withs
