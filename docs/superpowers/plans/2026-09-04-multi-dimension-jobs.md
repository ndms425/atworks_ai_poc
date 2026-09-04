# Multi-dimension Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Read "Global Constraints" and "Review gates" before Task 1; they apply to every task implicitly.**

**Goal:** Turn a job from "one API set × one environment × at most one schedule × no data" into a **matrix** — `api_ids × target_envs × test_data`, executed once per schedule occurrence, approved with one click, reported with the environments side by side — so that "오늘 업데이트한 API를 개발서버와 이관서버에서 동일한 테스트 데이터로 수행하고 결과를 비교해줘" stages as one job.

**Architecture:** The three dimensions stay as three lists on `JobSpec`/`JobDraft` (approach A — not flattened cells, not a job group). Everything that bounds the job stays in the one place it already lives: `check_job_guardrails`, run at stage time and again at apply time. Bookkeeping moves from a single `runs_remaining` counter to `JobSpec.executions` plus a per-schedule `JobSchedule.done`, so several schedules on one job advance independently and `remaining_executions` is derived, never stored. `execute_job_once` grows an env × data loop inside one call, so "one execution" still means exactly one `record_execution`. The comparison lives in the report (`data.json.matrix` + a template table), not in chat.

**Tech Stack:** Python 3.11+, pydantic v2, `commerce_common` (imported, never edited), FastAPI, pytest, ruff. Web: Next.js 16 + React 19 + TypeScript + Tailwind 4, Node 24.

**Spec:** `docs/superpowers/specs/2026-09-04-multi-dimension-jobs-design.md` (authority). Impact analysis with the 45 assumption sites, safety invariants and the test inventory: `docs/superpowers/specs/2026-09-04-multi-dimension-jobs-impact.md`. Both travel with this plan; executors read all three.

---

## Global Constraints

- **Platform:** Windows 11, Git Bash. Python is run through the venv explicitly: `.venv/Scripts/python.exe -m pytest ...` and `.venv/Scripts/python.exe -m ruff check .`. Node 24; the web builds with `cd web && npm run build`.
- **File I/O is UTF-8 everywhere.** Every `read_text`/`write_text` in this codebase passes `encoding="utf-8"`; Korean is a first-class input and a cp949 default corrupts it.
- **Never modify `refs/`** (the reference clones are read-only; needed changes go in `atworks_agent`) **and never modify `web/web-shared/`** (the shared portal shell is a verbatim copy).
- **Baseline:** 136 pytest tests pass and ruff is clean at the start. Every task ends with `ruff check .` clean and the **full** suite green; each task below states the expected count, which grows to **168** by Task 9.
- **Tool and component names are prompt/UI contracts and do not change:** `stage_job`, `apply_job`, `discard_job`, `get_pending_jobs`, `present_job_preview` → component `job_preview`, `present_run_digest` → `run_digest`, `present_question_form` → `question_form`, `present_suggestions`. The web's `GenerativeBlock` switch is bound to these strings.
- **The `stage_job` input schema changes exactly as spec §2.1 says and stays a pure function of config.** Every new cap (`maxItems` on `target_envs`, `schedules`, `test_data`) reads from an `AtworksAgentConfig` field — never a literal, never runtime state. `test_registry.py::test_same_config_same_bytes` must keep passing unchanged.
- **Guardrails run at stage and at apply (two-phase).** Every new rule goes into `check_job_guardrails` and nowhere else, so both phases and both call sites (`JobLedger.stage`/`JobLedger.apply`, `gates.check_apply_job`) pick it up for free. Never re-implement a cap in the executor or the backend.
- **`prod` protection:** the allow-list check collects **every** offending env with `[e for e in target_envs if e not in allowed]`. Never `all(...)`, never `target_envs[0]`.
- **The scheduler never imports the model.** `host/atworks_host/scheduler.py` keeps importing only `atworks_agent` types and the backend ABC; `due_at`/`tick` stay pure arithmetic over stored fields.
- **Approval marks are written only by `app.job_action`** in `host/atworks_host/app.py`. Nothing in this plan adds a second writer of `approved_job_ids`.
- **No alias period.** The web and the core ship in this same plan, so `job_record` drops `target_env`/`schedule`/`runs_remaining` outright (spec §4.5). Tasks 1–7 leave `web/atworks-web` type-inconsistent on purpose; Task 8 fixes it and is the first task that runs `npm run build`.
- **Commit messages:** `feat|fix|test|chore(scope): ...`, at least one commit per task, and every commit ends with the trailer:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  ```
- **Out of scope** (spec §7, do not build): partial approval of a matrix, a chat comparison card (`present_env_comparison`), a `get_job_results` read tool, dataset identifiers / file upload, parameter-value lookup, per-env verdict rules in aTworks. `.env.example` is not touched by this plan.

## Review gates

Two mid-plan gates plus a final one, mirroring Part H of the first plan (`docs/superpowers/plans/2026-09-03-atworks-ai-chat-mvp.md`):

| When | What |
|---|---|
| After Task 3's commit | `/review-commerce-agent atworks-agent/core` (Steps 1–3 only — produce the row-by-row comparison table, do not convert) **plus** `superpowers:requesting-code-review` over Tasks 1–3. The rows that matter here: Rules (guardrail placement), Tools (schema is a function of config), Writes (staged → guardrail → approval). |
| After Task 7's commit | The same two, this time including `host/`. The rows that matter: Writes ("who applies, and how does the code know"), UI (server-side enrichment), Figures (no model-produced numbers on the report). |
| After Task 9's commit | `superpowers:requesting-code-review` over the whole branch, then `superpowers:finishing-a-development-branch`. |

`/review-commerce-agent` appends to `CLAUDE.md`'s `## Commerce agent decision record`; leave what it writes there.

---

## File structure

Files this plan touches, and what each is responsible for after the change:

