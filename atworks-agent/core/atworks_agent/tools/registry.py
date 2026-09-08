"""툴 계약, 고정 순서. 목록은 config만의 함수라 매 요청 같은 바이트다. 툴 설명은 그 툴 하나의
규칙만 담고, 툴을 가로지르는 계약(스테이징·판정 금지)은 프롬프트에, 흐름은 스킬에 있다."""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, get_args

from commerce_common.execution import LOAD_SKILL, with_status
from commerce_common.presentation import PresentationExtension

from ..catalog import catalog_hint
from ..config import AtworksAgentConfig
from ..question_form import QUESTION_FORM_INPUT_SCHEMA
from ..types import Dimension, HttpMethod, Measure, RunStatusFilter
from .presentation import (
    DIGEST_TOOL,
    FORMAT_BATCH_TOOL,
    GROUPS_TOOL,
    HIGHLIGHT_SCREEN_TOOL,
    NAVIGATE_SCREEN_TOOL,
    PARITY_SUMMARY_TOOL,
    PREVIEW_TOOL,
    PROFILE_PREVIEW_TOOL,
    QUERY_TABLE_TOOL,
    QUESTION_TOOL,
    RULE_PREVIEW_TOOL,
)

_STATUS_READER = "the operator"
_SESSION_API_ID = "api_id that search_apis or get_api returned this session."
_ISO_DATETIME = "ISO 8601 datetime with offset, e.g. 2026-08-27T00:00:00+09:00."


def _job_id() -> dict[str, Any]:
    return {"type": "string", "description": "job_id staged this conversation or listed by get_pending_jobs."}


def _rule_id() -> dict[str, Any]:
    return {"type": "string", "description": "rule_id staged this conversation or listed by get_pending_rules."}


def _batch_id() -> dict[str, Any]:
    return {"type": "string", "description": "batch_id staged this conversation or listed by get_pending_format_batches."}


def _profile_id() -> dict[str, Any]:
    return {"type": "string", "description": "profile_id staged this conversation or listed by get_pending_profiles."}


# -- query_runs (self-growth spec §3/§5) ----------------------------------------------------
# The schema MIRRORS QuerySpec/QueryFilters field for field, with the catalogue enums read from
# the Dimension/Measure literals themselves (get_args) rather than retyped -- so a catalogue that
# grows moves the tool bytes and pydantic together. It is hand-written rather than
# `QuerySpec.model_json_schema()` because that emits $defs/$ref and `anyOf: [T, null]` wrappers
# for every optional field; every other tool here is a flat hand-written schema, and
# test_registry asserts the property names match the models so the two cannot drift apart.
# Nothing below reads config: these bytes are a constant, which is what keeps the tool list a
# pure function of config (cache-stable prefix).
_QUERY_EXAMPLES = (
    {"filters": {"status": "non_pass", "window_days": 30}, "dimensions": ["path_segment_2"],
     "measures": ["non_pass", "apis", "runs"], "limit": 20},
    {"filters": {"scope_operator": "<operator_id>", "window_days": 14}, "dimensions": ["day"],
     "measures": ["runs", "non_pass"], "order_by": "key", "descending": False},
)


def _query_filters_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False, "properties": {
            "status": {"type": "string", "enum": list(get_args(RunStatusFilter))},
            "since": {"type": "string", "description": _ISO_DATETIME + " Use since/until OR window_days, never both."},
            "until": {"type": "string", "description": _ISO_DATETIME},
            "window_days": {"type": "integer", "minimum": 1, "maximum": 180,
                            "description": "The window ends now and runs back this many days. Cannot be combined with since/until."},
            "api_ids": {"type": "array", "maxItems": 100, "items": {"type": "string", "description": _SESSION_API_ID}},
            "path_contains": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 120},
                              "description": "Lowercase substring match on the API path; several entries are OR-ed."},
            "path_prefix": {"type": "string", "maxLength": 120, "description": "API paths starting with this prefix."},
            "method": {"type": "array", "items": {"type": "string", "enum": list(get_args(HttpMethod))}},
            "api_group": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 60}},
            "target_env": {"type": "array", "items": {"type": "string", "maxLength": 40}},
            "test_data_label": {"type": "array", "items": {"type": "string", "maxLength": 40}},
            "executed_by": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 60},
                            "description": "Runs THOSE operators executed. Different from scope_operator."},
            "scope_operator": {"type": "string", "maxLength": 60,
                               "description": "Narrow to the APIs this operator has run ('내가 실행한 것'). Only the operator of this session; naming anyone else is refused."},
            "failed_rule": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 120}},
            "http_status": {"type": "array", "maxItems": 10, "items": {"type": "integer"}},
        },
    }


