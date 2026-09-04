"""툴 계약, 고정 순서. 목록은 config만의 함수라 매 요청 같은 바이트다. 툴 설명은 그 툴 하나의
규칙만 담고, 툴을 가로지르는 계약(스테이징·판정 금지)은 프롬프트에, 흐름은 스킬에 있다."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from commerce_common.execution import LOAD_SKILL, with_status
from commerce_common.presentation import PresentationExtension

from ..config import AtworksAgentConfig
from ..question_form import QUESTION_FORM_INPUT_SCHEMA
from .presentation import DIGEST_TOOL, PREVIEW_TOOL, QUESTION_TOOL

_STATUS_READER = "the operator"
_SESSION_API_ID = "api_id that search_apis or get_api returned this session."
_ISO_DATETIME = "ISO 8601 datetime with offset, e.g. 2026-08-27T00:00:00+09:00."


def _job_id() -> dict[str, Any]:
    return {"type": "string", "description": "job_id staged this conversation or listed by get_pending_jobs."}


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
                            "/ 등록된 API를 검색한다. '지난 1주일 업데이트'는 updated_after로 푼다."),
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
            "description": ("Run results, newest first, filtered by time window, status (pass|fail|error), or api_id. "
                            "It also records the total count for the window (the digest's population). The status on each "
                            "run is the deterministic verdict; never restate it as your own judgment. / 실행 이력. "
                            "status는 결정론 판정이다."),
            "input_schema": {"type": "object", "properties": {
                "since": {"type": "string", "description": _ISO_DATETIME},
                "status": {"type": "string", "enum": ["pass", "fail", "error"]},
                "api_id": {"type": "string", "description": _SESSION_API_ID},
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
        # -- 실행 계획 ------------------------------------------------------------------
        {
            "name": "get_pending_jobs",
            "description": "Jobs staged and waiting for approval. / 승인 대기 중인 실행 계획.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "stage_job",
            "description": ("Stage an execution plan (JobSpec) for the operator's approval — it runs nothing. api_ids must come "
                            "from search_apis/get_api this session. Every value you defaulted (target_env, schedule start, "
                            "binding) goes into assumptions with a confidence below 0.5, so the preview asks the operator. "
                            "With stage_shows_preview the call renders its own preview card. / 실행 계획을 스테이징한다. "
                            "실행하지 않는다. 기본값으로 채운 슬롯은 assumptions+낮은 confidence로 표시한다."),
            "input_schema": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["run_now", "scheduled_run"]},
                "summary": {"type": "string", "maxLength": 200, "description": "One line the preview card shows."},
                "api_ids": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "string", "description": _SESSION_API_ID}},
                "target_env": {"type": "string", "description": "dev | stg. Never prod. When the operator did not say, default dev with confidence 0.3."},
                "schedule": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["once", "daily"]},
                    "at": {"type": "string", "pattern": "^\\d{2}:\\d{2}$"},
                    "tz": {"type": "string"},
                    "from_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "First run date. If today's time-of-day has passed, tomorrow — and say so in assumptions."},
                    "count": {"type": "integer", "minimum": 1, "maximum": 30}},
                    "required": ["kind", "at", "from_date", "count"], "additionalProperties": False},
                "binding": {"type": "string", "enum": ["FROZEN", "LATE"], "description": "FROZEN: today's resolved api_ids every run. LATE: re-evaluate select_where each run. Ambiguous from speech — ask via present_question_form or set confidence 0.4."},
                "select_where": {"type": "object", "description": "The search that produced api_ids (query/group/updated_after), kept for LATE binding and for the preview's provenance.", "additionalProperties": True},
                "report": {"type": "boolean"},
                "confidence": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}, "description": "Per-slot confidence: target_env, schedule.from_date, binding, api_ids."},
                "assumptions": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 160}}},
                "required": ["kind", "summary", "api_ids", "target_env"], "additionalProperties": False},
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
            "name": PREVIEW_TOOL,
            "description": ("Show the approval card for a job staged or listed earlier; the card fills in APIs, target, "
                            "schedule, assumptions, and highlights low-confidence slots. A stage call shows this card itself; "
                            "do not call it for a job staged this turn." if config.stage_shows_preview else
                            "Show the approval card for one staged job. Show every staged job with it before anything is applied."),
            "input_schema": {"type": "object", "properties": {
                "job_id": _job_id(),
                "headline": {"type": "string", "maxLength": 120},
                "note": {"type": "string", "maxLength": 200}},
                "required": ["job_id"], "additionalProperties": False},
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