| File | Responsibility after this plan |
|---|---|
| `atworks-agent/core/atworks_agent/types.py` | `TestDataSet`; `JobSchedule.done`; `JobSpec` dimension lists + the four derived properties; `RunResult.test_data_label` |
| `atworks-agent/core/atworks_agent/jobs.py` | `JobDraft` dimension lists; all 8 guardrail rules in one function; `JobLedger` with per-schedule execution bookkeeping |
| `atworks-agent/core/atworks_agent/config.py` | The four new caps as config fields (guardrail values, and the source of the tool schema's caps) |
| `atworks-agent/core/atworks_agent/gates.py` | `JobDraft` rebuild from a `JobSpec` at apply time; the follow-through reminder's slot list |
| `atworks-agent/core/atworks_agent/executor.py` | `_stage_job` parses/sanitizes the three dimensions and runs the guardrail (with the API catalogue) before the backend call |
| `atworks-agent/core/atworks_agent/tools/registry.py` | `stage_job`'s array-shaped schema, caps wired to config |
| `atworks-agent/core/atworks_agent/enrichment.py` | `enrich_job_preview` adds the server-computed `matrix` block |
| `atworks-agent/core/atworks_agent/prompt.py` | The two new static job-contract sentences |
| `atworks-agent/core/atworks_agent/backend.py` | `execute_job_once(..., schedule_index)` and `record_execution(..., schedule_index)` contracts |
| `atworks-agent/skills/schedule-run/SKILL.md` | Envs / schedules / test data as list-valued slots; where the comparison appears |
| `atworks-agent/skills/job-approval/SKILL.md` | One sentence on approval scope |
| `host/atworks_host/mock_backend.py` | `stub_verdict(api, env, data)`; the env × data × api execution loop |
| `host/atworks_host/scheduler.py` | `due_at(schedule, index)`; multi-schedule `tick` |
| `host/atworks_host/reports.py` | `data.json` gains `matrix` and `summary.by_env` |
| `host/atworks_host/report_template.html` | Comparison table + 차이 tile above the run list; env and 데이터 columns |
| `web/atworks-web/lib/types.ts` + 3 components | The TS mirror and the three views/cards that render lists |
| `scripts/smoke_chat.py`, `docs/safety.md`, `README.md` | Turn 4, the guardrail/approval rows, manual scenario ④ |

---

### Task 1: Data model — the three dimensions, derived counters, and the sweep that keeps the suite green

**Files:**
- Modify: `atworks-agent/core/atworks_agent/types.py:78-135` (JobSchedule, JobSpec), `:33-43` (RunResult)
- Modify: `atworks-agent/core/atworks_agent/jobs.py:34-77` (JobDraft, guardrails), `:80-158` (JobLedger)
- Modify: `atworks-agent/core/atworks_agent/gates.py:82-84` (JobDraft rebuild)
- Modify: `atworks-agent/core/atworks_agent/executor.py:249-272` (`_stage_job`)
- Modify: `atworks-agent/core/atworks_agent/__init__.py` (re-export `TestDataSet`)
- Modify: `host/atworks_host/mock_backend.py:97-139`, `host/atworks_host/scheduler.py:17-71`, `host/atworks_host/report_template.html:20`
- Test: `atworks-agent/core/tests/test_types.py`, `test_jobs.py`, `test_gates.py`, `test_executor.py`, `test_presentation.py`, `conftest.py`; `atworks-agent/runtime/tests/conftest.py`, `test_orchestrator.py`; `host/tests/test_app.py`, `test_mock_backend.py`, `test_scheduler_reports.py`

**Interfaces:**
- Produces: `TestDataSet(label: str, values: dict[str, str])`; `JobSchedule.done: int`; `JobSpec.target_envs: list[str]`, `.schedules: list[JobSchedule]`, `.test_data: list[TestDataSet]`, `.executions: int`, and the read-only properties `.matrix_size: int`, `.total_executions: int`, `.remaining_executions: int`, `.runs_total: int`; `RunResult.test_data_label: str | None`; `JobDraft(kind, summary, api_ids, target_envs, schedules, test_data, select_where, binding, report, confidence, assumptions)`; `JobLedger.record_execution(job_id: str, run_ids: list[str], schedule_index: int | None) -> JobSpec`
- Removes: `JobSpec.target_env`, `JobSpec.schedule`, `JobSpec.runs_remaining`, `JobDraft.target_env`, `JobDraft.schedule`

> **Note for the implementer:** `atworks-agent/core/tests/conftest.py` and `atworks-agent/runtime/tests/conftest.py` are **byte-identical duplicates** (`InMemoryBackend`). Every edit to one must be applied verbatim to the other; `diff` them before committing.

- [ ] **Step 1: Write the failing tests**

Append to `atworks-agent/core/tests/test_types.py` (and change the existing `test_jobspec_defaults_are_staged_and_frozen` to pass `target_envs=["dev"]` instead of `target_env="dev"`). Add `import pytest` as a top-level import between the `datetime` line and the `atworks_agent.types` import, and add `TestDataSet` to that import list — the file's existing function-local `import pytest` lines can stay or go, isort only cares about the top block:

```python


def test_test_data_set_bounds_its_label_and_values():
    ok = TestDataSet(label="S1 정상", values={"amount": "1000"})
    assert ok.values["amount"] == "1000"
    with pytest.raises(ValueError):
        TestDataSet(label="", values={"amount": "1"})
    with pytest.raises(ValueError):
        TestDataSet(label="S1 정상", values={})
    with pytest.raises(ValueError):
        TestDataSet(label="<script>", values={"amount": "1"})


def test_test_data_set_rejects_an_oversized_key_or_value():
    with pytest.raises(ValueError):
        TestDataSet(label="S1", values={"k" * 61: "1"})
    with pytest.raises(ValueError):
        TestDataSet(label="S1", values={"amount": "9" * 201})


def test_jobspec_matrix_properties_multiply_the_dimensions():
    job = JobSpec(job_id="job-0001", kind=JobKind.SCHEDULED_RUN, summary="s",
                  api_ids=["api-001", "api-002"], target_envs=["dev", "stg"],
                  schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3),
                             JobSchedule(kind="once", at="09:00", from_date="2026-09-08", count=1)],
                  test_data=[TestDataSet(label="S1", values={"amount": "1"}),
                             TestDataSet(label="S2", values={"amount": "-1"})],
                  created_at=datetime.now(UTC), created_by="op")
    assert job.matrix_size == 8          # 2 apis × 2 envs × 2 data sets
    assert job.total_executions == 4     # 3 + 1
    assert job.remaining_executions == 4
    assert job.runs_total == 32


def test_jobspec_without_schedules_or_data_runs_its_matrix_once():
    job = JobSpec(job_id="job-0002", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"],
                  target_envs=["dev"], created_at=datetime.now(UTC), created_by="op", executions=1)
    assert job.matrix_size == 1 and job.total_executions == 1
    assert job.remaining_executions == 0 and job.runs_total == 1


def test_remaining_executions_never_goes_below_zero():
    job = JobSpec(job_id="job-0003", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"],
                  target_envs=["dev"], created_at=datetime.now(UTC), created_by="op", executions=5)
    assert job.remaining_executions == 0


def test_run_result_carries_the_data_set_it_was_bound_to():
    bound = RunResult(run_id="run-1", api_id="api-001", executed_at=datetime.now(UTC),
                      target_env="stg", status=RunStatus.PASS, test_data_label="S2 음수 금액")
    assert bound.test_data_label == "S2 음수 금액"
    unbound = RunResult(run_id="run-2", api_id="api-001", executed_at=datetime.now(UTC),
                        target_env="dev", status=RunStatus.PASS)
    assert unbound.test_data_label is None
```

In `atworks-agent/core/tests/test_jobs.py`, replace the `_draft` helper and the two schedule/env tests, and add two new ones:

```python
from atworks_agent.types import ActorKind, Binding, JobKind, JobSchedule, JobStatus, TestDataSet

CFG = AtworksAgentConfig(model="m", max_apis_per_job=3, allowed_target_envs=("dev", "stg"))


def _draft(**over):
    base = dict(kind=JobKind.RUN_NOW, summary="run", api_ids=["a", "b"], target_envs=["dev"],
                schedules=[], test_data=[], report=True)
    base.update(over)
    return JobDraft(**base)


def test_guardrail_target_env_protected():
    v = check_job_guardrails(_draft(target_envs=["prod"]), CFG)
    assert any("prod" in m and "not allowed targets" in m for m in v)


def test_guardrail_flags_every_bad_env_not_just_the_first():
    v = check_job_guardrails(_draft(target_envs=["dev", "prod", "qa"]), CFG)
    assert any("prod" in m and "qa" in m and "not allowed targets" in m for m in v)


def test_guardrail_schedule_count():
    cfg = AtworksAgentConfig(model="m", max_schedule_count=3)
    sched = JobSchedule(kind="daily", at="09:00", from_date="2026-09-04", count=5)
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[sched]), cfg)
    assert any("5 runs" in m and "limit is 3" in m for m in v)


def test_record_execution_counts_executions_not_runs():
    cfg = AtworksAgentConfig(model="m", max_schedule_count=3)
    ledger = JobLedger(cfg)
    sched = JobSchedule(kind="daily", at="09:00", from_date="2026-09-04", count=3)
    job = ledger.stage(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[sched]), actor="op")
    assert job.remaining_executions == 3
    ledger.apply(job.job_id, actor="op")
    after_first = ledger.record_execution(job.job_id, ["run-0031", "run-0032"], 0)
    assert after_first.run_ids == ["run-0031", "run-0032"] and after_first.remaining_executions == 2
    after_second = ledger.record_execution(job.job_id, ["run-0033", "run-0034"], 0)
    assert len(after_second.run_ids) == 4 and after_second.remaining_executions == 1


def test_record_execution_advances_only_the_schedule_it_consumed():
    ledger = JobLedger(CFG)
    # `done=2` on the draft is the model's invention; the ledger owns that counter and resets it.
    daily = JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3, done=2)
    once = JobSchedule(kind="once", at="09:00", from_date="2026-09-08", count=1)
    job = ledger.stage(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[daily, once]), actor="op")
    assert job.executions == 0 and [s.done for s in job.schedules] == [0, 0]
    assert job.total_executions == 4
    ledger.apply(job.job_id, actor="op")
    after = ledger.record_execution(job.job_id, ["run-0031", "run-0032"], 1)
    assert after.executions == 1 and after.remaining_executions == 3
    assert [s.done for s in after.schedules] == [0, 1]
    after2 = ledger.record_execution(job.job_id, ["run-0033"], 0)
    assert after2.executions == 2 and [s.done for s in after2.schedules] == [1, 1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest atworks-agent/core/tests/test_types.py atworks-agent/core/tests/test_jobs.py -v`
Expected: FAIL — `ImportError: cannot import name 'TestDataSet' from 'atworks_agent.types'`, and the `JobSpec(... target_envs=...)` constructions raise `ValidationError: Unexpected keyword argument`.

- [ ] **Step 3: Implement `types.py`**

In `atworks-agent/core/atworks_agent/types.py`, add `ConfigDict` to the pydantic import line:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

Add `done` to `JobSchedule` (right after `count`, before the validators):

```python
class JobSchedule(BaseModel):
    kind: Literal["once", "daily"]
    at: str = Field(pattern=r"^\d{2}:\d{2}$")   # "09:00"
    tz: str = "Asia/Seoul"
    from_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    count: int = Field(ge=1, le=30)
    done: int = Field(default=0, ge=0)   # occurrences already executed; the ledger owns it
```

Insert `TestDataSet` immediately after `JobSchedule` and before `JobSpec`:

```python
class TestDataSet(BaseModel):
    """One named parameter binding, applied identically to every environment in the job's
    matrix. These values are inputs the operator approves on the preview card — never facts
    about the system. The model may propose a set, but every invented value belongs in
    ``JobSpec.assumptions`` with a low confidence."""
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.\-가-힣 ]+$")
    values: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_and_values_are_bounded(self) -> TestDataSet:
        for key, value in self.values.items():
            if not 1 <= len(key) <= 60:
                raise ValueError(f"test data key must be 1-60 chars, got {len(key)} for {key[:20]!r}")
            if len(value) > 200:
                raise ValueError(f"test data value for {key!r} must be at most 200 chars, got {len(value)}")
        return self
```

Add `test_data_label` to `RunResult`, after `target_env`:

```python
class RunResult(BaseModel):
    """실행 1건. status는 결정론 DSL(Mock에선 스텁)이 낸 값이고 LLM은 이 값을 바꾸지 못한다."""
    run_id: str
    api_id: str
    executed_at: datetime
    target_env: str
    test_data_label: str | None = None   # which TestDataSet was bound; None = no binding
    status: RunStatus
    failed_rules: list[str] = Field(default_factory=list)
    http_status: int | None = None
    duration_ms: int | None = None
    job_id: str | None = None
```

Replace `JobSpec`'s three scalar fields and `runs_remaining`, and add the derived properties:

```python
class JobSpec(BaseModel):
    """LLM이 초안을 잡고 사람이 승인하는 실행 계획. StagedChange 미러.
    ``confidence``는 슬롯별 확신도(0~1)로 승인 카드가 낮은 항목을 강조하는 데 쓴다.
    ``assumptions``는 LLM이 기본값으로 채운 슬롯의 설명이다.
    한 job은 매트릭스다: 스케줄 1회마다 ``api_ids × target_envs × test_data`` 전체를 실행한다."""
    job_id: str
    kind: JobKind
    status: JobStatus = JobStatus.STAGED
    summary: str = Field(max_length=200)
    api_ids: list[str] = Field(default_factory=list)
    select_where: dict[str, Any] | None = None
    binding: Binding = Binding.FROZEN
    target_envs: list[str] = Field(min_length=1)
    schedules: list[JobSchedule] = Field(default_factory=list)   # empty ⇒ run_now, one execution
    test_data: list[TestDataSet] = Field(default_factory=list)   # empty ⇒ no binding
    report: bool = True
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list)
    created_at: datetime
    created_by: str
    created_by_kind: ActorKind = ActorKind.OPERATOR
    applied_at: datetime | None = None
    applied_by: str | None = None
    discarded_at: datetime | None = None
    discarded_by: str | None = None
    discarded_by_kind: ActorKind | None = None
    run_ids: list[str] = Field(default_factory=list)
    executions: int = Field(default=0, ge=0)   # executions performed; run_now completes at 1

    # -- derived: pure functions of the stored fields, never persisted -------------------

    @property
    def matrix_size(self) -> int:
        """Runs one execution produces."""
        return len(self.api_ids) * len(self.target_envs) * max(1, len(self.test_data))

    @property
    def total_executions(self) -> int:
        """Executions the job is approved for; a job with no schedules runs its matrix once."""
        return sum(s.count for s in self.schedules) if self.schedules else 1

    @property
    def remaining_executions(self) -> int:
        return max(self.total_executions - self.executions, 0)

    @property
    def runs_total(self) -> int:
        return self.matrix_size * self.total_executions
```

Re-export the new type from `atworks-agent/core/atworks_agent/__init__.py` — add `TestDataSet` to the alphabetical `.types` import list (between `RunStatus` and the closing paren):

```python
from .types import (
    ActorKind,
    ApiSpec,
    AttachedItem,
    AtworksSessionContext,
    AtworksSessionState,
    Binding,
    FailedRank,
    JobKind,
    JobSchedule,
    JobSpec,
    JobStatus,
    RunResult,
    RunStatus,
    TestDataSet,
)
```

- [ ] **Step 4: Implement `jobs.py`**

In `atworks-agent/core/atworks_agent/jobs.py`, extend the imports:

```python
from .types import ActorKind, Binding, JobKind, JobSchedule, JobSpec, JobStatus, TestDataSet
```

Replace `JobDraft`:

```python
class JobDraft(BaseModel):
    """stage_job 툴 입력이 검증·정규화된 뒤의 모양. 백엔드는 이걸 받아 JobSpec을 만든다."""
    kind: JobKind
    summary: str = Field(max_length=200)
    api_ids: list[str]
    target_envs: list[str] = Field(min_length=1)
    schedules: list[JobSchedule] = Field(default_factory=list)
    test_data: list[TestDataSet] = Field(default_factory=list)
    select_where: dict[str, Any] | None = None
    binding: Binding = Binding.FROZEN
    report: bool = True
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("target_envs")
    @classmethod
    def _each_env_is_short(cls, value: list[str]) -> list[str]:
        for env in value:
            if len(env) > 32:
                raise ValueError(f"each target env is at most 32 chars, got {len(env)}")
        return value
```

(add `field_validator` to the pydantic import: `from pydantic import BaseModel, ConfigDict, Field, field_validator`)

Replace the three scalar guardrail blocks in `check_job_guardrails` (the `target_env`, `SCHEDULED_RUN` and `RUN_NOW` blocks) with their list forms; the api-count, empty-selection and LATE rules stay exactly as they are:

```python
    # Rule 1 — every offending env is named. Never all(...) and never a check of one element:
    # a single prod anywhere in the list must block the whole job.
    bad_envs = [e for e in draft.target_envs if e not in config.allowed_target_envs]
    if bad_envs:
        violations.append(
            f"target_envs {', '.join(bad_envs)} are not allowed targets "
            f"({', '.join(config.allowed_target_envs)}); the assistant may never target them"
        )
    if draft.kind is JobKind.SCHEDULED_RUN:
        if not draft.schedules:
            violations.append("a scheduled_run needs at least one schedule")
        if not config.enable_scheduling:
            violations.append("scheduling is switched off for this deployment")
    if draft.kind is JobKind.RUN_NOW and draft.schedules:
        violations.append("run_now must not carry schedules — use scheduled_run")
    scheduled_runs = sum(s.count for s in draft.schedules)
    if scheduled_runs > config.max_schedule_count:
        violations.append(
            f"schedules total {scheduled_runs} runs and the limit is {config.max_schedule_count} per job; shorten them"
        )
```

In `JobLedger.stage`, build the new `JobSpec` fields (replacing `target_env=`, `schedule=` and `runs_remaining=`):

```python
            target_envs=list(draft.target_envs),
            # A draft may carry a `done` the model invented; the ledger owns that counter.
            schedules=[s.model_copy(update={"done": 0}) for s in draft.schedules],
            test_data=[d.model_copy() for d in draft.test_data],
            report=draft.report,
            confidence=dict(draft.confidence),
            assumptions=list(draft.assumptions),
            created_at=datetime.now(UTC),
            created_by=actor,
            created_by_kind=actor_kind,
            executions=0,
```

In `JobLedger.apply`, rebuild the draft from the new fields:

```python
        draft = JobDraft(kind=job.kind, summary=job.summary, api_ids=job.api_ids,
                         target_envs=job.target_envs, schedules=job.schedules,
                         test_data=job.test_data, select_where=job.select_where,
                         binding=job.binding, report=job.report)
```

Replace `record_execution`:

```python
    def record_execution(self, job_id: str, run_ids: list[str], schedule_index: int | None) -> JobSpec:
        """One execution of the job's matrix happened and produced these runs. ``executions``
        counts executions (the schedules' counts summed, or 1 for run_now), never individual
        runs; ``schedule_index`` names which schedule occurrence was consumed — without it
        several schedules on one job could not advance independently. Exactly one call per
        execution, even when the execution produced nothing."""
        job = self._jobs[job_id]
        update: dict[str, Any] = {
            "run_ids": [*job.run_ids, *run_ids],
            "executions": job.executions + 1,
        }
        if schedule_index is not None and 0 <= schedule_index < len(job.schedules):
            schedules = list(job.schedules)
            consumed = schedules[schedule_index]
            schedules[schedule_index] = consumed.model_copy(update={"done": consumed.done + 1})
            update["schedules"] = schedules
        updated = job.model_copy(update=update)
        self._jobs[job_id] = updated
        return updated
```

- [ ] **Step 5: Implement the `gates.py` and `executor.py` call sites**

`atworks-agent/core/atworks_agent/gates.py`, in `check_apply_job`, replace the draft rebuild:

```python
    draft = JobDraft(kind=known.kind, summary=known.summary, api_ids=known.api_ids,
                     target_envs=known.target_envs, schedules=known.schedules,
                     test_data=known.test_data, select_where=known.select_where,
                     binding=known.binding, report=known.report)
```

`atworks-agent/core/atworks_agent/executor.py`, replace the body of `_stage_job` (lines 249–272) with:

```python
    async def _stage_job(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api_ids = list(dict.fromkeys(str(a) for a in (_coerce_list(tool_input.get("api_ids")) or [])))
        if held := check_api_provenance(self._state, api_ids):
            return held
        select_where = tool_input.get("select_where")
        if select_where is not None:
            select_where = parse_argument(SelectWhere, select_where).model_dump(mode="json", exclude_none=True)
        # Envs are sanitized to the per-item cap and de-duplicated the way api_ids are: a
        # repeated env is a harmless restatement, and an over-long one is trimmed here so the
        # guardrail message names a readable value instead of a wall of text. The trimmed
        # value still has to be on the allow-list, so nothing slips through by being long.
        target_envs = list(dict.fromkeys(
            self._sanitize(e, 32) for e in (_coerce_list(tool_input.get("target_envs")) or [])
        ))
        # Test-data labels, keys and values are model-authored strings that end up on a card
        # and in a report: sanitize each through the fence before pydantic validates them.
        # A label that had to be truncated or scrubbed carries the fence's "...[truncated]" /
        # "[removed]" marker, whose brackets TestDataSet.label's pattern rejects — an
        # over-long or hostile label surfaces as a named invalid-arguments error rather than
        # being quietly mangled into something the operator then approves.
        test_data = [
            {
                "label": self._sanitize(item.get("label"), 40),
                "values": {
                    self._sanitize(key, 60): self._sanitize(value, 200)
                    for key, value in (item.get("values") or {}).items()
                },
            }
            for item in (_coerce_list(tool_input.get("test_data")) or [])
            if isinstance(item, dict)
        ]
        draft = parse_argument(JobDraft, {
            "kind": tool_input.get("kind"), "summary": self._sanitize(tool_input.get("summary"), 200),
            "api_ids": api_ids,
            "target_envs": target_envs,
            "schedules": _coerce_list(tool_input.get("schedules")) or [],
            "test_data": test_data,
            "select_where": select_where,
            "binding": tool_input.get("binding") or "FROZEN", "report": tool_input.get("report", True),
            "confidence": tool_input.get("confidence") or {},
            "assumptions": [self._sanitize(a, 160) for a in (_coerce_list(tool_input.get("assumptions")) or [])][:6],
        })
        if violations := check_job_guardrails(draft, self._config):
            return ToolOutcome.held(GUARDRAIL_GATE, guardrail_block_message(violations))
        job = await self._backend.stage_job(self._session, draft, ActorKind.AGENT)
        return await self._remember_and_preview(job)
```

- [ ] **Step 6: Sweep the host so it compiles and behaves as before**

`host/atworks_host/mock_backend.py` — `execute_job_once` loops the envs (the data loop and the new verdict arrive in Task 5), and `record_execution` forwards a `None` index for now:

```python
    async def record_execution(self, session, job_id, run_ids):
        return self.ledger.record_execution(job_id, run_ids, None)
```

```python
    async def execute_job_once(self, session, job_id) -> list[RunResult]:
        job = self.ledger.get(job_id)
        if job is None:
            return []
        if job.remaining_executions <= 0:
            return []
        api_ids = job.api_ids
        if job.binding is Binding.LATE and job.select_where:
            w = job.select_where
            api_ids = [a.api_id for a in await self.search_apis(
                session, query=w.get("query", ""), group=w.get("group"),
                updated_after=datetime.fromisoformat(w["updated_after"]) if w.get("updated_after") else None,
                limit=self._config.max_apis_per_job + 1)]
            if len(api_ids) > self._config.max_apis_per_job:
                self.ledger.add_guardrail_note(
                    job_id,
                    f"execution skipped: LATE selection resolved to {len(api_ids)} APIs, "
                    f"above the limit of {self._config.max_apis_per_job}",
                )
                # the slot is spent even though nothing ran, so a scheduled job does not
                # retry the same over-limit selection forever
                self.ledger.record_execution(job_id, [], None)
                return []
        produced: list[RunResult] = []
        for env in job.target_envs:
            for api_id in api_ids:
                api = self.apis.get(api_id)
                if api is None:
                    continue
                self._run_seq += 1
                status, rules, http = stub_verdict(api, self._run_seq)
                run = RunResult(run_id=f"run-{self._run_seq:04d}", api_id=api_id, executed_at=datetime.now(UTC),
                                target_env=env, status=status, failed_rules=rules, http_status=http,
                                duration_ms=100 + self._run_seq % 50, job_id=job_id)
                self.runs[run.run_id] = run
                produced.append(run)
        self.ledger.record_execution(job_id, [r.run_id for r in produced], None)
        return produced
```

`host/atworks_host/scheduler.py` — `due_at` reads the first schedule and the occurrence index comes straight off `executions` (Task 6 replaces both):

```python
def due_at(job: JobSpec, index: int) -> datetime | None:
    """index번째(0부터) 회차의 예정 시각. run_now는 시각이 없다(승인 즉시, tick이 따로 본다)."""
    if job.kind is JobKind.RUN_NOW:
        return None
    s = job.schedules[0] if job.schedules else None
    if s is None or index >= s.count:
        return None
    hh, mm = (int(x) for x in s.at.split(":"))
    first = datetime.fromisoformat(s.from_date).replace(hour=hh, minute=mm, tzinfo=ZoneInfo(s.tz))
    return first + timedelta(days=index) if s.kind == "daily" else (first if index == 0 else None)
```

and in `tick`, replace the two `runs_remaining` reads and the `record_execution` call:

```python
                if job.status is not JobStatus.APPLIED or job.remaining_executions <= 0:
                    continue
                index = job.executions
```
```python
                    await self.backend.record_execution(self.session, job.job_id, [])
```

`host/atworks_host/report_template.html` line 20 — the meta line reads the list:

```javascript
document.getElementById('meta').textContent=`${d.job.job_id} · target ${d.job.target_envs.join(', ')} · ${d.job.api_ids.length} APIs · runs so far ${d.job.run_ids.length}`;
```

- [ ] **Step 7: Sweep the tests**

Apply these mechanical rewrites (nothing else in these files changes):

- **`atworks-agent/core/tests/conftest.py` and `atworks-agent/runtime/tests/conftest.py` (identical edits in both):** in `InMemoryBackend.record_execution`, call `self.ledger.record_execution(job_id, run_ids, None)`.
- **`atworks-agent/core/tests/test_gates.py`:** `_job` becomes
  ```python
  def _job(job_id="job-0001", env="dev"):
      return JobSpec(job_id=job_id, kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"],
                     target_envs=[env], created_at=datetime.now(UTC), created_by="op")
  ```
- **`atworks-agent/core/tests/test_executor.py`:** every `"target_env": "dev"` / `"prod"` in a `stage_job` input becomes `"target_envs": ["dev"]` / `["prod"]`; `"confidence": {"target_env": 0.3}` becomes `{"target_envs": 0.3}` and the matching assertion becomes `== ["target_envs"]`; `PermissiveBackend.stage_job` builds `JobSpec(..., target_envs=draft.target_envs, ...)`. Replace `test_stage_job_rejects_an_oversized_target_env` with:
  ```python
  async def test_stage_job_truncates_an_oversized_target_env_then_the_guardrail_blocks_it(backend, config, skills, session, state):
      ex = _exec(backend, config, skills, session, state)
      await ex.execute("search_apis", {"query": ""})
      out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                            "target_envs": ["x" * 200]})
      assert out.blocked == "guardrail"
      assert "not allowed targets" in out.result_text
      assert "x" * 33 not in out.result_text   # sanitized to 32 chars before the message is built
  ```
- **`atworks-agent/core/tests/test_presentation.py`:** the staged `JobSpec` in `test_job_preview_joins_staged_record` uses `target_envs=["dev"]`, `confidence={"target_envs": 0.3}`, and its assertions read `payload["job"]["confidence"]["target_envs"] == 0.3` and `payload["low_confidence"] == ["target_envs"]`.
- **`atworks-agent/runtime/tests/test_orchestrator.py`:** the `stage_job` fixture at lines 62–64 becomes
  ```python
        tool_calls_message(("stage_job", {"kind": "scheduled_run", "summary": "1주일 업데이트분 3일간 09시", "api_ids": ["api-1"], "target_envs": ["dev"],
                                          "schedules": [{"kind": "daily", "at": "09:00", "from_date": "2026-09-04", "count": 3}],
                                          "confidence": {"target_envs": 0.3}, "assumptions": ["target_envs defaulted to [dev]"]}, "tu-stage")),
  ```
- **`host/tests/test_app.py`:** every `JobDraft(..., target_env="dev")` becomes `target_envs=["dev"]` (5 sites: lines 78, 101, 175, 193, 226).
- **`host/tests/test_mock_backend.py`:** the same at lines 30 and 45.
- **`host/tests/test_scheduler_reports.py`:** every `JobDraft(..., target_env="dev")` becomes `target_envs=["dev"]`; `schedule=JobSchedule(...)` becomes `schedules=[JobSchedule(...)]`; `RunResult(...)` is unchanged; `JobSpec(...)` in `test_scheduler_uses_only_the_backend_abc` becomes `target_envs=["dev"], report=True, created_at=now, created_by="op", executions=0`; `RecordingBackend.execute_job_once` updates `{"run_ids": [self.run.run_id], "executions": 1}`; the three `runs_remaining` assertions become `remaining_executions` (`== 1`, `== 0`, `== 2` respectively).

- [ ] **Step 8: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `144 passed`, ruff `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(core): JobSpec becomes a matrix of envs, schedules and test data

target_env/schedule/runs_remaining give way to target_envs, schedules,
test_data and executions, with matrix_size/total_executions/
remaining_executions/runs_total derived. record_execution takes the
schedule index it consumed so several schedules advance independently.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Guardrails — the four caps and the four new rules

**Files:**
- Modify: `atworks-agent/core/atworks_agent/config.py:27-31` (guardrail section)
- Modify: `atworks-agent/core/atworks_agent/jobs.py` (`check_job_guardrails` signature and rules 2, 3, 6, 7; `JobLedger.__init__`)
- Modify: `atworks-agent/core/atworks_agent/gates.py:85` (pass `None` for the catalogue, with the reason)
- Modify: `atworks-agent/core/tests/conftest.py`, `atworks-agent/runtime/tests/conftest.py` (ledger gets the catalogue)
- Modify: `host/atworks_host/mock_backend.py:33-42` (ledger gets the catalogue)
- Test: `atworks-agent/core/tests/test_config.py`, `atworks-agent/core/tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 1's `JobDraft`, `TestDataSet`, `JobLedger`
- Produces: `AtworksAgentConfig.max_target_envs_per_job: int = 2`, `.max_schedules_per_job: int = 3`, `.max_test_data_sets: int = 5`, `.max_matrix_size: int = 400`; `check_job_guardrails(draft: JobDraft, config: AtworksAgentConfig, apis: Mapping[str, ApiSpec] | None = None) -> list[str]`; `JobLedger(config: AtworksAgentConfig, apis: Mapping[str, ApiSpec] | None = None)`

- [ ] **Step 1: Write the failing tests**

Append to `atworks-agent/core/tests/test_config.py`:

```python
def test_matrix_caps_are_config_fields_with_defaults():
    cfg = AtworksAgentConfig(model="m")
    assert (cfg.max_target_envs_per_job, cfg.max_schedules_per_job,
            cfg.max_test_data_sets, cfg.max_matrix_size) == (2, 3, 5, 400)
```

Append to `atworks-agent/core/tests/test_jobs.py` (and add `from datetime import UTC, datetime` plus `from atworks_agent.types import ApiSpec` to its imports):

```python
def _apis(**params: list[str]) -> dict[str, ApiSpec]:
    return {
        api_id: ApiSpec(api_id=api_id, method="POST", path=f"/v1/{api_id}", name=api_id,
                        updated_at=datetime(2026, 9, 1, tzinfo=UTC), params=list(names))
        for api_id, names in params.items()
    }


def test_guardrail_env_count_cap():
    cfg = AtworksAgentConfig(model="m", allowed_target_envs=("dev", "stg", "qa"),
                             max_target_envs_per_job=2)
    v = check_job_guardrails(_draft(target_envs=["dev", "stg", "qa"]), cfg)
    assert any("3 environments" in m and "limit is 2" in m for m in v)


def test_guardrail_schedule_list_and_data_set_caps():
    cfg = AtworksAgentConfig(model="m", max_schedules_per_job=2, max_test_data_sets=1,
                             max_schedule_count=30)
    scheds = [JobSchedule(kind="daily", at="09:00", from_date=f"2026-09-0{d}", count=1) for d in (4, 5, 6)]
    data = [TestDataSet(label="S1", values={"amount": "1"}),
            TestDataSet(label="S2", values={"amount": "2"})]
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedules=scheds, test_data=data), cfg)
    assert any("3 schedules" in m and "limit is 2" in m for m in v)
    assert any("2 test data sets" in m and "limit is 1" in m for m in v)


def test_guardrail_matrix_cap_when_each_dimension_is_within_its_own_limit():
    cfg = AtworksAgentConfig(model="m", max_apis_per_job=100, max_target_envs_per_job=2,
                             max_test_data_sets=5, max_matrix_size=20)
    data = [TestDataSet(label=f"S{i}", values={"amount": "1"}) for i in range(3)]
    v = check_job_guardrails(_draft(api_ids=[f"api-{i}" for i in range(5)],
                                    target_envs=["dev", "stg"], test_data=data), cfg)
    assert any("30 runs per execution" in m and "5 APIs × 2 envs × 3 data sets" in m
               and "limit is 20" in m for m in v)


def test_guardrail_duplicate_schedule():
    s = JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=2)
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[s, s.model_copy()]), CFG)
    assert any("duplicate schedule" in m for m in v)
    v2 = check_job_guardrails(
        _draft(kind=JobKind.SCHEDULED_RUN, schedules=[s, s.model_copy(update={"at": "18:00"})]), CFG)
    assert not any("duplicate schedule" in m for m in v2)


def test_guardrail_test_data_key_must_be_a_param_of_a_selected_api():
    data = [TestDataSet(label="S1 정상", values={"amount": "1000"})]
    ok = check_job_guardrails(_draft(test_data=data), CFG, _apis(a=["amount"], b=["id"]))
    assert not any("test data set" in m for m in ok)

    bad = check_job_guardrails(_draft(test_data=data), CFG, _apis(a=["id"], b=["id"]))
    assert any("test data set 'S1 정상' binds parameters (amount)" in m for m in bad)


def test_guardrail_skips_the_test_data_rule_without_a_catalogue():
    data = [TestDataSet(label="S1 정상", values={"amount": "1000"})]
    assert not any("test data set" in m for m in check_job_guardrails(_draft(test_data=data), CFG))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest atworks-agent/core/tests/test_config.py atworks-agent/core/tests/test_jobs.py -v`
Expected: FAIL — `AttributeError: 'AtworksAgentConfig' object has no attribute 'max_target_envs_per_job'`, `TypeError: check_job_guardrails() takes 2 positional arguments but 3 were given`, and the count/duplicate assertions find no matching message.

- [ ] **Step 3: Add the config fields**

In `atworks-agent/core/atworks_agent/config.py`, replace the guardrail section:

```python
    # -- guardrail (stage·apply 2회 검사) ---------------------------------------------
    max_apis_per_job: int = Field(default=100, ge=1)
    allowed_target_envs: tuple[str, ...] = ("dev", "stg")
    max_target_envs_per_job: int = Field(default=2, ge=1)      # 한 job이 겨냥할 수 있는 계 수
    max_schedules_per_job: int = Field(default=3, ge=1)        # 스케줄 항목 수 (회차 수가 아니다)
    max_test_data_sets: int = Field(default=5, ge=1)           # 테스트 데이터 세트 수
    max_matrix_size: int = Field(default=400, ge=1)            # apis × envs × data 곱의 상한
    max_schedule_count: int = Field(default=14, ge=1)          # 전체 스케줄의 회차 합
    max_concurrency: int = Field(default=4, ge=1, le=32)
```

- [ ] **Step 4: Add the four rules to `check_job_guardrails`**

In `atworks-agent/core/atworks_agent/jobs.py`, add one new stdlib import at the top of its group and add `ApiSpec` to the `.types` import:

```python
from collections.abc import Mapping
```
```python
from .types import ActorKind, ApiSpec, Binding, JobKind, JobSchedule, JobSpec, JobStatus, TestDataSet
```

Replace the function signature and docstring, add rule 2 and rule 3 right after rule 1, and rules 6 and 7 after the schedule block:

```python
def check_job_guardrails(
    draft: JobDraft,
    config: AtworksAgentConfig,
    apis: Mapping[str, ApiSpec] | None = None,
) -> list[str]:
    """Every rule a staged job must satisfy, checked at stage time and again at apply time —
    apply-time config may have tightened. Adding a rule here is the only way to add one:
    both phases and both call sites (``JobLedger``, ``gates.check_apply_job``) read this list.

    ``apis`` is the catalogue rule 7 (test-data keys must be parameters of a selected API)
    needs. ``None`` skips that one rule, and a caller that cannot see the catalogue must pass
    ``None`` rather than an empty mapping — a session that knows a job but not its APIs would
    otherwise turn every binding into a violation."""
```

```python
    # Rule 2 — per-dimension counts.
    if len(draft.target_envs) > config.max_target_envs_per_job:
        violations.append(
            f"job targets {len(draft.target_envs)} environments and the limit is "
            f"{config.max_target_envs_per_job} per job; narrow the selection"
        )
    if len(draft.schedules) > config.max_schedules_per_job:
        violations.append(
            f"job carries {len(draft.schedules)} schedules and the limit is "
            f"{config.max_schedules_per_job} per job; combine or drop some"
        )
    if len(draft.test_data) > config.max_test_data_sets:
        violations.append(
            f"job carries {len(draft.test_data)} test data sets and the limit is "
            f"{config.max_test_data_sets} per job; drop some"
        )
    # Rule 3 — the product. The dimensions multiply rather than concatenate, so no
    # per-dimension cap bounds the total work on its own (50 APIs × 2 envs × 5 data sets
    # passes every cap above and is 500 runs an execution).
    data_sets = max(1, len(draft.test_data))
    matrix = len(draft.api_ids) * len(draft.target_envs) * data_sets
    if matrix > config.max_matrix_size:
        violations.append(
            f"job expands to {matrix} runs per execution ({len(draft.api_ids)} APIs × "
            f"{len(draft.target_envs)} envs × {data_sets} data sets) and the limit is "
            f"{config.max_matrix_size}; narrow the selection"
        )
```

```python
    # Rule 6 — a repeated schedule is a mistake, unlike a repeated api id (which the
    # executor de-duplicates): two identical entries would double every occurrence silently.
    seen_schedules: set[tuple[str, str, str, str]] = set()
    for s in draft.schedules:
        key = (s.kind, s.at, s.tz, s.from_date)
        if key in seen_schedules:
            violations.append(
                f"duplicate schedule: two entries share kind={s.kind}, at={s.at}, tz={s.tz}, "
                f"from_date={s.from_date} — a repeated schedule is a mistake; drop one or raise its count"
            )
        seen_schedules.add(key)
    # Rule 7 — a data set may only bind parameters the selected APIs actually declare.
    if apis is not None:
        known = {p for i in draft.api_ids for p in (apis[i].params if i in apis else [])}
        for data in draft.test_data:
            unknown = sorted(k for k in data.values if k not in known)
            if unknown:
                violations.append(
                    f"test data set '{data.label}' binds parameters ({', '.join(unknown)}) that "
                    "none of the selected APIs declares"
                )
```

- [ ] **Step 5: Give the ledger a catalogue and keep `check_apply_job` catalogue-free**

`atworks-agent/core/atworks_agent/jobs.py`, `JobLedger.__init__` and the two `check_job_guardrails` calls inside it:

```python
    def __init__(self, config: AtworksAgentConfig, apis: Mapping[str, ApiSpec] | None = None):
        self._config = config
        self._apis = apis          # rule 7's catalogue; None (no catalogue) skips that rule
        self._jobs: dict[str, JobSpec] = {}
        self._sequence = 0
```
In `stage`: `violations = check_job_guardrails(draft, self._config, self._apis)`.
In `apply`: `violations = check_job_guardrails(draft, self._config, self._apis)`.

`atworks-agent/core/atworks_agent/gates.py`, in `check_apply_job`:

```python
    # apis stays None here on purpose: this session may know the job (the host remembered it
    # for a card click) without ever having seen its APIs, and a missing catalogue must never
    # become a test-data violation. The ledger re-checks rule 7 with the backend's catalogue.
    if violations := check_job_guardrails(draft, config, None):
```

`host/atworks_host/mock_backend.py` — load the APIs before the ledger so it can hand over the catalogue:

```python
    def __init__(self, config: AtworksAgentConfig, fixtures_dir: Path):
        self._config = config
        self.apis: dict[str, ApiSpec] = {
            row["api_id"]: ApiSpec(**row) for row in json.loads((fixtures_dir / "apis.json").read_text(encoding="utf-8"))
        }
        self.ledger = JobLedger(config, self.apis)
        self.runs: dict[str, RunResult] = {
            row["run_id"]: RunResult(**row) for row in json.loads((fixtures_dir / "runs.json").read_text(encoding="utf-8"))
        }
        self._run_seq = len(self.runs)
```

`atworks-agent/core/tests/conftest.py` **and** `atworks-agent/runtime/tests/conftest.py` (identical edits) — move `self.apis` above the ledger and pass it:

```python
    def __init__(self, config: AtworksAgentConfig):
        self.apis = {
            "api-1": ApiSpec(api_id="api-1", method="POST", path="/v1/contracts", name="계약 생성", group="contract", updated_at=T0, has_rules=True, params=["contractNo", "amount"]),
            "api-2": ApiSpec(api_id="api-2", method="GET", path="/v1/contracts/{id}", name="계약 조회", group="contract", updated_at=T0 - timedelta(days=20), has_rules=False),
        }
        self.ledger = JobLedger(config, self.apis)
        self.runs = [
            RunResult(run_id="run-1", api_id="api-1", executed_at=T0, target_env="dev", status=RunStatus.FAIL, failed_rules=["amount >= 0"], http_status=200),
            RunResult(run_id="run-2", api_id="api-2", executed_at=T0 + timedelta(minutes=5), target_env="dev", status=RunStatus.ERROR, http_status=503),
            RunResult(run_id="run-3", api_id="api-1", executed_at=T0 + timedelta(minutes=9), target_env="dev", status=RunStatus.PASS, http_status=200),
        ]
        self.executed: list[str] = []
```

- [ ] **Step 6: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `151 passed`, ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(core): matrix guardrails — per-dimension caps, product cap, duplicate schedules, test-data keys

check_job_guardrails takes the API catalogue so a data set can only bind
parameters a selected API declares; callers without a catalogue pass None
rather than an empty mapping.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Tool contract and preview — `stage_job` schema, the executor's catalogue, the `matrix` block

**Files:**
- Modify: `atworks-agent/core/atworks_agent/tools/registry.py:100-129` (the `stage_job` entry)
- Modify: `atworks-agent/core/atworks_agent/executor.py` (one line in `_stage_job`)
- Modify: `atworks-agent/core/atworks_agent/enrichment.py:109-122` (`enrich_job_preview`)
- Test: `atworks-agent/core/tests/test_registry.py`, `test_executor.py`, `test_presentation.py`; `atworks-agent/runtime/tests/test_orchestrator.py`

**Interfaces:**
- Consumes: Task 2's config caps and `check_job_guardrails(draft, config, apis)`
- Produces: `stage_job` input schema with `target_envs` (array, `minItems: 1`, `maxItems: config.max_target_envs_per_job`), `schedules` (array, `maxItems: config.max_schedules_per_job`), `test_data` (array, `maxItems: config.max_test_data_sets`), `required: ["kind", "summary", "api_ids", "target_envs"]`; `job_preview` payload key `matrix = {"apis": int, "envs": list[str], "data_sets": list[str], "executions": int, "runs_per_execution": int, "runs_total": int}`

- [ ] **Step 1: Write the failing tests**

In `atworks-agent/core/tests/test_registry.py`, replace `test_stage_job_schema_has_confidence_and_assumptions` and add two tests:

```python
def test_stage_job_schema_has_confidence_and_assumptions():
    stage = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "stage_job")
    props = stage["input_schema"]["properties"]
    assert {"kind", "summary", "api_ids", "target_envs", "schedules", "test_data",
            "binding", "confidence", "assumptions"} <= set(props)
    assert stage["input_schema"]["required"] == ["kind", "summary", "api_ids", "target_envs"]


def test_stage_job_matrix_caps_come_from_config():
    cfg = AtworksAgentConfig(model="m", max_target_envs_per_job=4, max_schedules_per_job=2,
                             max_test_data_sets=1)
    props = next(t for t in build_tools(cfg, []) if t["name"] == "stage_job")["input_schema"]["properties"]
    assert props["target_envs"]["type"] == "array"
    assert props["target_envs"]["minItems"] == 1 and props["target_envs"]["maxItems"] == 4
    assert props["schedules"]["type"] == "array" and props["schedules"]["maxItems"] == 2
    assert props["test_data"]["type"] == "array" and props["test_data"]["maxItems"] == 1


def test_stage_job_test_data_items_are_bounded_and_closed():
    stage = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "stage_job")
    item = stage["input_schema"]["properties"]["test_data"]["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["label", "values"]
    assert item["properties"]["label"]["maxLength"] == 40
    assert item["properties"]["values"]["additionalProperties"] == {"type": "string", "maxLength": 200}
```

Append to `atworks-agent/core/tests/test_executor.py`:

```python
async def test_stage_job_sanitizes_and_dedupes_target_envs(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev", "dev", "stg"]})
    assert not out.refused
    job_id = next(iter(state.seen_jobs))
    assert state.seen_jobs[job_id].target_envs == ["dev", "stg"]


async def test_stage_job_binds_test_data_and_previews_the_matrix(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {
        "kind": "scheduled_run", "summary": "dev/stg 비교", "api_ids": ["api-1"],
        "target_envs": ["dev", "stg"],
        "schedules": [{"kind": "daily", "at": "09:00", "from_date": "2026-09-05", "count": 3}],
        "test_data": [{"label": "S1 정상", "values": {"amount": "1000"}},
                      {"label": "S2 음수 금액", "values": {"amount": "-1"}}],
        "confidence": {"target_envs": 0.3, "test_data": 0.4},
    })
    assert not out.refused
    payload = out.events[1].data["payload"]
    assert payload["matrix"] == {
        "apis": 1, "envs": ["dev", "stg"], "data_sets": ["S1 정상", "S2 음수 금액"],
        "executions": 3, "runs_per_execution": 4, "runs_total": 12,
    }
    assert payload["low_confidence"] == ["target_envs", "test_data"]


async def test_stage_job_rejects_a_test_data_key_no_selected_api_declares(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev"],
                                          "test_data": [{"label": "S1", "values": {"nope": "1"}}]})
    assert out.blocked == "guardrail"
    assert "test data set 'S1' binds parameters (nope)" in out.result_text


async def test_stage_job_matrix_guardrail_checked_before_backend_call(backend, config, skills, session, state):
    class PermissiveBackend(backend.__class__):
        async def stage_job(self, session, draft, actor_kind):
            from datetime import UTC, datetime

            from atworks_agent.types import ActorKind as AK
            from atworks_agent.types import JobSpec
            return JobSpec(job_id="job-bypass", kind=draft.kind, summary=draft.summary,
                           api_ids=draft.api_ids, target_envs=draft.target_envs,
                           created_at=datetime.now(UTC), created_by=session.operator,
                           created_by_kind=AK.AGENT)

    tight = config.model_copy(update={"max_matrix_size": 1})
    ex = _exec(PermissiveBackend(tight), tight, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s",
                                          "api_ids": ["api-1", "api-2"], "target_envs": ["dev", "stg"]})
    assert out.blocked == "guardrail" and "4 runs per execution" in out.result_text
    assert "job-bypass" not in state.seen_jobs
```

Append to `atworks-agent/core/tests/test_presentation.py`:

```python
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
    }
```

And in `atworks-agent/runtime/tests/test_orchestrator.py`, drive the end-to-end matrix through `test_stage_turn_shows_preview_and_change_update` — change the fixture's job to two envs plus a data set and assert the preview carries them:

```python
        tool_calls_message(("stage_job", {"kind": "scheduled_run", "summary": "1주일 업데이트분 3일간 09시 dev/stg", "api_ids": ["api-1"], "target_envs": ["dev", "stg"],
                                          "schedules": [{"kind": "daily", "at": "09:00", "from_date": "2026-09-04", "count": 3}],
                                          "test_data": [{"label": "S1 정상", "values": {"amount": "1000"}}],
                                          "confidence": {"target_envs": 0.3}, "assumptions": ["target_envs defaulted to [dev, stg]"]}, "tu-stage")),
```
```python
    assert kinds == [("change_update", None), ("ui", "job_preview"), ("ui", "suggestions")]
    preview = next(e for e in events if e.data.get("component") == "job_preview")
    assert preview.data["payload"]["matrix"]["envs"] == ["dev", "stg"]
    assert preview.data["payload"]["matrix"]["data_sets"] == ["S1 정상"]
    assert state.seen_jobs["job-0001"].status.value == "staged" and not state.approved_job_ids
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
.venv/Scripts/python.exe -m pytest atworks-agent/core/tests/test_registry.py atworks-agent/core/tests/test_executor.py atworks-agent/core/tests/test_presentation.py atworks-agent/runtime/tests/test_orchestrator.py -v
```
Expected: FAIL — `KeyError: 'target_envs'` in the registry tests, `KeyError: 'matrix'` in the preview tests, and `test_stage_job_rejects_a_test_data_key_no_selected_api_declares` reports `assert None == 'guardrail'` because the executor still calls `check_job_guardrails` without a catalogue.

- [ ] **Step 3: Rewrite the `stage_job` schema**

In `atworks-agent/core/atworks_agent/tools/registry.py`, replace the whole `stage_job` entry:

```python
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
                    "updated_after": {"type": "string", "description": _ISO_DATETIME}},
                    "additionalProperties": False,
                    "description": "The search that produced api_ids (query/group/updated_after), kept for LATE binding and for the preview's provenance."},
                "report": {"type": "boolean"},
                "confidence": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}, "description": "Per-slot confidence: target_envs, schedules, test_data, binding, api_ids, report."},
                "assumptions": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 160}}},
                "required": ["kind", "summary", "api_ids", "target_envs"], "additionalProperties": False},
        },
```

Also update the `PREVIEW_TOOL` description's field list so the card's contract matches — replace `"schedule, assumptions"` with `"schedules, test data, assumptions"`:

```python
            "description": ("Show the approval card for a job staged or listed earlier; the card fills in APIs, targets, "
                            "schedules, test data, the matrix totals, assumptions, and highlights low-confidence slots. A "
                            "stage call shows this card itself; do not call it for a job staged this turn." if config.stage_shows_preview else
                            "Show the approval card for one staged job. Show every staged job with it before anything is applied."),
```

- [ ] **Step 4: Hand the executor's catalogue to the guardrail**

In `atworks-agent/core/atworks_agent/executor.py`, `_stage_job`, replace the single guardrail line:

```python
        if violations := check_job_guardrails(draft, self._config):
```
with:
```python
        # The guardrail runs here, before the backend call: a permissive backend must never be
        # handed a job this deployment's caps reject (R18). state.seen_apis is the catalogue
        # rule 7 needs — every api_id above already passed provenance, so every one is in it.
        if violations := check_job_guardrails(draft, self._config, self._state.seen_apis):
```

- [ ] **Step 5: Add the `matrix` block to the preview**

In `atworks-agent/core/atworks_agent/enrichment.py`, `enrich_job_preview`, insert after the `low_confidence` line:

```python
    # Server-computed so the one approval click is informed consent over the whole matrix:
    # every env, every data set, and the totals the operator is actually authorizing.
    enriched["matrix"] = {
        "apis": len(job.api_ids),
        "envs": list(job.target_envs),
        "data_sets": [d.label for d in job.test_data],
        "executions": job.total_executions,
        "runs_per_execution": job.matrix_size,
        "runs_total": job.runs_total,
    }
```

Update the module docstring's `job_preview` line to say what it now carries:

```python
- job_preview: 스테이징 레코드 그대로 + confidence<0.5 슬롯 목록 + 서버가 계산한 matrix(계·데이터·총 실행)
```

- [ ] **Step 6: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `158 passed`, ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(core): stage_job takes lists, preview carries the matrix

target_envs/schedules/test_data are arrays whose caps come from config, so
the tool bytes stay a pure function of config. enrich_job_preview computes
apis/envs/data_sets/executions/runs_total server-side.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

> **REVIEW GATE 1 — run before starting Task 4.** `/review-commerce-agent atworks-agent/core` (Steps 1–3 only) plus `superpowers:requesting-code-review` over Tasks 1–3. Fix any row that came out misaligned before continuing.

---

### Task 4: Prompt and skills — the matrix contract in words

**Files:**
- Modify: `atworks-agent/core/atworks_agent/prompt.py:23-39` (`job_contract`, `scheduling_note`)
- Modify: `atworks-agent/core/atworks_agent/gates.py:27-37` (`STAGING_FOLLOWTHROUGH_REMINDER` slot list)
- Modify: `atworks-agent/skills/schedule-run/SKILL.md` (whole file), `atworks-agent/skills/job-approval/SKILL.md` (one line)
- Test: `atworks-agent/core/tests/test_prompt.py`, `atworks-agent/core/tests/test_skills_load.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime; this is static prompt text, so it is a one-time cache bust — make every wording change here, not piecemeal.
- Produces: no new symbols. `build_static_system` output gains two sentences when `config.stages_jobs`.

- [ ] **Step 1: Write the failing tests**

Append to `atworks-agent/core/tests/test_prompt.py`:

```python
def test_static_prompt_states_the_matrix_contract():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "several target environments, several schedules and several test-data sets" in text
    assert "approval covers the whole matrix" in text
    assert "never facts about the system" in text
    assert "Never default target_envs to anything but [dev]" in text
```

Append to `atworks-agent/core/tests/test_skills_load.py`:

```python
def test_schedule_run_skill_covers_envs_schedules_and_test_data():
    body = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("schedule-run")
    assert "present_question_form" in body
    assert "양쪽" in body and "[dev, stg]" in body
    assert "test data" in body.lower() and "임의로" in body
    assert "one column per environment" in body
    approval = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("job-approval")
    assert "every environment, schedule and data set" in approval
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest atworks-agent/core/tests/test_prompt.py atworks-agent/core/tests/test_skills_load.py -v`
Expected: FAIL — both new tests fail on `assert ... in text` / `in body`.

- [ ] **Step 3: Update `prompt.py`**

In `atworks-agent/core/atworks_agent/prompt.py`, replace `job_contract` and `scheduling_note`:

```python
    job_contract = (
        "\n- Every job is staged with stage_job, shown on its preview card, and applied with apply_job "
        f"only after the operator approves that specific job {approval_where}. Do not call apply_job "
        "unprompted. Approval typed in chat approves nothing."
        "\n- A job may span several target environments, several schedules and several test-data sets; "
        "every schedule occurrence runs the whole api × env × data matrix. The preview card lists all "
        "of them; approval covers the whole matrix. There is no partial approval — if the operator "
        "wants only part of it, stage a narrower job."
        "\n- Test-data values are inputs the operator approves on the card; they are never facts about "
        "the system. Never invent a value the operator did not ask for without naming it in assumptions."
        "\n- When the operator's words name APIs and an action (run, schedule), stage the job this turn. "
        "A slot they did not state (target_envs, schedules, test data, FROZEN vs LATE binding) is either "
        "asked with present_question_form — at most 5 questions, every one prefilled with your best "
        "default and a `why` — or defaulted with confidence below 0.5 and named in assumptions, so the "
        "preview asks instead of you guessing silently. Never default target_envs to anything but [dev]."
        "\n- When a guardrail blocks a job, report what it held and propose a compliant alternative; do not "
        "split a job to get past the API-count, environment, or matrix-size limits."
        if stages else ""
    )
    scheduling_note = (
        "\n- A schedule whose first run time has already passed today starts tomorrow; say so in "
        "assumptions, one assumption per schedule you adjusted."
        if stages and config.enable_scheduling else ""
    )
```

In `atworks-agent/core/atworks_agent/gates.py`, update the reminder's slot list (the rest of the string is unchanged):

```python
    "queue as a preview — staging never runs anything. Put every value you defaulted "
    "(target_envs, schedules, binding, test data) into `assumptions` with a low `confidence`, so the "
```

- [ ] **Step 4: Rewrite `schedule-run/SKILL.md`**

Replace `atworks-agent/skills/schedule-run/SKILL.md` in full:

```markdown
---
name: schedule-run
description: Running or scheduling a set of APIs — a selection by date, group, or text, one or more target environments, one or more schedules, optional test data, and a report — as one staged job for approval. / API 묶음을 하나 이상의 대상 계에서, 지금 또는 정해진 시각마다, 정해진 테스트 데이터로 실행하고 리포트를 남기는 실행 계획.
---

# Schedule a run

Every run is a staged job. Nothing runs until the operator approves it on the Jobs page. One job is a
matrix: each schedule occurrence runs every selected API against every target environment with every
test-data set. Approval covers the whole matrix.

## Resolve the selection
- Turn the operator's words into a `search_apis` call: "지난 1주일 업데이트" → `updated_after` seven days before now; "오늘 업데이트한" → `updated_after` today 00:00; a group name → `group`; otherwise `query`. Keep the call's arguments as `select_where`.
- The ids in the result are the only ids the job may carry. If the result is empty, say so and stop.

## Environments — a list
- "어느 계에서" is a slot with a list value. '개발' → `dev`; '이관' → `stg`; '양쪽 / 두 계 / 비교 / 동일하게' → `[dev, stg]`.
- Missing from the utterance → ask on the question form, default `[dev]` with `confidence` 0.3 and an assumption naming it. Never `prod`.

## Schedules — a list
- Each distinct time pattern is one entry in `schedules`. "매일 09시 3일간" is one daily entry with `count: 3`; "그리고 다음 주 월요일 한 번 더" is a second `once` entry.
- Empty `schedules` means `run_now`. Two entries that agree on kind, time, timezone and start date are a mistake, not a repetition — merge them by raising `count`.
- `sum(count)` across all entries must stay within this deployment's limit. When the operator asks for more, say the limit held and propose a shorter plan instead of staging something that will be blocked.

## Test data — a list
- When the operator names values, bind them: one `test_data` entry, `label` in the operator's language, `values` keyed by the parameter names.
- When the operator says '임의로 / 아무 값 / 적당히', propose at most 3 sets drawn from the selected APIs' `params` (one normal, one boundary, and one invalid when the rules suggest one). Label each, and list **every** invented value in `assumptions` with `confidence` 0.4 — the operator approves the values on the card.
- Never bind a key that no selected API declares; call `get_api` when you are unsure which parameters exist.
- The same sets apply to every environment: that is what makes the comparison meaningful.

## Fill the slots — never silently
Slots usually missing from speech: `target_envs`, the schedules, `test_data`, `binding`, and whether a
report is wanted. When two or more are missing, ask once with `present_question_form` (id `job-slots`,
≤4 questions, every one with a `default` and a `why`), then end the turn. When one is missing, default
it, set its `confidence` below 0.5, and name it in `assumptions`.
- `target_envs`: default `[dev]`, confidence 0.3.
- Schedule start: "오늘부터" when today's time has passed → tomorrow, confidence 0.4, one assumption per adjusted schedule ("09:00 has passed today; starting tomorrow").
- `test_data`: default none (no binding), confidence 0.4 when the operator implied data but named none.
- `binding`: FROZEN unless the operator says the selection should be re-evaluated each run.
- `report`: true.

## Stage
- `stage_job` with `kind: scheduled_run` when `schedules` is non-empty, else `run_now`; `summary` in the operator's language. The call shows the preview card, which lists every environment, schedule and data set and the total run count.
- Then one sentence: where approval happens, plus the chips (adjust the schedule, change the environments, discard).

## Compare
- Comparison across environments appears in the job's report, one column per environment, with the differing rows highlighted. Say where the report will be; do not compare in prose and do not restate any status.

## After approval
- The operator approves on the Jobs page; you do not call `apply_job` unless the operator asks you to apply a job they already approved there.
```

- [ ] **Step 5: Add the approval-scope line to `job-approval/SKILL.md`**

Replace `atworks-agent/skills/job-approval/SKILL.md` in full:

```markdown
---
name: job-approval
description: What is waiting for approval, applying a job the operator already approved on the Jobs page, discarding a staged job. / 승인 대기 목록, 이미 승인된 job 적용, 스테이징 취소.
---

# Job approval

- `get_pending_jobs` first; refer to jobs by id and show one with `present_job_preview` only when it was not shown this turn.
- A job's card lists every environment, schedule and data set the approval covers; if the operator wants only part of it, discard and stage a narrower job.
- `apply_job` only for a job the operator approved on the Jobs page; when the gate holds, tell the operator approval happens there and stop.
- `discard_job` when the operator rejects or replaces a job. Confirm after the call succeeds.
```

- [ ] **Step 6: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `160 passed`, ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(core): prompt and skills describe the job matrix

One deliberate cache bust: the static job contract now says a job spans
several envs, schedules and data sets and that one approval covers all of
them, and schedule-run treats each as a list-valued slot.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Backend contract and the mock's matrix execution

**Files:**
- Modify: `atworks-agent/core/atworks_agent/backend.py:73-95` (`record_execution`, `execute_job_once`)
- Modify: `host/atworks_host/mock_backend.py:23-29` (`stub_verdict`), `:97-139` (`record_execution`, `execute_job_once`)
- Modify: `atworks-agent/core/tests/conftest.py`, `atworks-agent/runtime/tests/conftest.py` (both `InMemoryBackend`s, identical edits)
- Modify: `host/atworks_host/scheduler.py` (the two backend calls gain the index argument)
- Modify: `host/tests/test_scheduler_reports.py` (`RecordingBackend`)
- Test: `host/tests/test_mock_backend.py`

**Interfaces:**
- Consumes: Task 1's `JobSpec.remaining_executions`, `TestDataSet`, `RunResult.test_data_label`; Task 1's `JobLedger.record_execution(job_id, run_ids, schedule_index)`
- Produces: `AtworksBackend.execute_job_once(session, job_id, schedule_index: int | None) -> list[RunResult]`; `AtworksBackend.record_execution(session, job_id, run_ids, schedule_index: int | None) -> JobSpec`; `stub_verdict(api: ApiSpec, env: str, data: TestDataSet | None) -> tuple[RunStatus, list[str], int]`

- [ ] **Step 1: Write the failing tests**

Append to `host/tests/test_mock_backend.py`. Its import block becomes exactly this — add only what the tests use, or ruff's `F401` fails the task:

```python
from atworks_agent import ActorKind, AtworksAgentConfig, AtworksSessionContext, JobDraft, JobKind, RunStatus, TestDataSet
from atworks_host.mock_backend import MockAtworks, stub_verdict
```

```python
async def test_execute_job_once_produces_the_whole_matrix():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="matrix", api_ids=["api-001", "api-003"],
        target_envs=["dev", "stg"],
        test_data=[TestDataSet(label="S1 정상", values={"amount": "1000"}),
                   TestDataSet(label="S2 음수", values={"amount": "-1"})]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    produced = await b.execute_job_once(SESSION, job.job_id, None)

    assert len(produced) == 8                       # 2 apis × 2 envs × 2 data sets
    keys = {(r.api_id, r.target_env, r.test_data_label) for r in produced}
    assert len(keys) == 8                           # every run in one execution is distinguishable
    assert {r.target_env for r in produced} == {"dev", "stg"}
    assert {r.test_data_label for r in produced} == {"S1 정상", "S2 음수"}
    after = b.ledger.get(job.job_id)
    assert after.executions == 1 and after.remaining_executions == 0
    assert len(after.run_ids) == 8


async def test_stub_verdict_fails_a_negative_amount_binding():
    b = _backend()
    api = b.apis["api-001"]
    assert stub_verdict(api, "dev", TestDataSet(label="S2", values={"amount": "-1"})) == (
        RunStatus.FAIL, ["amount >= 0"], 200)
    assert stub_verdict(api, "dev", TestDataSet(label="S1", values={"amount": "1000"}))[0] is RunStatus.PASS
    # today's refund rule still holds when nothing is bound, so the fixtures keep their meaning
    assert stub_verdict(b.apis["api-003"], "dev", None) == (RunStatus.FAIL, ["refundAmount >= 0"], 200)


async def test_stub_verdict_differs_between_dev_and_stg_for_api_007():
    b = _backend()
    api = b.apis["api-007"]
    assert stub_verdict(api, "dev", None)[0] is RunStatus.PASS
    assert stub_verdict(api, "stg", None) == (RunStatus.ERROR, [], 503)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest host/tests/test_mock_backend.py -v`
Expected: FAIL — `TypeError: execute_job_once() takes 3 positional arguments but 4 were given`, and `stub_verdict()` rejects a `TestDataSet` where it expects an `int` sequence.

- [ ] **Step 3: Update the backend ABC**

In `atworks-agent/core/atworks_agent/backend.py`, replace `record_execution` and `execute_job_once`:

```python
    @abstractmethod
    async def record_execution(
        self, session: AtworksSessionContext, job_id: str, run_ids: list[str], schedule_index: int | None
    ) -> JobSpec:
        """job의 실행 1회를 기록한다: run_ids가 이번 실행이 낸 결과(비어 있을 수 있다 — 실행이
        실패했거나 LATE 재평가가 상한을 넘겨 건너뛴 경우), ``executions``를 정확히 1 늘린다.
        ``schedule_index``는 이번 실행이 소비한 스케줄의 인덱스다(run_now면 ``None``) — 여러
        스케줄이 각자의 ``done``을 따로 세기 때문에 이 값 없이는 어느 회차가 소비됐는지 알 수 없다."""
```

```python
    # -- 실행 (스케줄러가 부른다, LLM 경로 아님) ------------------------------------------
    @abstractmethod
    async def execute_job_once(
        self, session: AtworksSessionContext, job_id: str, schedule_index: int | None
    ) -> list[RunResult]:
        """job의 매트릭스를 1회 실행하고 결과를 돌려준다: ``target_envs`` × (``test_data`` 또는
        바인딩 없음) × ``api_ids``(LATE면 ``select_where`` 재평가)의 조합마다 run 1건. 각 run은
        자기 계를 ``target_env``에, 자기 데이터 세트를 ``test_data_label``에 달고 나오므로 한 번의
        실행이 낸 두 run이 같은 식별자로 겹치지 않는다.

        계약: 이 호출 하나가 정확히 한 번의 실행이다. 구현은 결과와 무관하게(빈 리스트를 내더라도)
        ``record_execution``을 정확히 한 번, 받은 ``schedule_index``와 함께 호출해 job의 남은 실행
        횟수를 소비해야 한다 — 그러지 않으면 스케줄러의 ``due_at``이 앞으로 나아가지 않고 같은 job이
        매 tick마다 실제 target을 향해 다시 실행된다(R29가 막으려던 바로 그 루프). 예외로 남는 경우는
        딱 하나, ``remaining_executions``가 이미 0이어서 애초에 소비할 슬롯이 없을 때뿐이다 — 그때는
        아무 것도 기록하지 않고 빈 리스트를 돌려준다. LATE 재평가와 상한 초과 스킵은 계마다가 아니라
        실행 1회당 한 번 적용된다. apply 이후 guardrail이 다시 걸린 실행 시도는 빈 결과와 함께
        ``add_guardrail_note``로 이유를 남기고, 그 시도 역시 슬롯을 소비한다."""
```

- [ ] **Step 4: Implement the mock's verdict and matrix loop**

In `host/atworks_host/mock_backend.py`, extend the imports with `TestDataSet` (from `atworks_agent`) and replace `stub_verdict`:

```python
def stub_verdict(api: ApiSpec, env: str, data: TestDataSet | None) -> tuple[RunStatus, list[str], int]:
    """결정론 스텁 "DSL". 실제 aTworks에선 규칙 엔진이 내는 판정을 대신한다. 규칙은 위에서 아래로,
    처음 맞는 하나가 판정이다:

    1. 바인딩된 키 중 이름에 ``amount``/``Amount``가 들어가고 값이 음수로 파싱되면 그 규칙 FAIL(200).
    2. 그 외, 경로에 ``refund``가 있고 바인딩이 없으면 ``refundAmount`` 규칙 FAIL(200) — 기존
       픽스처와 테스트가 계속 의미를 갖도록 남긴 오늘의 규칙.
    3. 그 외, ``api-007``을 ``stg``에서 부르면 ERROR(503) — dev/stg 비교가 차이를 보이도록. dev에선
       통과한다.
    4. 그 외 PASS(200).
    """
    if data is not None:
        for key, value in data.values.items():
            if "amount" in key or "Amount" in key:
                try:
                    number = float(value)
                except ValueError:
                    continue
                if number < 0:
                    return RunStatus.FAIL, [f"{key} >= 0"], 200
    if "refund" in api.path and data is None:
        return RunStatus.FAIL, ["refundAmount >= 0"], 200
    if api.api_id == "api-007" and env == "stg":
        return RunStatus.ERROR, [], 503
    return RunStatus.PASS, [], 200
```

Replace `record_execution` and `execute_job_once`:

```python
    async def record_execution(self, session, job_id, run_ids, schedule_index):
        return self.ledger.record_execution(job_id, run_ids, schedule_index)
```

```python
    async def execute_job_once(self, session, job_id, schedule_index=None) -> list[RunResult]:
        job = self.ledger.get(job_id)
        if job is None:
            return []
        if job.remaining_executions <= 0:
            return []
        api_ids = job.api_ids
        if job.binding is Binding.LATE and job.select_where:
            # LATE re-resolves once per execution, not once per environment: the selection is
            # a property of the job, and re-running it per env would let two envs disagree
            # about what the job even is.
            w = job.select_where
            api_ids = [a.api_id for a in await self.search_apis(
                session, query=w.get("query", ""), group=w.get("group"),
                updated_after=datetime.fromisoformat(w["updated_after"]) if w.get("updated_after") else None,
                limit=self._config.max_apis_per_job + 1)]
            if len(api_ids) > self._config.max_apis_per_job:
                self.ledger.add_guardrail_note(
                    job_id,
                    f"execution skipped: LATE selection resolved to {len(api_ids)} APIs, "
                    f"above the limit of {self._config.max_apis_per_job}",
                )
                # the slot is spent even though nothing ran, so a scheduled job does not
                # retry the same over-limit selection forever
                self.ledger.record_execution(job_id, [], schedule_index)
                return []
        produced: list[RunResult] = []
        bindings: list[TestDataSet | None] = list(job.test_data) or [None]
        for env in job.target_envs:
            for data in bindings:
                for api_id in api_ids:
                    api = self.apis.get(api_id)
                    if api is None:
                        continue
                    self._run_seq += 1
                    status, rules, http = stub_verdict(api, env, data)
                    run = RunResult(
                        run_id=f"run-{self._run_seq:04d}", api_id=api_id, executed_at=datetime.now(UTC),
                        target_env=env, test_data_label=data.label if data is not None else None,
                        status=status, failed_rules=rules, http_status=http,
                        duration_ms=100 + self._run_seq % 50, job_id=job_id)
                    self.runs[run.run_id] = run
                    produced.append(run)
        self.ledger.record_execution(job_id, [r.run_id for r in produced], schedule_index)
        return produced
```

- [ ] **Step 5: Update the other backend implementations and the scheduler's calls**

`atworks-agent/core/tests/conftest.py` **and** `atworks-agent/runtime/tests/conftest.py` (identical edits in both):

```python
    async def record_execution(self, session, job_id, run_ids, schedule_index):
        return self.ledger.record_execution(job_id, run_ids, schedule_index)
```
and, further down the same class:
```python
    async def execute_job_once(self, session, job_id, schedule_index=None):
        self.executed.append(job_id)
        return []
```

`host/tests/test_scheduler_reports.py`, `RecordingBackend`:

```python
    async def execute_job_once(self, session, job_id, schedule_index=None):
        self.calls.append("execute_job_once")
        self.job = self.job.model_copy(update={"run_ids": [self.run.run_id], "executions": 1})
        return [self.run]
```
```python
    async def record_execution(self, session, job_id, run_ids, schedule_index):
        self.calls.append("record_execution")
        return self.job
```

`host/atworks_host/scheduler.py`, in `tick` — pass the index through (still `None` until Task 6):

```python
                    produced = await self.backend.execute_job_once(self.session, job.job_id, None)
```
```python
                    await self.backend.record_execution(self.session, job.job_id, [], None)
```

`host/tests/test_app.py:104`, in `test_report_opens_without_session_header` — pass the index explicitly rather than relying on the mock's default:

```python
    runs = await backend.execute_job_once(session, job.job_id, None)
```

- [ ] **Step 6: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `163 passed`, ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(host): execute_job_once runs the env x data x api matrix in one call

The ABC's two execution methods take the schedule index they consume, and
the mock's stub verdict now varies by bound data and by environment so a
dev/stg comparison actually shows a difference.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Scheduler — several schedules, each with its own due clock

**Files:**
- Modify: `host/atworks_host/scheduler.py:17-71` (whole module body)
- Test: `host/tests/test_scheduler_reports.py`

**Interfaces:**
- Consumes: Task 1's `JobSchedule.done` / `JobSpec.executions` / `.remaining_executions`; Task 5's `execute_job_once(session, job_id, schedule_index)` and `record_execution(session, job_id, run_ids, schedule_index)`
- Produces: `due_at(schedule: JobSchedule, index: int) -> datetime | None` (takes one schedule, not the job); `Scheduler.tick(now) -> list[str]` returning one entry **per execution**, so a job whose two schedules are both due appears twice

- [ ] **Step 1: Write the failing tests**

Append to `host/tests/test_scheduler_reports.py`:

```python
async def test_two_schedules_on_one_job_run_independently(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="daily 3 + once", api_ids=["api-001"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=3),
                   JobSchedule(kind="once", at="14:00", tz="Asia/Seoul", from_date="2026-09-08", count=1)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 5, 8, 59, tzinfo=KST)) == []
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 5, 9, 30, tzinfo=KST)) == []   # 같은 회차는 두 번 안 돈다
    assert await sched.tick(datetime(2026, 9, 6, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 7, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 8, 14, 0, tzinfo=KST)) == [job.job_id]

    after = backend.ledger.get(job.job_id)
    assert [s.done for s in after.schedules] == [3, 1]
    assert after.executions == 4 and after.remaining_executions == 0
    assert await sched.tick(datetime(2026, 9, 9, 9, 0, tzinfo=KST)) == []


