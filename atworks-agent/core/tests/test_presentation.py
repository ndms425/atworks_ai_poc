from datetime import UTC, datetime

import pytest
from commerce_common.presentation import EnrichmentContext, PresentationRefused, run_presentation

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.enrichment import (
    PRESENTATION_COMPONENTS,
    enrich_highlight_screen,
    enrich_navigate_screen,
)
from atworks_agent.gates import PROVENANCE_GATE
from atworks_agent.rules import RuleImpact, ValidationRule
from atworks_agent.tools.presentation import (
    HighlightScreenPayload,
    HighlightTarget,
    NavigateScreenPayload,
)
from atworks_agent.types import (
    ApiSpec,
    AtworksSessionContext,
    AtworksSessionState,
    ComparisonProfile,
    FailedRank,
    FormatBatch,
    FormatBatchEntry,
    JobKind,
    JobSpec,
    RunGroup,
    RunResult,
    RunStatus,
    ScreenFilter,
    ScreenState,
    ScreenTarget,
)

CFG = AtworksAgentConfig(model="m")
SESSION = AtworksSessionContext(session_id="s", project_id="p", operator="op")


def _ctx(state):
    return EnrichmentContext(backend=None, config=CFG, session=SESSION, state=state)


def _state_with_run():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="POST", path="/v1/contracts", name="계약 생성", updated_at=datetime.now(UTC)))
    state.remember_run(RunResult(run_id="run-17", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.FAIL, failed_rules=["amount >= 0"], http_status=200))
    state.remember_rank(FailedRank(run_id="run-17", api_id="api-1", scorer="risk_v1", score=4.0, reasons=["1 rule failed"]))
    state.last_population = 47
    state.last_listed_run_ids = ["run-17"]
    return state


async def test_run_digest_joins_run_and_population():
    state = _state_with_run()
    state.last_listed_filter = "fail"
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"title": "먼저 볼 실패", "items": [{"kind": "fail", "ref_id": "run-17", "headline": "amount 음수", "why_it_matters": "계약 금액 규칙 위반"}]},
        _ctx(state), "Shown.",
    )
    ui = outcome.events[0]
    assert ui.data["component"] == "run_digest"
    payload = ui.data["payload"]
    assert payload["population"] == 47 and payload["scorer"] == "risk_v1"
    assert payload["population_filter"] == "fail"
    item = payload["items"][0]
    assert item["run"]["status"] == "fail" and item["api"]["path"] == "/v1/contracts"
    assert item["rank"]["score"] == 4.0


async def test_run_digest_drops_unknown_ref_and_reports():
    state = _state_with_run()
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-999", "headline": "x"}, {"kind": "fail", "ref_id": "run-17", "headline": "y"}]},
        _ctx(state), "Shown.",
    )
    assert len(outcome.events[0].data["payload"]["items"]) == 1
    assert "run-999" in outcome.result_text


async def test_run_digest_drops_run_outside_window():
    state = _state_with_run()
    state.remember_run(RunResult(run_id="run-18", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.FAIL, http_status=200))
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-17", "headline": "y"}, {"kind": "fail", "ref_id": "run-18", "headline": "z"}]},
        _ctx(state), "Shown.",
    )
    assert len(outcome.events[0].data["payload"]["items"]) == 1
    assert "run-18" in outcome.result_text


async def test_run_digest_uses_run_status_and_drops_pass():
    state = _state_with_run()
    state.remember_run(RunResult(run_id="run-20", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.ERROR, http_status=500))
    state.remember_run(RunResult(run_id="run-21", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.PASS, http_status=200))
    state.last_listed_run_ids = ["run-17", "run-20", "run-21"]
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [
            {"kind": "fail", "ref_id": "run-20", "headline": "x"},
            {"kind": "fail", "ref_id": "run-21", "headline": "y"},
        ]},
        _ctx(state), "Shown.",
    )
    items = outcome.events[0].data["payload"]["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "error"
    assert "run-21" in outcome.result_text


async def test_run_digest_refused_without_population():
    state = _state_with_run()
    state.last_population = None
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-17", "headline": "y"}]}, _ctx(state), "Shown.",
    )
    assert outcome.is_error and "list_runs" in outcome.result_text


