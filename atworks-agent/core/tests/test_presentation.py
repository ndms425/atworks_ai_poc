import json
from datetime import UTC, datetime

import pytest
from commerce_common.presentation import EnrichmentContext, PresentationRefused, run_presentation

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.enrichment import (
    PRESENTATION_COMPONENTS,
    enrich_highlight_screen,
    enrich_navigate_screen,
    enrich_query_table,
)
from atworks_agent.gates import PROVENANCE_GATE
from atworks_agent.rules import RuleImpact, ValidationRule
from atworks_agent.tools.presentation import (
    HighlightScreenPayload,
    HighlightTarget,
    NavigateScreenPayload,
    PresentQueryTablePayload,
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
    QueryFilters,
    QueryResult,
    QueryRow,
    QuerySpec,
    RunGroup,
    RunResult,
    RunStatus,
    ScreenFilter,
    ScreenState,
    ScreenTarget,
    VocabularyEntry,
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


async def test_run_digest_run_record_drops_response_body_and_sets_has_body():
    # Review fix (Task 3 round 1): enrich_run_digest used to dump the RunResult through a bare
    # model_dump with no exclusion, carrying the raw response_body into a card payload the web
    # and the model both see. It must go through run_record like every other run serialization.
    state = _state_with_run()
    secret_run = RunResult(
        run_id="run-99", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
        status=RunStatus.FAIL, failed_rules=["amount >= 0"], http_status=200,
        response_body={"secret": "4111-1111-1111-1111"},
    )
    state.remember_run(secret_run)
    state.last_listed_run_ids = ["run-17", "run-99"]
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-99", "headline": "x"}]},
        _ctx(state), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    entry = payload["items"][0]
    assert "response_body" not in entry["run"]
    assert entry["run"]["has_body"] is True
    dumped = json.dumps(payload)
    assert "4111" not in dumped
    assert '"response_body"' not in dumped


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


async def test_run_groups_renders_a_group_whose_evidence_scan_found_nothing():
    """Fix round 1 (G): ``Store._fill_run_ids_by_scan`` is bounded, so a failed_rule/http_status
    group whose runs are all older than the newest ``_RUN_ID_SCAN_CAP`` comes back with exact
    counters and ``run_ids == []``. The card must still show the row (the web omits only its
    "attach" button) — an empty evidence list is not a refusal."""
    state = AtworksSessionState()
    state.remember_groups("failed_rule", [
        RunGroup(key="amount <= limit", label="amount <= limit", count=7, fail=7, error=0, passed=0, run_ids=[]),
    ], None)
    state.last_population = 9

    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"],
                                     {"group_keys": ["amount <= limit"]}, _ctx(state), "Shown.")

    payload = outcome.events[0].data["payload"]
    assert payload["shown"] == 1 and payload["items"][0]["count"] == 7
    assert payload["items"][0]["run_ids"] == []


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


async def test_run_groups_payload_never_carries_a_cited_runs_response_body():
    # RunGroup only ever carries run_ids (strings), never embedded RunResult objects, so this
    # locks in that a group citing a body-bearing run cannot leak it through this card either.
    state = AtworksSessionState()
    state.remember_run(RunResult(
        run_id="run-99", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
        status=RunStatus.FAIL, http_status=200, response_body={"secret": "4111-1111-1111-1111"},
    ))
    g = RunGroup(key="amount <= limit", label="amount <= limit", count=1, fail=1, error=0, passed=0, run_ids=["run-99"])
    state.remember_groups("failed_rule", [g], None)
    state.last_population = 1
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_groups"], {"group_keys": ["amount <= limit"]}, _ctx(state), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    dumped = json.dumps(payload)
    assert "4111" not in dumped
    assert '"response_body"' not in dumped


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


# -- query_table (self-growth spec §5) ---------------------------------------------------

def _query_result(**overrides):
    fields = {"filters": QueryFilters(status="non_pass", window_days=30),
              "dimensions": ["path_segment_2"], "measures": ["non_pass", "apis"],
              "order_by": "non_pass", "limit": 20} | overrides.pop("spec", {})
    spec = QuerySpec(**fields)
    base = {
        "spec": spec,
        "rows": [
            QueryRow(keys={"path_segment_2": "contracts"}, measures={"non_pass": 12, "apis": 3},
                     api_ids=["api-1", "api-2"], run_ids=["run-9", "run-8"]),
            QueryRow(keys={"path_segment_2": "orders"}, measures={"non_pass": 4, "apis": 1},
                     api_ids=["api-3"], run_ids=["run-2"]),
        ],
        "total_groups": 7, "population": 431,
        "window": (datetime(2026, 8, 9, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC)),
        "source": "rollup_day",
    }
    return QueryResult(**(base | overrides))


async def _query_table(state, tool_input=None):
    return await run_presentation(
        PRESENTATION_COMPONENTS["present_query_table"],
        tool_input or {"title": "엔드포인트별 실패"}, _ctx(state), "Shown.",
    )


async def test_query_table_numbers_come_from_the_last_query_result():
    state = AtworksSessionState()
    state.last_query_result = _query_result()
    outcome = await _query_table(state)
    ui = outcome.events[0]
    assert ui.data["component"] == "query_table"
    payload = ui.data["payload"]
    assert payload["population"] == 431 and payload["total_groups"] == 7
    assert payload["source"] == "rollup_day"
    assert payload["window"]["since"].startswith("2026-08-09")
    assert [r["measures"] for r in payload["rows"]] == [{"non_pass": 12, "apis": 3}, {"non_pass": 4, "apis": 1}]
    assert payload["rows"][0]["run_ids"] == ["run-9", "run-8"]
    assert payload["title"] == "엔드포인트별 실패" and payload["note"] is None