async def test_two_schedules_due_in_the_same_tick_run_twice(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="morning + evening", api_ids=["api-001"], target_envs=["dev"],
        schedules=[JobSchedule(kind="once", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=1),
                   JobSchedule(kind="once", at="18:00", tz="Asia/Seoul", from_date="2026-09-05", count=1)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    executed = await sched.tick(datetime(2026, 9, 5, 20, 0, tzinfo=KST))

    assert executed == [job.job_id, job.job_id]     # two schedules due, two executions
    after = backend.ledger.get(job.job_id)
    assert after.executions == 2 and [s.done for s in after.schedules] == [1, 1]
    assert len(after.run_ids) == 2                  # one api × one env × no data, twice
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest host/tests/test_scheduler_reports.py -v`
Expected: FAIL — the first test's 09-08 tick returns `[]` (the shim only ever looks at `schedules[0]`) and the second returns one entry instead of two.

- [ ] **Step 3: Rewrite the scheduler**

Replace the body of `host/atworks_host/scheduler.py` below the imports (keep the module docstring's first line, updated as shown):

```python
"""승인(applied)된 job을 예정 시각에 실행한다. LLM 호출 없음 — 여기서 모델을 부르는 순간
"실행은 사람이 승인한 계획대로만"이라는 선이 무너진다. tick(now)는 멱등적이다: 한 스케줄의 같은
회차는 두 번 돌지 않는다(회차 = 그 스케줄의 done). 한 job에 여러 스케줄이 있으면 각자의 시계로
따로 판단하고, 한 tick에 둘이 동시에 due면 실행도 두 번이다."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from atworks_agent import AtworksBackend, AtworksSessionContext, JobKind, JobSchedule, JobStatus

from .reports import Reports

logger = logging.getLogger(__name__)


def due_at(schedule: JobSchedule, index: int) -> datetime | None:
    """이 스케줄의 index번째(0부터) 회차 예정 시각. count를 넘어서면 None."""
    if index >= schedule.count:
        return None
    hh, mm = (int(x) for x in schedule.at.split(":"))
    first = datetime.fromisoformat(schedule.from_date).replace(hour=hh, minute=mm, tzinfo=ZoneInfo(schedule.tz))
    return first + timedelta(days=index) if schedule.kind == "daily" else (first if index == 0 else None)


class Scheduler:
    def __init__(self, backend: AtworksBackend, reports: Reports, session: AtworksSessionContext | None):
        self.backend = backend
        self.reports = reports
        self.session = session or AtworksSessionContext(session_id="scheduler", project_id="default", operator="scheduler")
        self._lock = asyncio.Lock()

    async def tick(self, now: datetime) -> list[str]:
        async with self._lock:
            executed: list[str] = []
            for job in list(await self.backend.applied_jobs(self.session)):
                if job.status is not JobStatus.APPLIED or job.remaining_executions <= 0:
                    continue
                if job.kind is JobKind.RUN_NOW:
                    # No schedule to advance: run_now is due once, the moment it is applied.
                    slots: list[int | None] = [None] if job.executions == 0 else []
                else:
                    slots = []
                    for index, schedule in enumerate(job.schedules):
                        if schedule.done >= schedule.count:
                            continue
                        when = due_at(schedule, schedule.done)
                        if when is not None and when <= now:
                            slots.append(index)
                for schedule_index in slots:
                    if await self._execute_one(job.job_id, schedule_index, job.report):
                        executed.append(job.job_id)
            return executed

    async def _execute_one(self, job_id: str, schedule_index: int | None, report: bool) -> bool:
        """One occurrence of one schedule. Returns whether it produced runs. The backend
        consumes the slot itself; the only slot this method consumes is the one an exception
        would otherwise leave unspent (R29: an unspent slot re-runs against the real target
        on the very next tick)."""
        try:
            produced = await self.backend.execute_job_once(self.session, job_id, schedule_index)
        except Exception as error:
            # Execution failure: record as such and move on.
            logger.exception("job %s failed during scheduled execution", job_id)
            await self.backend.add_guardrail_note(self.session, job_id, f"execution failed: {type(error).__name__}")
            await self.backend.record_execution(self.session, job_id, [], schedule_index)
            return False
        if not produced:
            return False
        if report:
            try:
                current = await self.backend.get_job(self.session, job_id)
                every = await self.backend.runs_by_ids(self.session, current.run_ids)
                self.reports.write(current, every)
            except Exception as error:
                # Report failure: log and note it, but do NOT record a second execution.
                # The execution already happened.
                logger.exception("job %s report failed", job_id)
                await self.backend.add_guardrail_note(self.session, job_id, f"report failed: {type(error).__name__}")
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest host/tests/test_scheduler_reports.py -v`
Expected: PASS, 10 passed — including `test_scheduler_uses_only_the_backend_abc` unchanged in intent (still no `ledger`/`runs` attribute reached) and `test_report_failure_does_not_consume_a_second_slot`.

- [ ] **Step 5: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `165 passed`, ruff `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(host): scheduler tracks each schedule's occurrences separately

due_at takes one JobSchedule and an occurrence index; tick walks every
schedule whose done < count and whose next occurrence is past, so two
schedules due in one tick are two executions. Still no model call.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Report — the comparison table

**Files:**
- Modify: `host/atworks_host/reports.py:33-51` (`write`), add a module-level `_matrix` helper
- Modify: `host/atworks_host/report_template.html` (whole file)
- Test: `host/tests/test_scheduler_reports.py`

**Interfaces:**
- Consumes: Task 5's `RunResult.test_data_label`, Task 1's `JobSpec.target_envs`
- Produces: `data.json` gains `matrix = {"envs": list[str], "rows": [{"api_id": str, "test_data_label": str | None, "cells": {env: {"status": str, "run_id": str}}, "differs": bool}], "differs_count": int}` and `summary.by_env = {env: {"total": int, "pass": int, "fail": int, "error": int}}`

- [ ] **Step 1: Write the failing tests**

Append to `host/tests/test_scheduler_reports.py`:

```python
async def _matrix_report(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="dev/stg 비교", api_ids=["api-001", "api-007"],
        target_envs=["dev", "stg"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    return backend, reports, job


async def test_report_matrix_rows_flag_env_differences(tmp_path):
    _, _, job = await _matrix_report(tmp_path)
    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))

    assert data["matrix"]["envs"] == ["dev", "stg"]
    assert data["matrix"]["differs_count"] == 1
    rows = {r["api_id"]: r for r in data["matrix"]["rows"]}
    assert rows["api-007"]["differs"] is True
    assert rows["api-007"]["cells"]["dev"]["status"] == "pass"
    assert rows["api-007"]["cells"]["stg"]["status"] == "error"
    assert rows["api-007"]["cells"]["stg"]["run_id"].startswith("run-")
    assert rows["api-001"]["differs"] is False
    assert rows["api-001"]["test_data_label"] is None
    assert data["summary"]["total"] == 4
    assert data["summary"]["by_env"]["dev"] == {"total": 2, "pass": 2, "fail": 0, "error": 0}
    assert data["summary"]["by_env"]["stg"] == {"total": 2, "pass": 1, "fail": 0, "error": 1}


async def test_report_template_renders_the_comparison_table(tmp_path):
    _, reports, job = await _matrix_report(tmp_path)
    html = reports.read_html(job.job_id)

    assert '"envs": ["dev", "stg"]' in html and '"differs_count": 1' in html
    template_source = TEMPLATE.read_text(encoding="utf-8")
    assert "compare-rows" in template_source and "차이" in template_source
    assert "d.job.target_envs.join" in template_source
    assert "esc(" in template_source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
.venv/Scripts/python.exe -m pytest host/tests/test_scheduler_reports.py -v -k "matrix or comparison"
```
Expected: FAIL — `KeyError: 'matrix'` on `data["matrix"]`, and `assert "compare-rows" in template_source` fails.

- [ ] **Step 3: Implement `reports.py`**

In `host/atworks_host/reports.py`, add the helper above `class Reports` and rewrite `write`:

```python
def _matrix(job: JobSpec, runs: list[RunResult]) -> dict:
    """The api × data grid, one column per environment, built from the LATEST run per
    (api_id, test_data_label, env). A row `differs` when its cells do not agree — that, and
    the count of such rows, is the whole comparison the operator asked for. No model output
    reaches this: every status here is a run record's own verdict."""
    envs = list(job.target_envs)
    latest: dict[tuple[str, str | None, str], RunResult] = {}
    order: list[tuple[str, str | None]] = []
    for run in runs:
        key = (run.api_id, run.test_data_label, run.target_env)
        current = latest.get(key)
        if current is None or run.executed_at >= current.executed_at:
            latest[key] = run
        if (run.api_id, run.test_data_label) not in order:
            order.append((run.api_id, run.test_data_label))
    rows: list[dict] = []
    for api_id, label in sorted(order, key=lambda pair: (pair[0], pair[1] or "")):
        cells = {
            env: {"status": latest[(api_id, label, env)].status.value,
                  "run_id": latest[(api_id, label, env)].run_id}
            for env in envs if (api_id, label, env) in latest
        }
        rows.append({
            "api_id": api_id,
            "test_data_label": label,
            "cells": cells,
            "differs": len({c["status"] for c in cells.values()}) > 1,
        })
    return {"envs": envs, "rows": rows, "differs_count": sum(1 for r in rows if r["differs"])}
```

```python
    def write(self, job: JobSpec, runs: list[RunResult], *, generator: str = "refresh_runner") -> Path:
        folder = self._folder(job.job_id)
        folder.mkdir(parents=True, exist_ok=True)
        counts: dict = {"total": len(runs), "pass": 0, "fail": 0, "error": 0}
        by_env: dict[str, dict[str, int]] = {
            env: {"total": 0, "pass": 0, "fail": 0, "error": 0} for env in job.target_envs
        }
        for r in runs:
            counts[r.status.value] += 1
            bucket = by_env.setdefault(r.target_env, {"total": 0, "pass": 0, "fail": 0, "error": 0})
            bucket["total"] += 1
            bucket[r.status.value] += 1
        counts["by_env"] = by_env
        data = {
            "job": job.model_dump(mode="json", exclude_none=True),
            "summary": counts,
            "matrix": _matrix(job, runs),
            "runs": [r.model_dump(mode="json", exclude_none=True) for r in runs],
            "provenance": {"generator": generator, "generated_at": datetime.now(UTC).isoformat()},
        }
        (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # </script> inside a job summary or a failed rule must not close the data script
        # block early; escaping the slash keeps the JSON valid while breaking that tag.
        embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        html = TEMPLATE.read_text(encoding="utf-8").replace("__REPORT_DATA__", embedded)
        (folder / "index.html").write_text(html, encoding="utf-8")
        return folder / "index.html"
```

- [ ] **Step 4: Rewrite the template**

Replace `host/atworks_host/report_template.html` in full:

```html
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>aTworks 실행 리포트</title>
<style>
body{font:14px/1.5 system-ui,sans-serif;margin:24px;color:#1b1f2a;background:#fafbfc}
h1{font-size:18px;margin:0 0 4px} h2{font-size:14px;margin:18px 0 6px} .meta{color:#5a6072;font-size:12px;margin-bottom:16px}
.tiles{display:flex;gap:10px;margin-bottom:16px}.tile{border:1px solid #e3e6ee;border-radius:10px;padding:10px 14px;min-width:90px;background:#fff}
.tile b{display:block;font-size:20px}.fail b{color:#b42318}.error b{color:#b54708}.pass b{color:#067647}.diff b{color:#b54708}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;background:#fff}th,td{border-bottom:1px solid #e3e6ee;padding:6px 8px;text-align:left;font-size:13px}
tr.fail td:nth-child(4){color:#b42318;font-weight:600}tr.error td:nth-child(4){color:#b54708;font-weight:600}
tr.differs{background:#fff7ed}
.prov{margin-top:14px;font-size:11px;color:#8a90a3}
.hidden{display:none}
</style></head><body>
<h1 id="title"></h1><div class="meta" id="meta"></div>
<div class="tiles"><div class="tile">전체<b id="t-total"></b></div><div class="tile fail">fail<b id="t-fail"></b></div><div class="tile error">error<b id="t-error"></b></div><div class="tile pass">pass<b id="t-pass"></b></div><div class="tile diff hidden" id="t-diff-tile">차이<b id="t-diff"></b></div></div>
<section id="compare" class="hidden"><h2>계 비교</h2><div class="scroll"><table><thead><tr id="compare-head"></tr></thead><tbody id="compare-rows"></tbody></table></div></section>
<h2>실행 목록</h2>
<div class="scroll"><table><thead><tr><th>run</th><th>API</th><th>env</th><th>status</th><th>데이터</th><th>failed rules</th><th>HTTP</th><th>executed</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="prov" id="prov"></div>
<script id="report-data" type="application/json">__REPORT_DATA__</script>
<script>
const esc=(v)=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const d=JSON.parse(document.getElementById('report-data').textContent);
document.getElementById('title').textContent=d.job.summary;
document.getElementById('meta').textContent=`${d.job.job_id} · target ${d.job.target_envs.join(', ')} · ${d.job.api_ids.length} APIs · runs so far ${d.job.run_ids.length}`;
for(const k of ['total','fail','error','pass'])document.getElementById('t-'+k).textContent=d.summary[k];
const m=d.matrix||{envs:[],rows:[],differs_count:0};
if(m.envs.length>1){
  document.getElementById('t-diff').textContent=m.differs_count;
  document.getElementById('t-diff-tile').classList.remove('hidden');
  document.getElementById('compare').classList.remove('hidden');
  document.getElementById('compare-head').innerHTML=`<th>API</th><th>데이터</th>${m.envs.map(e=>`<th>${esc(e)}</th>`).join('')}`;
  document.getElementById('compare-rows').innerHTML=m.rows.map(r=>`<tr class="${r.differs?'differs':''}"><td>${esc(r.api_id)}</td><td>${esc(r.test_data_label??'—')}</td>${m.envs.map(e=>`<td>${esc((r.cells[e]||{}).status??'—')}</td>`).join('')}</tr>`).join('');
}
const order={fail:0,error:1,pass:2};
document.getElementById('rows').innerHTML=[...d.runs].sort((a,b)=>order[a.status]-order[b.status]).map(r=>`<tr class="${esc(r.status)}"><td>${esc(r.run_id)}</td><td>${esc(r.api_id)}</td><td>${esc(r.target_env)}</td><td>${esc(r.status)}</td><td>${esc(r.test_data_label??'—')}</td><td>${esc((r.failed_rules||[]).join(', '))}</td><td>${esc(r.http_status)}</td><td>${esc(r.executed_at)}</td></tr>`).join('');
document.getElementById('prov').textContent=`generated by ${d.provenance.generator} at ${d.provenance.generated_at} — statuses are aTworks rule verdicts; no model output on this page.`;
</script></body></html>
```

The page still has exactly two `</script>` tags, which is what `test_report_html_escapes_json_and_uses_client_side_escaping` pins.

- [ ] **Step 5: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `167 passed`, ruff `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(host): report compares the environments side by side

data.json gains a matrix block (latest run per api x data x env, with a
differs flag) and per-env counts; the template renders a comparison table
and a 차이 tile above the run list when the job spans more than one env.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

> **REVIEW GATE 2 — run before starting Task 8.** `/review-commerce-agent` (Steps 1–3 only) including `host/`, plus `superpowers:requesting-code-review` over Tasks 4–7.

---

### Task 8: Web — the portal renders the matrix

**Files:**
- Modify: `web/atworks-web/lib/types.ts:19-70`, `:95-104`
- Modify: `web/atworks-web/components/generative/JobPreviewCard.tsx` (whole file)
- Modify: `web/atworks-web/components/views/JobsView.tsx:23-45`
- Modify: `web/atworks-web/components/views/RunsView.tsx:60-108`

**Interfaces:**
- Consumes: `job_record` (Task 1's `JobSpec` shape — no `target_env`/`schedule`/`runs_remaining`) and Task 3's `job_preview` payload `matrix`
- Produces: no server-visible interface. `web/web-shared/` is not touched.

> This is the first task that builds the web. Tasks 1–7 deliberately left the TS mirror stale; `npm run build` fails before Step 3 and must pass after it.

- [ ] **Step 1: See the stale mirror compile even though it is wrong**

Run:
```bash
cd web && npm run build
```
Expected: PASS — and that is the problem this task fixes. The TS is internally consistent but no longer matches the server: `JobSpec.target_env` and `.schedule` are fields the host stopped sending, so the card renders `undefined` at runtime with nothing to catch it at build time. Step 2 corrects the mirror, which makes the compiler flag every stale reader; Step 5's clean build is the real gate.

- [ ] **Step 2: Update `lib/types.ts`**

Replace `RunResult`, `JobSchedule` and `JobSpec`, and add `TestDataSet` and `JobMatrix`:

```typescript
export interface RunResult {
  run_id: string;
  api_id: string;
  executed_at: string;
  target_env: string;
  test_data_label?: string | null;
  status: RunStatus;
  failed_rules?: string[];
  http_status?: number;
  duration_ms?: number;
  job_id?: string;
}
```

```typescript
export interface JobSchedule {
  kind: "once" | "daily";
  at: string;
  tz: string;
  from_date: string;
  count: number;
  done: number;
}

export interface TestDataSet {
  label: string;
  values: Record<string, string>;
}

export interface JobSpec {
  job_id: string;
  change_id: string;
  kind: "run_now" | "scheduled_run";
  status: "staged" | "applied" | "discarded";
  summary: string;
  api_ids: string[];
  target_envs: string[];
  schedules: JobSchedule[];
  test_data: TestDataSet[];
  binding: "FROZEN" | "LATE";
  report: boolean;
  confidence?: Record<string, number>;
  assumptions?: string[];
  guardrail_notes?: string[];
  created_at: string;
  created_by: string;
  created_by_kind: "operator" | "agent";
  applied_at?: string | null;
  applied_by?: string | null;
  discarded_by?: string | null;
  discarded_by_kind?: "operator" | "agent" | null;
  run_ids?: string[];
  executions: number;
}

/** Executions a job is approved for; mirrors JobSpec.total_executions in Python. */
export function totalExecutions(job: JobSpec): number {
  return job.schedules.length ? job.schedules.reduce((n, s) => n + s.count, 0) : 1;
}
```

And extend `JobPreviewPayload`:

```typescript
export interface JobMatrix {
  apis: number;
  envs: string[];
  data_sets: string[];
  executions: number;
  runs_per_execution: number;
  runs_total: number;
}

export interface JobPreviewPayload {
  job_id: string;
  change_id: string;
  headline?: string;
  note?: string;
  job: JobSpec;
  change?: JobSpec;
  low_confidence: string[];
  apis: ApiSpec[];
  matrix: JobMatrix;
}
```

- [ ] **Step 3: Rewrite `JobPreviewCard.tsx`**

Replace `web/atworks-web/components/generative/JobPreviewCard.tsx` in full:

```tsx
// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import type { ReactNode } from "react";
import { ApproveBar, type ChangeAction, ChangeStatusPill, formatDate, GenCard, GenCardHeader, GuardrailNotes, useChangeActions } from "web-shared";
import { reportUrl } from "@/lib/api";
import type { JobPreviewPayload, JobSchedule, JobSpec } from "@/lib/types";

const SLOT_LABEL: Record<string, string> = {
  target_envs: "대상 계",
  schedules: "스케줄",
  test_data: "테스트 데이터",
  binding: "선택 고정",
  api_ids: "대상 API",
  runs_total: "총 실행",
  report: "리포트",
};

function scheduleLine(s: JobSchedule): string {
  return s.kind === "daily" ? `${s.from_date}부터 매일 ${s.at} × ${s.count}회` : `${s.from_date} ${s.at} 1회`;
}

export default function JobPreviewCard({ payload, onAct }: { payload: JobPreviewPayload; onAct?: (id: string, action: ChangeAction) => Promise<JobSpec | null> }) {
  // web-shared의 applyChangeUpdate는 후속 change_update를 payload.change에 얹는다 — 있으면 그것이 최신.
  const { change: job, busy, error, act, canAct } = useChangeActions(payload.change ?? payload.job, onAct);
  const low = new Set(payload.low_confidence);
  const m = payload.matrix;
  const rows: Array<[string, ReactNode]> = [
    ["target_envs", job.target_envs.join(", ")],
    ["api_ids", `${job.api_ids.length}개 (${payload.apis.slice(0, 3).map((a) => a.path).join(", ")}${payload.apis.length > 3 ? " …" : ""})`],
    ["binding", job.binding === "FROZEN" ? "오늘 고른 목록 고정" : "실행 때마다 재선택"],
    [
      "schedules",
      job.schedules.length === 0 ? (
        "즉시 1회"
      ) : (
        <ul>
          {job.schedules.map((s) => (
            <li key={`${s.kind}-${s.tz}-${s.from_date}-${s.at}`}>{scheduleLine(s)}</li>
          ))}
        </ul>
      ),
    ],
    [
      "test_data",
      job.test_data.length === 0 ? (
        "바인딩 없음"
      ) : (
        <ul>
          {job.test_data.map((d) => (
            <li key={d.label}>
              <details>
                <summary className="cursor-pointer">{d.label}</summary>
                <span className="text-[12px] text-(--ink-soft)">
                  {Object.entries(d.values)
                    .map(([k, v]) => `${k}=${v}`)
                    .join(", ")}
                </span>
              </details>
            </li>
          ))}
        </ul>
      ),
    ],
    ["runs_total", `${m.runs_per_execution}건 × ${m.executions}회 = ${m.runs_total}건`],
    ["report", job.report ? "남김" : "안 남김"],
  ];
  return (
    <GenCard>
      <GenCardHeader
        title={payload.headline ?? job.summary}
        meta={
          <>
            <ChangeStatusPill status={job.status} />
            <span>{job.kind}</span>
            <span aria-hidden>·</span>
            <span>{formatDate(job.created_at)}</span>
          </>
        }
      />
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] text-(--ink-soft)">{payload.note}</p> : null}
      <table className="mx-3.5 my-2 w-[calc(100%-28px)] text-[13px]">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k} className={low.has(k) ? "bg-(--warn-soft)" : ""}>
              <td className="py-1 pr-3 align-top text-(--ink-soft)">
                {SLOT_LABEL[k] ?? k}
                {low.has(k) ? (
                  <span className="ml-1 text-(--warn)" title="확신 낮음 — 확인 필요">
                    ●
                  </span>
                ) : null}
              </td>
              <td className="py-1 font-medium">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {job.assumptions?.length ? (
        <ul className="mx-3.5 mb-2 list-disc pl-4 text-[12px] text-(--ink-soft)">
          {job.assumptions.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      ) : null}
      <GuardrailNotes notes={job.guardrail_notes} />
      {job.status === "applied" && job.run_ids?.length ? (
        <a className="mx-3.5 mb-2 inline-block text-[12.5px] underline" href={reportUrl(job.job_id)} target="_blank" rel="noreferrer">
          리포트 열기
        </a>
      ) : null}
      <ApproveBar change={job} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </GenCard>
  );
}
```

- [ ] **Step 4: Update `JobsView.tsx` and `RunsView.tsx`**

`web/atworks-web/components/views/JobsView.tsx` — change the import line and the summary line inside `JobRow`:

```tsx
import type { JobSpec } from "@/lib/types";
import { totalExecutions } from "@/lib/types";
```
```tsx
          <div className="mt-0.5 text-[12px] tabular-nums text-(--ink-soft)">
            {change.kind} · {change.target_envs.join(", ")} · {change.executions}/{totalExecutions(change)}회 · {formatDate(change.created_at)}
          </div>
```

`web/atworks-web/components/views/RunsView.tsx` — add a 데이터 column between Env and Status, in both the header and the body:

```tsx
                <th className="px-3 py-2.5 font-semibold">Env</th>
                <th className="px-3 py-2.5 font-semibold">데이터</th>
                <th className="px-3 py-2.5 font-semibold">Status</th>
```
```tsx
                    <td className="px-3 py-2 text-[12.5px] text-(--ink-soft)">{run.target_env}</td>
                    <td className="px-3 py-2 text-[12.5px] text-(--ink-soft)">{run.test_data_label ?? "—"}</td>
```

- [ ] **Step 5: Build the web**

Run:
```bash
cd web && npm run build
```
Expected: both workspaces build; `atworks-web` reports `✓ Compiled successfully` with no TypeScript errors.

- [ ] **Step 6: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `167 passed`, ruff `All checks passed!` (ruff excludes `web/`; the web has no pytest tests).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(web): portal renders the job matrix

types.ts mirrors the new JobSpec with no alias for the dropped scalars; the
preview card lists every env, schedule and data set plus the run totals the
approval covers, JobsView shows execution progress, RunsView a 데이터 column.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Smoke turn 4 and the docs

**Files:**
- Modify: `scripts/smoke_chat.py:9-16` (TURNS), `:81-88` (the `--turns` default)
- Modify: `docs/safety.md` (the guardrails row, the LATE row, a new approval-scope row, the deployment-owns bullet)
- Modify: `README.md` (the 검증 section and manual scenario ④)
- Test: `host/tests/test_smoke_script.py`

**Interfaces:**
- Consumes: everything above. Nothing consumes this task.
- Produces: `smoke_chat.TURNS` with 4 entries. `.env.example` is deliberately unchanged — no new environment variable is introduced by this plan.

- [ ] **Step 1: Write the failing test**

Append to `host/tests/test_smoke_script.py`:

```python
def test_turns_include_the_comparison_utterance():
    """The fourth utterance is the one that could not be staged as one job before this
    change; it must reach either a job preview or the slot-filling form."""
    assert len(smoke_chat.TURNS) == 4
    text, want = smoke_chat.TURNS[3]
    assert "개발서버와 이관서버" in text and "비교" in text
    assert want == {"job_preview", "question_form"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest host/tests/test_smoke_script.py -v`
Expected: FAIL — `assert 3 == 4`.

- [ ] **Step 3: Add turn 4 to the smoke script**

In `scripts/smoke_chat.py`, replace the docstring's first line, the `TURNS` list and the `--turns` default:

```python
"""python scripts/smoke_chat.py [--base http://127.0.0.1:8010] [--turns 1,2]  — 키 필요.
발화 4개의 카드가 나오는지 본다."""
```

```python
TURNS = [
    ("최근 실패한 api들 중 risk가 있다고 판단하는 것들을 가져와봐", {"run_digest"}),
    ("이거 왜 실패했어", set()),
    (
        "지난 1주일간 새롭게 update된 api들을 모아서 오늘부터 3일간 매일 오전 9시에 전부 수행하고 리포트를 남겨줘",
        {"job_preview", "question_form"},
    ),
    (
        "오늘 업데이트한 API를 개발서버와 이관서버에서 동일한 테스트 데이터로 수행하고 결과를 비교해줘",
        {"job_preview", "question_form"},
    ),
]
```

```python
    ap.add_argument(
        "--turns",
        default="1,2,3,4",
        help="comma list of which of the four utterances to run (default: all)",
    )
```

- [ ] **Step 4: Update `docs/safety.md`**

Replace the **Guardrails twice** row:

```markdown
| **Guardrails twice.** Guardrails (APIs-per-job cap; every entry of `target_envs` checked against the allow-list; per-dimension caps on environments, schedules and test-data sets; the `api_ids × target_envs × test_data` matrix-size cap that no per-dimension cap bounds on its own; total schedule occurrences; duplicate schedules; test-data keys no selected API declares; LATE needs `select_where`) run at stage time and again at apply time against the config in force then. A `prod` anywhere in `target_envs` — not only in the first position — blocks the whole job, and every offending environment is named in the message. | `check_job_guardrails` in `atworks_agent/jobs.py`, called from `_stage_job` in `atworks_agent/executor.py` and again inside `JobLedger.apply` |
```

Replace the **LATE re-check at execution** row:

```markdown
| **LATE re-check at execution.** A `LATE`-bound job resolves `select_where` again at execution time, not at stage time, and once per execution rather than once per environment; if the re-resolved selection now exceeds `max_apis_per_job` the run is skipped (and the slot is still spent, so a scheduled job does not retry the same over-limit selection forever). | `execute_job_once` in `host/atworks_host/mock_backend.py` |
```

Add a new row directly after **Host approval**:

```markdown
| **Approval covers the matrix it shows.** One click authorizes one `job_id`, and that job may span several environments, several schedules and several test-data sets. `enrich_job_preview` computes the whole matrix server-side (`envs`, `data_sets`, `executions`, `runs_per_execution`, `runs_total`) and the preview card lists every one of them, so the click is informed consent over exactly what `apply_job` then runs. There is no partial approval: a narrower job is a new staging. | `enrich_job_preview` in `atworks_agent/enrichment.py`; `JobPreviewCard.tsx` in `web/atworks-web/components/generative/` |
```

Replace the **Guardrail values** deployment bullet:

```markdown
- **Guardrail values.** The defaults in `atworks_agent/config.py` (`max_apis_per_job`,
  `allowed_target_envs`, `max_target_envs_per_job`, `max_schedules_per_job`,
  `max_test_data_sets`, `max_matrix_size`, `max_schedule_count`, …) are demonstration
  values; a deployment sets its own. `max_matrix_size` is the one that bounds total work,
  because the other dimensions multiply.
```

And in **Still asked of the model**, add one bullet after the `stage_job` one:

```markdown
- Test-data values are inputs the operator approves, never facts about the system; invented
  values are named in `assumptions` with a low `confidence`.
```

- [ ] **Step 5: Update `README.md`**

In the 검증 block, change the smoke line's comment:

```bash
python scripts/smoke_chat.py           # host가 떠 있는 상태에서 — 발화 4개의 카드·게이트를 확인
```

Change the manual-scenario heading to "수동 시나리오 4개" and append scenario 4:

```markdown
4. "오늘 업데이트한 API를 개발서버와 이관서버에서 동일한 테스트 데이터로 수행하고 결과를 비교해줘"
   → `question_form` 또는 `job_preview` — 카드에 대상 계 2개(dev, stg), 테스트 데이터 세트,
   "총 실행 N건 × M회 = K건"이 보인다(승인 한 번이 이 전부를 덮는다) → Jobs 뷰에서 승인 →
   `POST /api/atworks/scheduler/tick` → 리포트 상단에 계 비교 표와 "차이 N건" 타일이 뜬다.
```

- [ ] **Step 6: Run the full suite and the linter**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```
Expected: `168 passed`, ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
test(smoke): add the dev/stg comparison utterance; document the matrix rules

smoke_chat turn 4 is the utterance that needed two jobs before this change.
safety.md records the new guardrail rules and that one approval click now
covers the whole matrix the card shows.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

> **FINAL REVIEW GATE.** `superpowers:requesting-code-review` over the whole branch, then `superpowers:finishing-a-development-branch`. Completion criteria: `.venv/Scripts/python.exe -m ruff check .` clean, `.venv/Scripts/python.exe -m pytest` → 168 passed, `cd web && npm run build` clean, `python scripts/smoke_chat.py` exits 0 against a running host, and README manual scenario ④ shows the comparison table in the browser.

---

## Spec coverage

| Spec section | Task |
|---|---|
| §1.1 `TestDataSet`, `JobSchedule.done`, JobSpec field table, derived properties, `RunResult.test_data_label`, confidence keys | 1 (types), 3 (confidence keys in the schema description and the preview) |
| §1.2 config caps table; rules 1–8; `apis` parameter | 1 (rules 1, 4, 5, 8), 2 (caps, rules 2, 3, 6, 7 and the `apis` parameter) |
| §1.3 ledger `record_execution(schedule_index)`, `stage` resets | 1 |
| §2.1 `stage_job` schema, `required`, description sentence, `test_same_config_same_bytes` | 3 |
| §2.2 executor `_stage_job` steps 1–6 | 1 (parsing/sanitizing), 3 (guardrail with `state.seen_apis`) |
| §2.3 `enrich_job_preview` matrix block | 3 |
| §3.1 prompt sentences | 4 |
| §3.2 both SKILL.md files | 4 |
| §4.1 backend ABC | 5 |
| §4.2 `stub_verdict(api, env, data)` | 5 |
| §4.3 `due_at`/`tick` | 6 |
| §4.4 reports `matrix`, `by_env`, template table | 7 |
| §4.5 routes and `job_record` (no alias period) | 1 (the record follows the model automatically), 8 (the web consumer) |
| §5 web | 8 |
| §6 tests and verification (unit, runtime, host, live, review gates) | 1–9 plus the three review gates |
| §7 out of scope | Global Constraints |