async def test_job_preview_joins_staged_record():
    state = AtworksSessionState()
    state.remember_job(JobSpec(job_id="job-0001", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_envs=["dev"],
                               confidence={"target_envs": 0.3}, assumptions=["target_env defaulted to dev"],
                               created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0001", "headline": "h"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["job"]["job_id"] == "job-0001" and payload["job"]["confidence"]["target_envs"] == 0.3
    assert payload["low_confidence"] == ["target_envs"]


async def test_job_preview_job_record_carries_change_id_for_the_card_buttons():
    # The card hands payload.job to web-shared's useChangeActions, which posts to
    # /changes/{change.change_id}/apply — without the alias the click went to /changes/undefined/apply.
    state = AtworksSessionState()
    state.remember_job(JobSpec(job_id="job-0001", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_envs=["dev"],
                               created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0001"}, _ctx(state), "Shown.")
    job = outcome.events[0].data["payload"]["job"]
    assert job["change_id"] == "job-0001"
    assert job["runs_total"] == 1 and job["total_executions"] == 1   # same record shape as GET /jobs


async def test_job_preview_refuses_unknown_job():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "nope"}, _ctx(AtworksSessionState()), "Shown.")
    assert outcome.blocked == "provenance"


async def test_question_form_marks_low_confidence():
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_question_form"],
        {"id": "job-slots", "title": "확인", "questions": [
            {"id": "target_env", "label": "어느 계?", "type": "radio", "why": "발화에 없음", "default": "dev", "options": ["dev", "stg"], "confidence": 0.3}]},
        _ctx(AtworksSessionState()), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    assert payload["questions"][0]["highlight"] is True


async def test_job_preview_carries_the_matrix_block():
    from atworks_agent.types import JobSchedule, TestDataSet

    state = AtworksSessionState()
    state.remember_job(JobSpec(
        job_id="job-0002", kind=JobKind.SCHEDULED_RUN, summary="s", api_ids=["api-1", "api-2"],
        target_envs=["dev", "stg"],
        schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3)],
        test_data=[TestDataSet(label="S1 정상", values={"amount": "1000"})],
        created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"],
                                     {"job_id": "job-0002"}, _ctx(state), "Shown.")
    assert outcome.events[0].data["payload"]["matrix"] == {
        "apis": 2, "envs": ["dev", "stg"], "data_sets": ["S1 정상"],
        "executions": 3, "runs_per_execution": 4, "runs_total": 12,
        "max_runs_per_execution": 4, "max_runs_total": 12,
    }


async def test_job_preview_matrix_ceiling_for_late_binding_is_the_deployment_cap():
    from atworks_agent.types import Binding, JobSchedule

    config = AtworksAgentConfig(model="m", max_matrix_size=400)
    state = AtworksSessionState()
    state.remember_job(JobSpec(
        job_id="job-0003", kind=JobKind.SCHEDULED_RUN, summary="s", api_ids=["api-1", "api-2"],
        target_envs=["dev", "stg"], binding=Binding.LATE, select_where={"query": "x"},
        schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3)],
        created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0003"},
        EnrichmentContext(backend=None, config=config, session=SESSION, state=state), "Shown.",
    )
    matrix = outcome.events[0].data["payload"]["matrix"]
    assert matrix["max_runs_per_execution"] == 400
    assert matrix["max_runs_total"] == 400 * 3


async def test_job_preview_matrix_ceiling_for_frozen_binding_equals_the_staged_figures():
    state = AtworksSessionState()
    state.remember_job(JobSpec(
        job_id="job-0004", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_envs=["dev"],
        created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"],
                                     {"job_id": "job-0004"}, _ctx(state), "Shown.")
    matrix = outcome.events[0].data["payload"]["matrix"]
    assert matrix["max_runs_per_execution"] == matrix["runs_per_execution"]
    assert matrix["max_runs_total"] == matrix["runs_total"]


def _rule(**overrides):
    fields = {
        "rule_id": "rule-0001", "api_id": "api-1", "param": "amount", "kind": "compare",
        "op": ">=", "value": "0", "message": "amount >= 0",
        "confidence": {"value": 0.3}, "assumptions": ["value defaulted to 0"],
        "created_at": datetime.now(UTC), "created_by": "op",
    }
    fields.update(overrides)
    return ValidationRule(**fields)


async def test_rule_preview_joins_staged_record_and_impact():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="POST", path="/v1/refunds", name="환불", updated_at=datetime.now(UTC)))
    state.remember_rule(_rule())
    state.rule_impacts["rule-0001"] = RuleImpact(window_runs=10, known_inputs=8, would_fail=2, excluded_unknown=2)
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001", "headline": "h"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["rule"]["rule_id"] == "rule-0001" and payload["rule"]["op"] == ">="
    assert payload["change_id"] == "rule-0001"
    assert payload["review_required"] is False
    assert payload["low_confidence"] == ["value"]
    assert payload["api"]["api_id"] == "api-1"
    assert payload["impact"] == {"window_runs": 10, "known_inputs": 8, "would_fail": 2, "excluded_unknown": 2}