async def test_query_table_columns_are_catalogue_labels_in_spec_order():
    state = AtworksSessionState()
    state.last_query_result = _query_result()
    payload = (await _query_table(state)).events[0].data["payload"]
    assert [(c["key"], c["kind"]) for c in payload["columns"]] == [
        ("path_segment_2", "dimension"), ("non_pass", "measure"), ("apis", "measure"),
    ]
    assert payload["columns"][0]["label"] == "경로 2조각별"
    assert payload["columns"][1]["label"] == "실패+에러 수"
    assert payload["compare"] is False and payload["compare_note"] is None


async def test_query_table_compare_adds_prev_and_delta_columns_and_the_two_top_lists_note():
    state = AtworksSessionState()
    result = _query_result(spec={"compare_previous_window": True})
    for row in result.rows:
        row.measures |= {"non_pass_prev": 0, "non_pass_delta": row.measures["non_pass"],
                         "apis_prev": None, "apis_delta": None}
    state.last_query_result = result
    payload = (await _query_table(state)).events[0].data["payload"]
    assert [(c["key"], c["kind"]) for c in payload["columns"]] == [
        ("path_segment_2", "dimension"),
        ("non_pass", "measure"), ("non_pass_prev", "prev"), ("non_pass_delta", "delta"),
        ("apis", "measure"), ("apis_prev", "prev"), ("apis_delta", "delta"),
    ]
    # The ledger's ruling: the compare column is a comparison of two TOP LISTS, and the card
    # must say so -- a key below the earlier window's cut reads 0, not "it did not happen".
    assert payload["compare"] is True
    assert "상위 목록" in payload["compare_note"]
    assert payload["rows"][0]["measures"]["apis_prev"] is None   # unmeasurable stays null, not 0


async def test_query_table_summary_names_the_window_and_carries_the_spec():
    state = AtworksSessionState()
    state.last_query_result = _query_result()
    payload = (await _query_table(state)).events[0].data["payload"]
    assert payload["spec_summary"] == "실패·에러 · 경로 2조각별 · 30일 · 상위 20"
    assert payload["spec"]["dimensions"] == ["path_segment_2"]
    assert payload["spec"]["filters"]["window_days"] == 30
    assert json.dumps(payload["spec"])   # the footer's collapsible JSON must serialize


async def test_query_table_without_dimensions_still_has_a_first_column():
    state = AtworksSessionState()
    result = _query_result(spec={"dimensions": []})
    result.rows = [QueryRow(keys={}, measures={"non_pass": 431, "apis": 12})]
    state.last_query_result = result
    payload = (await _query_table(state)).events[0].data["payload"]
    assert payload["columns"][0] == {"key": "_total", "label": "전체", "kind": "dimension"}
    assert payload["rows"][0]["keys"] == {"_total": "전체"}


async def test_query_table_carries_the_turn_id_and_no_alias_yet():
    state = AtworksSessionState()
    state.current_turn_id = "turn-77"
    state.last_query_result = _query_result()
    payload = (await _query_table(state)).events[0].data["payload"]
    assert payload["turn_id"] == "turn-77" and payload["pending_alias"] is None


async def test_query_table_asks_to_confirm_this_turn_s_alias():
    # self-growth §7: the confirmation rides the card the operator is already reading, and its
    # wording is built from catalogue labels -- the model contributes the term, nothing else.
    state = AtworksSessionState()
    state.last_query_result = _query_result()
    state.pending_aliases.append(VocabularyEntry(
        term="결제 계열", fragment=QueryFilters(path_prefix="/v1/payment"),
        proposed_by="minseong", proposed_at=datetime(2026, 9, 8, tzinfo=UTC),
    ))
    payload = (await _query_table(state)).events[0].data["payload"]
    assert payload["pending_alias"] == {"term": "결제 계열",
                                        "fragment_summary": "경로 접두사 /v1/payment"}


async def test_query_table_asks_about_the_newest_alias_only():
    state = AtworksSessionState()
    state.last_query_result = _query_result()
    for term, prefix in (("결제 계열", "/v1/payment"), ("계약 계열", "/v1/contract")):
        state.pending_aliases.append(VocabularyEntry(
            term=term, fragment=QueryFilters(path_prefix=prefix), proposed_by="minseong",
            proposed_at=datetime(2026, 9, 8, tzinfo=UTC),
        ))
    payload = (await _query_table(state)).events[0].data["payload"]
    assert payload["pending_alias"]["term"] == "계약 계열"


async def test_query_table_refuses_without_a_query_result():
    outcome = await _query_table(AtworksSessionState())
    assert outcome.blocked == PROVENANCE_GATE and not outcome.events
    assert "query_runs" in outcome.result_text
    # And the enrichment hook itself refuses, not just the wrapper.
    with pytest.raises(PresentationRefused) as refused:
        await enrich_query_table(PresentQueryTablePayload(title="t"), _ctx(AtworksSessionState()))
    assert refused.value.gate == PROVENANCE_GATE
