# Chat-authored validation rules — draft a rule, approve it, apply it to future runs only

**Date:** 2026-09-05 · **Status:** design for review · **Builds on:**
`2026-09-03-atworks-ai-chat-mvp.md`, `2026-09-04-multi-dimension-jobs-design.md`,
`2026-09-05-insights-and-briefing-design.md`

## 1. What this adds and why

The operator states a value-validation rule in plain language — "refundAmount는 금액이니
0 이상이어야 한다", "status는 PAID/CANCELLED/REFUNDED 중 하나여야 한다" — and the chat drafts
a **structured** validation rule for that API's parameter. A person approves it, and from that
moment aTworks evaluates it on **future** runs.

This is the project's core pattern — model proposes, deterministic code judges, a person approves —
applied to the **rule registry** instead of the job queue. The model translates natural language
into a closed rule object; it never becomes the judge, and the stored rule is what aTworks evaluates.

### 1.1 The non-negotiable guarantee (SI constraint)

**Approving a rule never changes a past result or a success rate.** In an SI project, historical
verdicts and pass rates must stay stable. Two mechanisms enforce this:

- **Effective-from-apply.** Every rule carries `effective_from`, stamped at approval. The verdict
  engine applies a rule only to a run whose `executed_at >= effective_from`. A run that already
  happened is never re-judged.
- **Immutable run records.** `RunResult.status` / `failed_rules` are written once at execution and
  never rewritten. Reports, briefings, `/runs/insights`, and every success-rate figure read stored
  verdicts, so they are unaffected by definition. Approving a rule changes nothing that already
  exists; it only shapes what the next execution decides.

### 1.2 The notification (impact preview, read-only — approach A)

The approval card carries a heads-up, computed but **never written**: how many recent runs *would*
have failed under the drafted rule. It is a simulation for the operator's judgment, not a mutation.

Honest scope (approach A, no schema change): a past run can be simulated only when its **input
values are reconstructable** — i.e. the run was produced by a job still in the ledger, so its
`test_data_label` maps to that job's `test_data` values. Runs with no reconstructable input (an
unbound run, or one from a job no longer held) are counted as "입력 불명" and excluded from the
"would-fail" count, never guessed. The card always states: "이 규칙은 승인 시점 이후 실행부터
적용됩니다. 과거 결과와 성공률은 그대로입니다." followed by "최근 M건 중 입력을 아는 K건, 그중
N건이 이 규칙에 해당." `RunResult` is not extended; nothing is persisted by the simulation.

## 2. Rule model (v1: four shapes, one closed DSL)

A rule is a structured object. The model fills its fields; the message string and evaluation are
server-derived. `atworks_agent/rules.py` (new) owns the type and the evaluator.

```python
RuleKind = Literal["compare", "membership", "required", "format"]
CompareOp = Literal[">=", ">", "<=", "<", "==", "!="]
NamedFormat = Literal["email", "date", "iso8601", "uuid", "number"]   # vetted patterns, closed set

class ValidationRule(BaseModel):
    rule_id: str
    api_id: str
    param: str                                  # must be a param of api_id
    kind: RuleKind
    op: CompareOp | Literal["in", "not_in"] | None = None   # compare/membership; None for required/format
    value: str | None = None                    # compare: the bound (parsed per param, see below)
    values: list[str] = Field(default_factory=list)         # membership set (<= max_membership_values)
    format: NamedFormat | None = None           # format: a named, vetted format
    pattern: str | None = None                  # format: a raw regex (review_required = True)
    review_required: bool = False               # raw regex or anything the model could get wrong
    status: RuleStatus = STAGED                  # staged | applied | discarded
    effective_from: datetime | None = None      # stamped at apply; None while staged
    message: str                                 # server-rendered, e.g. "refundAmount >= 0" (goes into failed_rules)
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    created_at: datetime
    created_by: str
    created_by_kind: ActorKind = OPERATOR
    applied_at / applied_by / discarded_at / discarded_by / discarded_by_kind   # audit, mirror JobSpec
```

Rules are **append-only with versions**: superseding a rule on the same `(api_id, param, kind)`
stages a new rule; the old one keeps its own `effective_from`, and a run is judged by the rule
versions whose `effective_from <= run.executed_at`. This keeps the audit trail and the SI guarantee
intact — an old run stays tied to the rules that were live when it ran.