async def test_rule_preview_omits_api_and_impact_when_unknown():
    state = AtworksSessionState()
    state.remember_rule(_rule())
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert "api" not in payload and "impact" not in payload


async def test_rule_preview_format_hint_for_named_format():
    from atworks_agent.rules import NAMED_FORMATS

    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, format="email", message="email matches email"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["format_hint"] == {"label": "email", "example": "user@example.com", "pattern": NAMED_FORMATS["email"]}


async def test_rule_preview_format_hint_for_raw_pattern():
    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, pattern="^[A-Z]{3}$", message="code matches /^[A-Z]{3}$/"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["format_hint"] == {"label": "정규식", "example": None, "pattern": "^[A-Z]{3}$"}


async def test_rule_preview_format_hint_includes_examples_for_raw_pattern():
    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, pattern="^[A-Z]{3}$",
                              pass_examples=["ABC"], fail_examples=["ab1"],
                              message="code matches /^[A-Z]{3}$/"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["format_hint"] == {
        "label": "정규식", "example": None, "pattern": "^[A-Z]{3}$",
        "pass_examples": ["ABC"], "fail_examples": ["ab1"],
    }


async def test_rule_preview_review_required_true_for_raw_pattern_false_for_library_resolved():
    # Fix round 2 ruling: the card must not warn "직접 검토 필요: 정규식" for a rule that
    # resolved a trusted, already-verified library format (format_name set) -- only a fresh,
    # unvetted raw pattern should carry review_required.
    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, pattern="^[A-Z]{3}$",
                              review_required=True, message="code matches /^[A-Z]{3}$/"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    assert outcome.events[0].data["payload"]["review_required"] is True

    state2 = AtworksSessionState()
    state2.remember_rule(_rule(kind="format", op=None, value=None, pattern=r"^\d{3}-\d{4}$",
                               format_name="phone-digits", review_required=False,
                               message="phone matches /^\\d{3}-\\d{4}$/"))
    outcome2 = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state2), "Shown.")
    assert outcome2.events[0].data["payload"]["review_required"] is False


async def test_rule_preview_refuses_unknown_rule():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "nope"}, _ctx(AtworksSessionState()), "Shown.")
    assert outcome.blocked == "provenance"


def _state_with_groups():
    state = AtworksSessionState()
    g1 = RunGroup(key="amount <= limit", label="amount <= limit", count=3, fail=3, error=0, passed=0, run_ids=["run-1"])
    g2 = RunGroup(key="(error) HTTP 503", label="(error) HTTP 503", count=1, fail=0, error=1, passed=0, run_ids=["run-5"])
    state.remember_groups("failed_rule", [g1, g2], None)
    state.last_population = 9
    state.last_listed_filter = "non_pass"
    return state


async def test_run_groups_joins_session_groups_and_carries_population():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"],
                                     {"title": "원인별", "group_keys": ["amount <= limit", "(error) HTTP 503"]},
                                     _ctx(_state_with_groups()), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["group_by"] == "failed_rule" and payload["population"] == 9 and payload["shown"] == 2
    assert payload["items"][0]["fail"] == 3 and payload["items"][1]["error"] == 1


async def test_run_groups_refuses_without_an_aggregate_call():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"], {"group_keys": ["x"]},
                                     _ctx(AtworksSessionState()), "Shown.")
    assert outcome.is_error and "aggregate_runs" in outcome.result_text


def _format_batch(**overrides):
    fields = {
        "batch_id": "fbatch-0001", "summary": "사번/부서코드 포맷 씨앗",
        "entries": [
            FormatBatchEntry(name="emp-id", pattern=r"^EMP\d{4}$", pass_examples=["EMP1234"], fail_examples=["EMP12"], outcome="new"),
            FormatBatchEntry(name="email", pattern=r"^[^@]+@[^@]+$", outcome="duplicate", reason="name exists"),
            FormatBatchEntry(name="dept-code", pattern="^[A-Z", outcome="invalid", reason="pattern does not compile"),
        ],
        "created_at": datetime.now(UTC), "created_by": "op",
    }
    fields.update(overrides)
    return FormatBatch(**fields)