def _query_spec_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False, "required": ["measures"], "properties": {
            "filters": _query_filters_schema(),
            "dimensions": {"type": "array", "maxItems": 2, "items": {"type": "string", "enum": list(get_args(Dimension))},
                           "description": "How to group, 0-2 axes. Omit (or []) for one grand-total row."},
            "measures": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string", "enum": list(get_args(Measure))},
                         "description": "What to compute per group."},
            "order_by": {"type": "string", "enum": [*get_args(Measure), "key"],
                         "description": "One of the measures you asked for, or 'key' for the group key. Defaults to non_pass when you asked for it, otherwise to the first measure."},
            "descending": {"type": "boolean", "description": "Default true; use false with order_by 'key' for a chronological trend."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many groups come back. Default 20."},
            "compare_previous_window": {"type": "boolean",
                                        "description": "Also run the immediately preceding window of the same length and attach _prev/_delta per measure. It compares two TOP lists, not two populations."},
            "include_samples": {"type": "boolean", "description": "Default true: each row carries up to 20 api_ids and the 5 newest run_ids as evidence."},
        },
    }


def _query_runs_description() -> str:
    examples = "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in _QUERY_EXAMPLES)
    return (
        "Ask one structured question of the run history and get host-computed group figures back. "
        "You pick the axes (dimensions), the numbers (measures), the filters, the order and the cut; "
        "the host compiles and runs it over its own materialized data — you never write SQL and you "
        "never compute a figure yourself. Read-only: it stages nothing and judges nothing. Reach for "
        "it whenever the operator's grouping or filter is outside aggregate_runs' five axes (by "
        "endpoint segment, by method, by week, by operator, two axes at once, a window comparison). "
        "Every returned row carries evidence ids you may then cite. Show the result with "
        "present_query_table — never restate its numbers in prose. / 실행 이력에 구조화 질의 1회. "
        "축·측정값·필터·정렬은 네가 고르고 숫자는 호스트가 계산한다. 읽기 전용이고, 결과는 "
        "present_query_table 카드로 보여준다.\n\n"
        "Catalogue (dimensions then measures):\n" + catalog_hint() + "\n\nExamples:\n" + examples
    )