### 2.1 The four shapes and their `message` rendering

| kind | fields | message (→ failed_rules on violation) | example NL |
|---|---|---|---|
| `compare` | op ∈ 6 ops, value | `refundAmount >= 0` | "환불 금액은 0 이상" |
| `membership` | op ∈ {in, not_in}, values | `status in [PAID, CANCELLED, REFUNDED]` | "status는 이 코드들 중 하나" |
| `required` | — | `contractNo required` | "계약번호는 필수" |
| `format` | format **or** pattern | `email matches email` / `code matches /^[A-Z]{3}$/` | "이메일 형식", "이 패턴이어야" |

`compare.value` is compared numerically when both the value and the run's binding parse as numbers,
else as a string (so `==`/`!=`/membership work on codes too). The evaluator is a pure function
`evaluate(rule, value: str | None) -> bool` (True = pass). `required` fails on `None`/empty;
`format`/`membership`/`compare` on a `None` binding are **skipped** (a missing value is a `required`
rule's job, not theirs) — this keeps rules composable.

### 2.2 Named formats over raw regex

`format` prefers a **named, vetted format** (`email`, `date`, `iso8601`, `uuid`, `number`) whose
pattern lives in a server constant `NAMED_FORMATS` — the model picks a name, not a regex. A raw
`pattern` is allowed but sets `review_required = True`, and the preview card flags it prominently
("직접 검토 필요: 정규식"). Rationale: a model-invented regex is the easiest thing here to get
subtly wrong, and a wrong rule that a person rubber-stamps is exactly what the approval gate exists
to prevent — so we make the risky path visible, not silent. The evaluator compiles a raw pattern
defensively (`re.compile` in a try; a rule that fails to compile is rejected at stage time).

## 3. Provenance and guardrails

- **Param provenance:** `param` must be a parameter of `api_id` as `get_api` declared it this
  session (the same rule test-data keys already obey). `api_id` must be one the session has seen.
  `stage_rule` holds on an unknown api/param and tells the model to look the API up first.
- **Caps (config fields, mirrored as schema `maxItems`/enums so tool bytes stay a config function):**
  `enable_rules=True`, `rule_review_policy="always"`, `max_membership_values=50`,
  `max_rules_per_api` (a sanity cap), allowed `RuleKind`/`CompareOp`/`NamedFormat` sets. A staged
  rule that duplicates an already-applied identical rule is reported, not re-staged.
- **No prod-like escalation risk:** a rule cannot target anything but a real API param; it cannot
  run anything; it only changes future verdicts, and only after host approval.

## 4. Flow (mirrors the job lifecycle exactly)

1. **Draft.** Chat resolves the API (`search_apis`/`get_api`), translates the NL into a
   `ValidationRule` draft, fills or asks missing slots (which param? which op? the code list?), and
   calls `stage_rule`. Low-confidence slots go to `assumptions` with confidence < 0.5, or to a
   `present_question_form`, the same as jobs.
2. **Preview card.** `stage_rule` renders `present_rule_preview` → component `rule_preview`: target
   API + param, the rendered `message`, the kind-specific detail, the `review_required` flag, and
   the §1.2 impact notification. Server-filled; the model supplies only the rule id and a headline.
3. **Approve (host-only).** The Rules page Approve button →
   `POST /api/atworks/rules/{rule_id}/apply` → `rule_action` (a mirror of `job_action`): the host
   marks approval immediately before the executor call and clears it immediately after, so no chat
   turn can spend it. `apply_rule` stamps `effective_from = now` and flips status to `applied`.
   Discards take the same path. Approval typed in chat approves nothing.
4. **Apply to future runs only.** From `effective_from`, the verdict engine consults applied rules.

## 5. Backend contract

`AtworksBackend` gains rule methods, each behind the same "stage proposes, apply is the sole state
change" contract as jobs:

- `stage_rule(session, draft, actor_kind) -> ValidationRule`
- `get_pending_rules(session) -> list[ValidationRule]`
- `apply_rule(session, rule_id) -> ValidationRule` (stamps `effective_from`)
- `discard_rule(session, rule_id, actor_kind) -> ValidationRule`
- `list_rules(session, api_id=None) -> list[ValidationRule]` (for the Rules page and get_api enrich)
- `simulate_rule(session, draft) -> RuleImpact` — read-only; returns
  `{window, known, would_fail, excluded_unknown}` computed from stored runs + reconstructable
  inputs. Writes nothing.

MVP Mock keeps a `RuleLedger` (mirror of `JobLedger`) plus the reconstruction helper. **Evaluation
wiring:** `stub_verdict` stays as the legacy seed behavior (existing fixtures/tests keep meaning);
`execute_job_once` additionally evaluates every applied rule for the api whose
`effective_from <= now` against the run's binding values, and a run **fails** if the legacy stub
fails **or** any applied rule fails — additive, so no existing verdict changes. `failed_rules`
accumulates each violated rule's `message`. The REST adapter delegates rule storage/evaluation to
aTworks; the same effective-from semantics are the adapter's contract (stated on the ABC).

## 6. Tools, grounding, skill, prompt

- **Tool `stage_rule` (write).** Input: `api_id`, `param`, `kind`, and the kind's fields
  (`op`+`value`, or `op:in/not_in`+`values`, or nothing for `required`, or `format`/`pattern`),
  plus `summary`, `confidence`, `assumptions`. `additionalProperties:false`; enums from config.
  No forced first tool (a write tool, like `stage_job`); provenance + the new-skill procedure carry
  it.
- **Card tool `present_rule_preview`** → component `rule_preview` (§4.2).
- **Grounding:** none forces `stage_rule`. A light rule can force `get_api` when the operator names
  an API and a condition but the session has not read that API — optional, decide in the plan.
- **Skill `rule-authoring` (5th skill).** Procedure: resolve API → identify the param → classify the
  NL into one of the four kinds → build the structured rule → prefer a named format → fill/ask
  missing slots → `stage_rule` → one sentence pointing at the Rules page. Loaded on demand over the
  prompt's index, like the other four.
- **Prompt hard line:** "You draft validation rules as structured objects; you never judge a run
  against them and you never change a past result. A rule applies only after a person approves it,
  and only to runs executed after that." Plus the existing "you never decide pass/fail" line already
  covers evaluation.

## 7. Surfaces (web)

- **New nav item `Rules`** (5th): lists staged (Approve/Dismiss) and applied rules per API, each
  showing the rendered `message`, `effective_from`, `review_required` flag, and audit (who/when).
- **`rule_preview` card** in chat, with the impact notification and the immutability statement.
- **APIs view:** each API row can show its applied-rule count (from `list_rules`); "채팅에 첨부"
  already exists for pulling an API into chat to author a rule against it.
- Reuses web-shared's ApproveBar / change-lifecycle hooks via a `change_id` alias on `rule_record`,
  exactly as jobs do.

## 8. What stays out of v1 (recorded, not built)

- **Editing an applied rule in place.** v1 supersedes (append a new version); no in-place edit.
- **Cross-field rules** ("if A then B"), conditional/compound rules — single param, single
  predicate only.
- **Full historical simulation (approach B).** Not storing per-run input values; the notification
  covers only reconstructable runs (§1.2). Revisit if operators need complete past-impact numbers.
- **Rule import/export, bulk authoring, rule templates library.**
- **Retroactive re-judgement of any kind** — permanently out; it violates §1.1.

## 9. Testing & review posture

- Rule evaluator unit tests: each kind, numeric-vs-string compare, `None`-binding skip semantics,
  membership, named formats pass/fail, raw-regex compile failure rejected at stage.
- Provenance/guardrail: param must be a seen API's param; caps; duplicate-applied detection.
- Effective-from: a run before `effective_from` keeps its verdict; a run after picks the rule up;
  superseding versions judged by `effective_from`. **Immutability test:** approving a rule does not
  change any stored `RunResult` or any `/runs/insights`/report/briefing figure.
- Simulation is read-only: `simulate_rule` writes nothing; excluded-unknown counted honestly.
- Host approval mark consumed exactly once; chat cannot spend it; scheduler path unchanged and
  LLM-free.
- Web: Rules page renders; `rule_preview` card; `next build` clean.
- After implementation, run the H3 review (`/review-commerce-agent`) since this adds a backend
  surface, a tool, a card, and a skill.
```