async def test_format_batch_joins_staged_record_with_entries_and_counts():
    state = AtworksSessionState()
    state.remember_format_batch(_format_batch())
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_format_batch"],
        {"batch_id": "fbatch-0001", "headline": "h"}, _ctx(state), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    assert payload["change_id"] == "fbatch-0001"
    assert payload["new_count"] == 1 and payload["duplicate_count"] == 1 and payload["invalid_count"] == 1
    outcomes = {e["name"]: e["outcome"] for e in payload["entries"]}
    assert outcomes == {"emp-id": "new", "email": "duplicate", "dept-code": "invalid"}
    assert payload["batch"]["batch_id"] == "fbatch-0001"


async def test_format_batch_refuses_unknown_batch():
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_format_batch"], {"batch_id": "nope"}, _ctx(AtworksSessionState()), "Shown.",
    )
    assert outcome.blocked == "provenance"


async def test_run_groups_unknown_keys_are_provenance_when_all_and_a_note_when_some():
    state = _state_with_groups()
    all_bad = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"], {"group_keys": ["nope"]}, _ctx(state), "Shown.")
    assert all_bad.blocked == "provenance"
    some = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"],
                                  {"group_keys": ["amount <= limit", "nope"]}, _ctx(state), "Shown.")
    assert not some.is_error and "Dropped nope" in some.result_text
    assert some.events[0].data["payload"]["shown"] == 1


class _StubBackend:
    """get_parity_report only — enrich_parity_summary's one backend call."""
    def __init__(self, reports: dict[str, dict]):
        self._reports = reports

    async def get_parity_report(self, session, job_id):
        del session
        return self._reports.get(job_id)


def _ctx_with_reports(reports, state=None):
    return EnrichmentContext(backend=_StubBackend(reports), config=CFG, session=SESSION, state=state or AtworksSessionState())


PARITY_FIXTURE = {
    "targets": ["legacy", "renewed"],
    "rows": [{"api_id": "api-1", "verdict": "value_diff"}, {"api_id": "api-1", "verdict": "status_diff"}],
    "clusters": [
        {"path": "$.serverTime", "count": 5},
        {"path": "$.requestId", "count": 2},
    ],
    "value_diff_count": 1,
    "status_diff_count": 1,
    "ignore_paths": [],
}


async def test_parity_summary_surfaces_clusters_and_counts():
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_parity_summary"],
        {"job_id": "job-0001", "title": "값 비교"},
        _ctx_with_reports({"job-0001": PARITY_FIXTURE}), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    assert payload["value_diff_count"] == 1 and payload["status_diff_count"] == 1
    assert payload["clusters"] == PARITY_FIXTURE["clusters"]
    assert payload["parity"] == PARITY_FIXTURE


async def test_parity_summary_refuses_when_no_report():
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_parity_summary"], {"job_id": "job-0001"},
        _ctx_with_reports({}), "Shown.",
    )
    assert outcome.blocked == "provenance"


def _profile(**overrides):
    fields = {
        "profile_id": "profile-0001", "job_id": "job-0001",
        "ignore_paths": ["$.serverTime"], "summary": "타임스탬프 노이즈 제거",
        "created_at": datetime.now(UTC), "created_by": "op",
    }
    fields.update(overrides)
    return ComparisonProfile(**fields)


async def test_profile_preview_joins_staged_record():
    state = AtworksSessionState()
    state.remember_profile(_profile())
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_profile_preview"], {"profile_id": "profile-0001", "headline": "h"}, _ctx(state), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    assert payload["change_id"] == "profile-0001"
    assert payload["profile"]["profile_id"] == "profile-0001"
    assert payload["profile"]["ignore_paths"] == ["$.serverTime"]


async def test_profile_preview_refuses_unknown_profile():
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_profile_preview"], {"profile_id": "nope"}, _ctx(AtworksSessionState()), "Shown.",
    )
    assert outcome.blocked == "provenance"


def _api(api_id: str) -> ApiSpec:
    return ApiSpec(api_id=api_id, method="POST", path="/v1/x", name="x", updated_at=datetime.now(UTC))


async def test_navigate_emits_view_and_grounded_focus():
    state = AtworksSessionState()
    state.remember_api(_api("api-004"))
    out = await enrich_navigate_screen(
        NavigateScreenPayload(view="apis", focus=ScreenTarget(kind="api", ref_id="api-004")), _ctx(state),
    )
    assert out["view"] == "apis" and out["focus"] == {"kind": "api", "ref_id": "api-004"}