def build_tools(
    config: AtworksAgentConfig,
    skill_names: list[str],
    extra_presentation_tools: Sequence[PresentationExtension] = (),
) -> list[dict[str, Any]]:
    chips_alone_cases = "a clarifying question, an API read-back"
    if config.stages_jobs:
        chips_alone_cases = "a guardrail explanation, " + chips_alone_cases
        if config.stage_shows_preview:
            chips_alone_cases += ", the sentence after a staging"

    tools: list[dict[str, Any]] = [
        {
            "name": LOAD_SKILL,
            "description": "Load a skill's full instructions when the request matches its entry in the skill index; then follow them for the rest of the flow.",
            "input_schema": {"type": "object", "properties": {"skill_name": {"type": "string", "enum": sorted(skill_names), "description": "Name of the skill as listed in the index."}},
                             "required": ["skill_name"], "additionalProperties": False},
        },
        # -- 읽기 -----------------------------------------------------------------------
        {
            "name": "search_apis",
            "description": ("Find registered APIs by text, group, or last-updated date. Use updated_after to resolve "
                            "'updated in the last week' style selections; the ids it returns are the only ids a job may name. "
                            "The result carries `total` (the true count for this filter, independent of `limit`) and "
                            "`next_cursor`; cite `total`, never the length of `items`. / 등록된 API를 검색한다. '지난 1주일 "
                            "업데이트'는 updated_after로 푼다. total이 모집단, items 길이가 아니다."),
            "input_schema": {"type": "object", "properties": {
                "query": {"type": "string", "maxLength": 120, "description": "Free text over method, path, name; empty scans everything."},
                "group": {"type": "string", "maxLength": 60},
                "updated_after": {"type": "string", "description": _ISO_DATETIME},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                "additionalProperties": False},
        },
        {
            "name": "get_api",
            "description": "Full record for one API. / API 1건의 전체 스펙.",
            "input_schema": {"type": "object", "properties": {"api_id": {"type": "string", "description": _SESSION_API_ID}},
                             "required": ["api_id"], "additionalProperties": False},
        },
        {
            "name": "list_runs",
            "description": ("Run results, newest first, filtered by `filters` (time window `since`, `status` "
                            "pass|fail|error|non_pass, or `api_id`). The result carries `total` — the true count for "
                            "this filter, independent of `limit` — and `next_cursor`; `total` is the population to "
                            "cite (the digest's population), never the length of `items`. The status on each run is "
                            "the deterministic verdict; never restate it as your own judgment. / 실행 이력. status는 "
                            "결정론 판정이다. total이 모집단이다."),
            "input_schema": {"type": "object", "properties": {
                "filters": {"type": "object", "properties": {
                    "since": {"type": "string", "description": _ISO_DATETIME},
                    "status": {"type": "string", "enum": ["pass", "fail", "error", "non_pass"],
                               "description": "One verdict, or non_pass for fail and error together (the triage population)."},
                    "api_id": {"type": "string", "description": _SESSION_API_ID}},
                    "additionalProperties": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                "additionalProperties": False},
        },
        {
            "name": "get_run",
            "description": "One run result by id, with its failed rules. / 실행 1건 상세.",
            "input_schema": {"type": "object", "properties": {"run_id": {"type": "string", "description": "run_id that list_runs returned this session."}},
                             "required": ["run_id"], "additionalProperties": False},
        },
        {
            "name": "rank_failed_runs",
            "description": ("Order this session's non-pass runs by a NAMED deterministic scorer and return the top items with "
                            "reasons. This is a reading order ('look first'), not a verdict and not a safety guarantee for the "
                            "rest. Call list_runs first. / 세션에서 본 실패 실행을 명명된 스코어러로 정렬한다. 판정이 아니다."),
            "input_schema": {"type": "object", "properties": {
                "scorer": {"type": "string", "enum": ["risk_v1"], "description": "Which scorer; risk_v1 unless the operator names another."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                "additionalProperties": False},
        },
        {
            "name": "aggregate_runs",
            "description": ("Group run results by ONE axis and return per-group counts, first-failure time, the last "
                            "pass before it, flakiness (flaky_v1) and p95 duration — all computed by the host, never by "
                            "you. Use it for 'group failures by cause' (failed_rule), 'since when is X broken' (api + "
                            "api_id), 'which APIs flap' (api_env_data). Call it before present_run_groups. / 실행 기록을 "
                            "한 축으로 묶어 집계한다. 숫자는 서버 계산이다."),
            "input_schema": {"type": "object", "properties": {
                "group_by": {"type": "string", "enum": ["api", "failed_rule", "http_status", "env", "api_env_data"]},
                "since": {"type": "string", "description": _ISO_DATETIME + f" Default and ceiling: now minus {config.max_aggregate_window_days} days."},
                "status": {"type": "string", "enum": ["pass", "fail", "error", "non_pass"]},
                "api_id": {"type": "string", "description": _SESSION_API_ID}},
                "required": ["group_by"], "additionalProperties": False},
        },
        {
            "name": "query_runs",
            "description": _query_runs_description(),
            "input_schema": _query_spec_schema(),
        },
        {
            "name": "note_unmet_ask",
            "description": ("Call this BEFORE you explain, whenever the catalogue above cannot answer what was asked — a "
                            "dimension that does not exist, evidence this deployment never recorded, a question outside "
                            "API testing, or something you must refuse. It renders no card and reads nothing; it records "
                            "the gap so the deployment can grow a dimension for it. Then answer in one sentence saying "
                            "WHAT WOULD LET YOU ANSWER — ending on '지원하지 않습니다' is a rule violation. / 카탈로그로 "
                            "답할 수 없다고 판단하면 설명하기 전에 부른다. 카드는 없다."),
            "input_schema": {"type": "object", "properties": {
                "reason": {"type": "string", "enum": ["no_dimension", "no_evidence", "out_of_scope", "refused"],
                           "description": ("no_dimension: the grouping or filter is not in the catalogue. no_evidence: the "
                                           "catalogue has it but this deployment recorded nothing to answer from. "
                                           "out_of_scope: not an API-testing question. refused: answering would break a rule.")},
                "summary": {"type": "string", "maxLength": 200, "description": "One line: what the operator asked. No values you were not shown."},
                "wanted": {"type": "string", "maxLength": 200, "description": "One line: what dimension, filter or record WOULD have answered it."}},
                "required": ["reason", "summary"], "additionalProperties": False},
        },
        {
            "name": "propose_alias",
            "description": ("Call this in the SAME turn you read an operator's own word as a filter — '결제 계열' as "
                            "path_prefix /v1/payment, '결제 API' as api_group payment. It proposes the reading to the "
                            "team; the card then asks the operator [예]/[아니오], and once someone confirms it, every "
                            "operator's later turns carry that term and you stop asking. Propose the SHAPE the word "
                            "names — a path, a method, a group, a target env, a rule, a status code — never a time "
                            "window and never a list of ids (those are true today and false next month, and the tool "
                            "refuses them). Do not propose catalogue words that already mean themselves, and do not "
                            "propose a person's or a customer's name. It stores nothing until a person clicks; it "
                            "renders no card of its own. / 사용자 용어를 필터로 해석했으면 그 해석을 제안한다. 사람이 "
                            "한 번 확인하면 팀 전체의 컨텍스트에 들어간다."),
            "input_schema": {"type": "object", "properties": {
                "term": {"type": "string", "maxLength": 40,
                         "description": "The operator's own word, as they wrote it (≤40 chars)."},
                "fragment": {**_query_filters_schema(),
                             "description": ("What the word means, as QueryFilters fields. At least one; "
                                             "since/until/window_days/api_ids/scope_operator are refused.")},
                "note": {"type": "string", "maxLength": 120,
                         "description": "Optional: one line on why you read it this way."}},
                "required": ["term", "fragment"], "additionalProperties": False},
        },
        # -- 실행 계획 ------------------------------------------------------------------
        {
            "name": "get_pending_jobs",
            "description": "Jobs staged and waiting for approval. / 승인 대기 중인 실행 계획.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "stage_job",
            "description": ("Stage an execution plan (JobSpec) for the operator's approval — it runs nothing. api_ids must come "
                            "from search_apis/get_api this session. One job may span several environments, schedules and "
                            "test-data sets; it runs the whole matrix once per schedule occurrence. Every value you defaulted "
                            "(target_envs, schedule start, binding, test data) goes into assumptions with a confidence below "
                            "0.5, so the preview asks the operator. With stage_shows_preview the call renders its own preview "
                            "card. / 실행 계획을 스테이징한다. 실행하지 않는다. 한 job은 여러 대상 계·여러 스케줄·여러 테스트 "
                            "데이터를 가질 수 있고, 스케줄 1회마다 전체 매트릭스를 실행한다. 기본값으로 채운 슬롯은 "
                            "assumptions+낮은 confidence로 표시한다."),
            "input_schema": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["run_now", "scheduled_run"]},
                "summary": {"type": "string", "maxLength": 200, "description": "One line the preview card shows."},
                "api_ids": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "string", "description": _SESSION_API_ID}},
                "target_envs": {"type": "array", "minItems": 1, "maxItems": config.max_target_envs_per_job,
                                "items": {"type": "string"},
                                "description": "dev | stg, one or more. Never prod. '양쪽/두 계/비교' means dev and stg. When the operator did not say, default [dev] with confidence 0.3."},
                "schedules": {"type": "array", "maxItems": config.max_schedules_per_job,
                              "items": {"type": "object", "properties": {
                                  "kind": {"type": "string", "enum": ["once", "daily"]},
                                  "at": {"type": "string", "pattern": "^\\d{2}:\\d{2}$"},
                                  "tz": {"type": "string"},
                                  "from_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "First run date. If today's time-of-day has passed, tomorrow — and say so in assumptions."},
                                  "count": {"type": "integer", "minimum": 1, "maximum": 30}},
                                  "required": ["kind", "at", "from_date", "count"], "additionalProperties": False},
                              "description": "Empty for run_now. Each entry runs the whole matrix once per occurrence."},
                "test_data": {"type": "array", "maxItems": config.max_test_data_sets,
                              "items": {"type": "object", "properties": {
                                  "label": {"type": "string", "maxLength": 40},
                                  "values": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 200}}},
                                  "required": ["label", "values"], "additionalProperties": False},
                              "description": "Parameter bindings applied identically to every environment. Keys must be params of the selected APIs (see get_api). When the operator says '임의로' propose one set per scenario worth covering, label each, and put every invented value into assumptions with confidence 0.4 — the operator approves the values on the preview card."},
                "binding": {"type": "string", "enum": ["FROZEN", "LATE"], "description": "FROZEN: today's resolved api_ids every run. LATE: re-evaluate select_where each run. Ambiguous from speech — ask via present_question_form or set confidence 0.4."},
                "select_where": {"type": "object", "properties": {
                    "query": {"type": "string", "maxLength": 120},
                    "group": {"type": "string", "maxLength": 60},
                    "updated_after": {"type": "string", "description": _ISO_DATETIME},
                    "failed_since": {"type": "string", "description": _ISO_DATETIME + " Server-resolved: keep only APIs with a fail/error run at or after this time ('실패한 것만 다시'). Default 24h ago when the operator gives no window."},
                    "related_to": {"type": "string", "description": _SESSION_API_ID + " Server-resolved: APIs in the same group or under the same leading path as this one ('X 고쳤는데 뭘 다시 돌려야 해')."}},
                    "additionalProperties": False,
                    "description": "The search that produced api_ids (query/group/updated_after), kept for LATE binding and for the preview's provenance. When failed_since or related_to is set the host resolves api_ids itself and replaces the list you sent; the card shows the selection basis."},
                "report": {"type": "boolean"},
                "confidence": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}, "description": "Per-slot confidence: target_envs, schedules, test_data, binding, api_ids, report."},
                "assumptions": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 160}}},
                "required": ["kind", "summary", "api_ids", "target_envs"], "additionalProperties": False},
        },
        {
            "name": "apply_job",
            "description": ("Apply a job the operator approved on the approval surface. It is the only tool that changes live "
                            "state; a job not marked approved by the host is held. / 승인된 job만 적용된다."),
            "input_schema": {"type": "object", "properties": {"job_id": _job_id()}, "required": ["job_id"], "additionalProperties": False},
        },
        {
            "name": "discard_job",
            "description": "Discard a staged job the operator rejected or replaced.",
            "input_schema": {"type": "object", "properties": {"job_id": _job_id()}, "required": ["job_id"], "additionalProperties": False},
        },
        # -- 검증 규칙 --------------------------------------------------------------------
        {
            "name": "stage_rule",
            "description": ("Stage a validation rule (a structured draft) for one API parameter — it judges "
                            "nothing. param must be one get_api listed for this api_id. Prefer a named `format` "
                            "(email, date, iso8601, uuid, number) over a raw `pattern`; a raw pattern is flagged "
                            "for review. The rule applies only after the operator approves it on the Rules page, "
                            "and only to runs executed after that — it never re-judges a past run. / 검증 규칙 "
                            "초안을 스테이징한다. 판정하지 않는다. param은 get_api가 돌려준 것이어야 하고, Rules "
                            "페이지 승인 후 이후 실행에만 적용된다."),
            "input_schema": {"type": "object", "properties": {
                "api_id": {"type": "string", "description": _SESSION_API_ID},
                "param": {"type": "string", "maxLength": 80, "description": "A parameter get_api listed for this api_id."},
                "kind": {"type": "string", "enum": list(config.allowed_rule_kinds)},
                "op": {"type": "string", "enum": list(config.allowed_compare_ops) + ["in", "not_in"],
                       "description": "compare rule: one of the comparison operators. membership rule: in | not_in."},
                "value": {"type": "string", "maxLength": 120, "description": "compare rule's bound."},
                "values": {"type": "array", "maxItems": config.max_membership_values,
                          "items": {"type": "string", "maxLength": 120}, "description": "membership rule's list."},
                "format": {"type": "string", "maxLength": 60, "description": "format rule: a named format — a built-in (email, date, iso8601, uuid, number) or a name saved to the format library — resolved server-side. Preferred over pattern."},
                "pattern": {"type": "string", "maxLength": 200, "description": "format rule: a raw regex, used only when no named format fits. Flagged for review."},
                "pass_examples": {"type": "array", "maxItems": config.max_format_examples,
                                  "items": {"type": "string", "maxLength": 120},
                                  "description": "format rule with a raw pattern: values that MUST match; the system verifies the pattern against them before it can be approved"},
                "fail_examples": {"type": "array", "maxItems": config.max_format_examples,
                                  "items": {"type": "string", "maxLength": 120},
                                  "description": "format rule with a raw pattern: values that MUST NOT match; the system verifies the pattern against them before it can be approved"},
                "save_format_as": {"type": "string", "maxLength": 60,
                                   "description": "optional: save this pattern to the shared library under this name on approval"},
                "summary": {"type": "string", "maxLength": 200, "description": "One line the preview card shows."},
                "confidence": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                              "description": "Per-slot confidence, e.g. for value, values, format."},
                "assumptions": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 160}}},
                "required": ["api_id", "param", "kind", "summary"], "additionalProperties": False},
        },
        {
            "name": "apply_rule",
            "description": ("Apply a rule the operator approved on the Rules page. It is the only tool that "
                            "makes a rule effective; a rule not marked approved by the host is held. Effective "
                            "only for runs executed after this moment — past runs are never re-evaluated. / "
                            "Rules 페이지에서 승인된 규칙만 effective해진다. 과거 실행은 재평가하지 않는다."),
            "input_schema": {"type": "object", "properties": {"rule_id": _rule_id()}, "required": ["rule_id"], "additionalProperties": False},
        },
        {
            "name": "discard_rule",
            "description": "Discard a staged rule the operator rejected or replaced.",
            "input_schema": {"type": "object", "properties": {"rule_id": _rule_id()}, "required": ["rule_id"], "additionalProperties": False},
        },
        {
            "name": "get_pending_rules",
            "description": "Rules staged and waiting for approval. / 승인 대기 중인 검증 규칙.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        # -- 추천 (읽기 전용, 판정 없음) ---------------------------------------------------
        {
            "name": "find_apis_with_param",
            "description": ("Find APIs that declare a given parameter and do NOT already have an applied format "
                            "rule for it — candidates for extending a format you just applied to more APIs. Use "
                            "it after applying a format rule, to offer the operator the same shape on other APIs "
                            "sharing that parameter name. The result carries `total` (the true count, independent "
                            "of `items`' length) and `next_cursor`. / 해당 파라미터를 선언했지만 아직 포맷 규칙이 적용되지 "
                            "않은 API를 찾는다. 방금 적용한 포맷을 다른 API로 넓힐 때 쓴다. total이 실제 건수다."),
            "input_schema": {"type": "object", "properties": {
                "param": {"type": "string", "maxLength": 80, "description": "Parameter name, e.g. one get_api listed."}},
                "required": ["param"], "additionalProperties": False},
        },
        {
            "name": "recommend_rules_for_api",
            "description": ("For each parameter of one API with no applied rule yet, suggest the applied rule(s) "
                            "peer APIs already carry for a same-named parameter. A parameter with no such peer "
                            "gets no suggestion — never fabricate a constraint from the parameter name alone. "
                            "Stage each suggestion individually with stage_rule, referencing the peer's format by "
                            "name; each is approved on its own. / 규칙이 없는 파라미터마다 같은 이름의 파라미터를 "
                            "가진 다른 API의 적용된 규칙을 제안한다. 대응 규칙이 없으면 아무것도 제안하지 않는다. "
                            "제안은 stage_rule로 개별 스테이징하고 각각 승인받는다."),
            "input_schema": {"type": "object", "properties": {
                "api_id": {"type": "string", "description": _SESSION_API_ID}},
                "required": ["api_id"], "additionalProperties": False},
        },
        # -- 포맷 라이브러리 (bulk seed, deduped, one host approval) ----------------------
        {
            "name": "stage_format_batch",
            "description": ("Stage a batch of raw-pattern formats to the shared format library in one call — it "
                            "saves nothing yet. Each entry needs pass_examples and fail_examples; the system "
                            "verifies the pattern against them and marks the entry invalid if it fails to "
                            "classify them correctly. An entry whose name or pattern already exists in the "
                            "library, or repeats an earlier entry in this same batch, is marked duplicate and "
                            "skipped — never re-added. Applying the batch only adds entries marked new. Prefer "
                            "this over calling stage_rule's format authoring repeatedly when the operator wants "
                            "several patterns seeded at once. / 포맷 여러 개를 한 번에 라이브러리에 씨앗한다. "
                            "저장은 승인 후에만 일어나고, 이름·패턴이 겹치면 건너뛴다."),
            "input_schema": {"type": "object", "properties": {
                "formats": {"type": "array", "maxItems": config.max_format_batch, "items": {
                    "type": "object", "properties": {
                        "name": {"type": "string", "maxLength": 60, "pattern": "^[a-z0-9][a-z0-9-]{0,59}$",
                                 "description": "Library key for this format, lowercase-with-hyphens."},
                        "pattern": {"type": "string", "maxLength": 200, "description": "The raw regex."},
                        "pass_examples": {"type": "array", "maxItems": config.max_format_examples,
                                          "items": {"type": "string", "maxLength": 120},
                                          "description": "Values that MUST match; verified before approval."},
                        "fail_examples": {"type": "array", "maxItems": config.max_format_examples,
                                          "items": {"type": "string", "maxLength": 120},
                                          "description": "Values that MUST NOT match; verified before approval."}},
                        "required": ["name", "pattern"], "additionalProperties": False},
                    "description": "One entry per format; each becomes new, duplicate, or invalid."},
                "summary": {"type": "string", "maxLength": 200, "description": "One line the preview card shows."}},
                "required": ["formats", "summary"], "additionalProperties": False},
        },
        {
            "name": "apply_format_batch",
            "description": ("Apply a format batch the operator approved on the Formats page. It is the only tool "
                            "that adds entries to the shared library — only entries marked new are added; "
                            "duplicate and invalid entries are reported but never added. A batch not marked "
                            "approved by the host is held. / Formats 페이지에서 승인된 배치만 라이브러리에 반영된다."),
            "input_schema": {"type": "object", "properties": {"batch_id": _batch_id()}, "required": ["batch_id"], "additionalProperties": False},
        },
        {
            "name": "discard_format_batch",
            "description": "Discard a staged format batch the operator rejected or replaced.",
            "input_schema": {"type": "object", "properties": {"batch_id": _batch_id()}, "required": ["batch_id"], "additionalProperties": False},
        },
        {
            "name": "get_pending_format_batches",
            "description": "Format batches staged and waiting for approval. / 승인 대기 중인 포맷 배치.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        # -- 값 비교 프로파일 (propose → approve → apply; effective_from 이후에도 과거 판정은 안 건드림) --
        {
            "name": "stage_profile",
            "description": ("Stage an ignore-spec (a ComparisonProfile) for a two-target job's value comparison — "
                            "it judges nothing and re-diffs nothing yet. job_id must be one this conversation staged, "
                            "applied, or listed. Each ignore path is a $-rooted JSON path (e.g. '$.serverTime', "
                            "'$.items[0].id'); per_api_ignore adds paths scoped to one api_id only. Approval is "
                            "host-only, on the Jobs/Profiles page; applying it re-diffs the job's stored response "
                            "bodies with these ignore paths — no new runs are made, and past comparison results "
                            "are never re-judged. / 값 비교의 무시 경로 초안을 스테이징한다. 승인은 호스트 전용이고, "
                            "적용은 저장된 응답 바디를 재-diff할 뿐 새로 실행하지 않는다."),
            "input_schema": {"type": "object", "properties": {
                "job_id": {"type": "string", "description": "job_id staged, applied, or listed by get_pending_jobs this session — the two-target job this profile compares."},
                "ignore_paths": {"type": "array", "maxItems": config.max_ignore_paths,
                                 "items": {"type": "string", "maxLength": 200},
                                 "description": "$-rooted JSON paths to ignore across every API in the job, e.g. '$.serverTime'."},
                "per_api_ignore": {"type": "object", "additionalProperties": {"type": "array", "maxItems": config.max_ignore_paths, "items": {"type": "string", "maxLength": 200}},
                                   "description": "Optional: extra $-rooted ignore paths keyed by api_id, scoped to that API only."},
                "summary": {"type": "string", "maxLength": 200, "description": "One line naming what noise this clears."}},
                "required": ["job_id", "summary"], "additionalProperties": False},
        },
        {
            "name": "apply_profile",
            "description": ("Apply a comparison profile the operator approved on the Jobs/Profiles page. It is the "
                            "only tool that makes a profile effective; a profile not marked approved by the host is "
                            "held. Effective only from this moment forward — past comparison results are never "
                            "re-judged; the target job's parity report is re-diffed from its stored bodies, no new "
                            "runs. / Jobs/Profiles 페이지에서 승인된 프로파일만 effective해진다. 과거 비교 결과는 "
                            "재평가하지 않는다."),
            "input_schema": {"type": "object", "properties": {"profile_id": _profile_id()}, "required": ["profile_id"], "additionalProperties": False},
        },
        {
            "name": "discard_profile",
            "description": "Discard a staged comparison profile the operator rejected or replaced.",
            "input_schema": {"type": "object", "properties": {"profile_id": _profile_id()}, "required": ["profile_id"], "additionalProperties": False},
        },
        {
            "name": "get_pending_profiles",
            "description": "Comparison profiles staged and waiting for approval. / 승인 대기 중인 비교 프로파일.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "recommend_ignore_paths",
            "description": ("Read-only: returns a parity report's noise clusters (paths that differ, and how many "
                            "rows) ranked biggest-first, so you can propose which paths to ignore. Every path and "
                            "count here is what the report already computed — never fabricate a path the report "
                            "did not surface. Propose the biggest cluster(s) as a profile with stage_profile. / "
                            "리포트가 이미 계산한 노이즈 클러스터를 큰 순서로 돌려준다. 판정 없음, 새 실행 없음."),
            "input_schema": {"type": "object", "properties": {
                "job_id": {"type": "string", "description": "job_id whose parity report to read."}},
                "required": ["job_id"], "additionalProperties": False},
        },
    ]

    presentation: list[dict[str, Any]] = [
        {
            "name": DIGEST_TOOL,
            "description": ("Show the 'look first' digest of non-pass runs: each entry by run_id with why it matters; the "
                            "card fills in status, failed rules, API, score, AND the population it was drawn from. Use it "
                            "after rank_failed_runs. Never put pass/fail words in the headline that contradict the record."),
            "input_schema": {"type": "object", "properties": {
                "title": {"type": "string", "maxLength": 80},
                "items": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["fail", "error", "pending_job", "note"]},
                    "ref_id": {"type": "string", "maxLength": 64, "description": "run_id (fail/error) or job_id (pending_job)."},
                    "headline": {"type": "string", "maxLength": 120},
                    "why_it_matters": {"type": "string", "maxLength": 160}},
                    "required": ["kind", "headline"], "additionalProperties": False}}},
                "required": ["items"], "additionalProperties": False},
        },
        {
            "name": GROUPS_TOOL,
            "description": ("Show the groups table for the last aggregate_runs call: pick the group keys it returned "
                            "(in its order unless the operator asked otherwise); the card fills counts, first failure, "
                            "last pass before it, flakiness, regression suspicion and p95 from the host's figures, plus the "
                            "population. Your sentence before it introduces the card without restating any number."),
            "input_schema": {"type": "object", "properties": {
                "title": {"type": "string", "maxLength": 80},
                "group_keys": {"type": "array", "minItems": 1, "maxItems": config.max_group_items, "items": {"type": "string", "maxLength": 200}},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["group_keys"], "additionalProperties": False},
        },
        {
            "name": QUERY_TABLE_TOOL,
            "description": ("Show the table for the last query_runs call: you supply only a title and an optional "
                            "note; the card fills every column, every figure, the population, the group count, the "
                            "window, the source and the spec summary from that result. Call it right after "
                            "query_runs — the numbers belong on the card, not in your sentence."),
            "input_schema": {"type": "object", "properties": {
                "title": {"type": "string", "maxLength": 80, "description": "What this table answers, in the operator's words."},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["title"], "additionalProperties": False},
        },
        {
            "name": PREVIEW_TOOL,
            "description": ("Show the approval card for a job staged or listed earlier; the card fills in APIs, targets, "
                            "schedules, test data, the matrix totals, assumptions, and highlights low-confidence slots. A "
                            "stage call shows this card itself; do not call it for a job staged this turn." if config.stage_shows_preview else
                            "Show the approval card for one staged job. Show every staged job with it before anything is applied."),
            "input_schema": {"type": "object", "properties": {
                "job_id": _job_id(),
                "headline": {"type": "string", "maxLength": 120},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["job_id"], "additionalProperties": False},
        },
        {
            "name": RULE_PREVIEW_TOOL,
            "description": ("Show the approval card for a rule staged or listed earlier; the card fills in the "
                            "parameter, the condition, low-confidence slots, and the impact on recent runs. A "
                            "stage call shows this card itself; do not call it for a rule staged this turn." if config.stage_shows_preview else
                            "Show the approval card for one staged rule. Show every staged rule with it before anything is applied."),
            "input_schema": {"type": "object", "properties": {
                "rule_id": _rule_id(),
                "headline": {"type": "string", "maxLength": 120},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["rule_id"], "additionalProperties": False},
        },
        {
            "name": FORMAT_BATCH_TOOL,
            "description": ("Show the approval card for a format batch staged or listed earlier; the card fills "
                            "in each entry's outcome (new, duplicate, invalid) and the counts. Show every staged "
                            "batch with it before anything is applied."),
            "input_schema": {"type": "object", "properties": {
                "batch_id": _batch_id(),
                "headline": {"type": "string", "maxLength": 120},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["batch_id"], "additionalProperties": False},
        },
        {
            "name": PARITY_SUMMARY_TOOL,
            "description": ("Show a parity job's noise clusters and value/status mismatch counts; the card fills "
                            "in every cluster, path, and count from the stored parity report — you supply only "
                            "job_id, title, and note. Use it after recommend_ignore_paths or after the parity job "
                            "has run."),
            "input_schema": {"type": "object", "properties": {
                "job_id": {"type": "string", "description": "job_id whose parity report to show."},
                "title": {"type": "string", "maxLength": 80},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["job_id"], "additionalProperties": False},
        },
        {
            "name": PROFILE_PREVIEW_TOOL,
            "description": ("Show the approval card for a comparison profile (an ignore-spec) staged or listed "
                            "earlier; the card fills in the ignore paths and the target job from the staging "
                            "record. Show every staged profile with it before anything is applied. Approving it "
                            "applies no new judgment: applying re-diffs the job's stored responses, it never "
                            "re-runs."),
            "input_schema": {"type": "object", "properties": {
                "profile_id": _profile_id(),
                "headline": {"type": "string", "maxLength": 120},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["profile_id"], "additionalProperties": False},
        },
        {
            "name": QUESTION_TOOL,
            "description": ("Ask the operator up to 5 structured questions when an unresolved fact would materially change "
                            "the job (target env, schedule start, FROZEN vs LATE). Prefill every question with your best "
                            "default and say WHY in `why`; put `default` before `options`. After this call, end the turn "
                            "with present_suggestions and wait — the answers come back as a '[form answers — id]' message."),
            "input_schema": QUESTION_FORM_INPUT_SCHEMA,
        },
        {
            "name": NAVIGATE_SCREEN_TOOL,
            "description": ("Move the portal: switch view, open/scroll to one item, set the view's own filter "
                            "(runs: status; apis: query). Changes the screen only — runs, approves, saves nothing. "
                            "Focus ids must come from a tool result or the current screen."),
            "input_schema": {"type": "object", "properties": {
                "view": {"type": "string", "enum": ["home", "apis", "runs", "jobs", "rules"]},
                "focus": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["api", "run", "job", "rule"]},
                    "ref_id": {"type": "string", "maxLength": 64}},
                    "required": ["kind", "ref_id"], "additionalProperties": False},
                "filter": {"type": "object", "properties": {
                    "status": {"type": "string", "enum": ["all", "pass", "fail", "error"]},
                    "query": {"type": "string", "maxLength": 80}},
                    "additionalProperties": False}},
                "required": ["view"], "additionalProperties": False},
        },
        {
            "name": HIGHLIGHT_SCREEN_TOOL,
            "description": ("Draw numbered red boxes on items the operator can see (①②③ in the order given; match "
                            "them in your prose). Use it when your explanation rests on specific on-screen items; "
                            "navigate_screen first if they are on another view. Screen only — nothing is run or "
                            "approved."),
            "input_schema": {"type": "object", "properties": {
                "targets": {"type": "array", "minItems": 1, "maxItems": config.max_highlight_targets,
                            "items": {"type": "object", "properties": {
                                "kind": {"type": "string", "enum": ["api", "run", "job", "rule"]},
                                "ref_id": {"type": "string", "maxLength": 64},
                                "note": {"type": "string", "maxLength": 120}},
                                "required": ["kind", "ref_id"], "additionalProperties": False}},
                "headline": {"type": "string", "maxLength": 80}},
                "required": ["targets"], "additionalProperties": False},
        },
        {
            "name": "present_suggestions",
            "description": (f"Give the turn its 1-4 chips; it ends the reply. Call it in the same round as the turn's last "
                            f"present_* call. Alone, after the text, only on a turn with no other present_* call ({chips_alone_cases})."),
            "input_schema": {"type": "object", "properties": {"suggestions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4}},
                             "required": ["suggestions"], "additionalProperties": False},
        },
    ]

    absent = config.absent_tools()
    tools = [with_status(t, _STATUS_READER) for t in tools if t["name"] not in absent]
    tools += [t for t in presentation if t["name"] not in absent]
    base_names = {t["name"] for t in tools}
    for extension in extra_presentation_tools:
        if extension.name in base_names:
            raise ValueError(f"presentation extension {extension.name!r} collides with a built-in tool")
        base_names.add(extension.name)
        tools.append(extension.tool_definition())
    return tools