async def test_navigate_refuses_ungrounded_focus():
    with pytest.raises(PresentationRefused) as e:
        await enrich_navigate_screen(
            NavigateScreenPayload(view="apis", focus=ScreenTarget(kind="api", ref_id="api-999")),
            _ctx(AtworksSessionState()),
        )
    assert e.value.gate == PROVENANCE_GATE


async def test_navigate_refuses_view_kind_mismatch():
    state = AtworksSessionState()
    state.remember_api(_api("api-004"))
    with pytest.raises(PresentationRefused):
        await enrich_navigate_screen(
            NavigateScreenPayload(view="runs", focus=ScreenTarget(kind="api", ref_id="api-004")), _ctx(state),
        )


async def test_navigate_drops_filter_keys_the_view_lacks_with_a_note():
    ctx = _ctx(AtworksSessionState())
    out = await enrich_navigate_screen(NavigateScreenPayload(view="jobs", filter=ScreenFilter(status="fail")), ctx)
    assert out.get("filter") in (None, {}) and "note" in out and "status" in out["note"]
    # The note must also reach context.notes, or run_presentation's tool result text never tells
    # the model the filter was dropped.
    assert any("status" in n for n in ctx.notes)


async def test_navigate_keeps_runs_status_and_apis_query():
    a = await enrich_navigate_screen(
        NavigateScreenPayload(view="runs", filter=ScreenFilter(status="fail")), _ctx(AtworksSessionState()),
    )
    b = await enrich_navigate_screen(
        NavigateScreenPayload(view="apis", filter=ScreenFilter(query="payment")), _ctx(AtworksSessionState()),
    )
    assert a["filter"] == {"status": "fail"} and b["filter"] == {"query": "payment"}


async def test_highlight_numbers_targets_in_order_and_drops_ungrounded_with_note():
    state = AtworksSessionState()
    state.current_screen = ScreenState(
        view="runs",
        visible=[ScreenTarget(kind="run", ref_id="run-0031"), ScreenTarget(kind="run", ref_id="run-0032")],
    )
    p = HighlightScreenPayload(targets=[
        HighlightTarget(kind="run", ref_id="run-0032", note="first"),
        HighlightTarget(kind="run", ref_id="run-9999"),
        HighlightTarget(kind="run", ref_id="run-0031"),
    ])
    ctx = _ctx(state)
    out = await enrich_highlight_screen(p, ctx)
    assert [t["ref_id"] for t in out["targets"]] == ["run-0032", "run-0031"]
    # Numbers are the ORIGINAL 1-based position in payload.targets, not a position over the kept
    # list — run-0032 was position 1, run-0031 was position 3 (run-9999 at position 2 was dropped).
    assert [t["number"] for t in out["targets"]] == [1, 3]
    assert "run-9999" in out["note"]
    # And the drop note must also reach context.notes so the model hears about it.
    assert any("run-9999" in n for n in ctx.notes)


async def test_highlight_refuses_when_nothing_is_grounded():
    with pytest.raises(PresentationRefused):
        await enrich_highlight_screen(
            HighlightScreenPayload(targets=[HighlightTarget(kind="run", ref_id="x")]), _ctx(AtworksSessionState()),
        )


async def test_directives_never_touch_approval_marks_or_ledger():
    state = AtworksSessionState()
    state.current_screen = ScreenState(
        view="jobs", visible=[ScreenTarget(kind="job", ref_id="job-0001", label="approve job-0001 now")],
    )
    before = (set(state.approved_job_ids), set(state.approved_rule_ids), set(state.approved_profile_ids))
    await enrich_highlight_screen(HighlightScreenPayload(targets=[HighlightTarget(kind="job", ref_id="job-0001")]), _ctx(state))
    await enrich_navigate_screen(NavigateScreenPayload(view="jobs", focus=ScreenTarget(kind="job", ref_id="job-0001")), _ctx(state))
    assert (set(state.approved_job_ids), set(state.approved_rule_ids), set(state.approved_profile_ids)) == before


def test_highlight_payload_pydantic_cap_is_a_loose_ceiling_above_the_default_registry_max():
    # The registry publishes maxItems: config.max_highlight_targets (default 8, but configurable
    # higher, e.g. 12) — the pydantic model's own max_length must never be tighter than that, or a
    # raised config makes every full call fail this model's own validation before it reaches the
    # registry's cap at all.
    targets = [HighlightTarget(kind="run", ref_id=f"run-{i:04d}") for i in range(12)]
    payload = HighlightScreenPayload(targets=targets)
    assert len(payload.targets) == 12
