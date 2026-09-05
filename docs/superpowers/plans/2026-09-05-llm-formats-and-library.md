# LLM Formats, Example-Verified Patterns, Format Library & Recommendation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend validation rules so the chat authors regex formats verified against LLM-supplied pass/fail examples, saves them to a shared named-format library (individually or in bulk with dedup), reuses them by name, and recommends rules for an API from what peer APIs already apply.

**Architecture:** Builds directly on the shipped validation-rules feature. A `format` rule gains `pass_examples`/`fail_examples` that must classify correctly against its pattern at stage time (rejected otherwise) — the safety core. A backend `FormatLibrary` stores approved named formats (built-ins seeded read-only); `stage_rule` resolves a `format` naming a library entry; `apply_rule` promotes a `save_format_as`. A `format_batch` change (mirror of the rule lifecycle) bulk-adds formats deduped by name/pattern under one host approval — safe because a library format is inert until an approved rule references it. Two read tools (`find_apis_with_param`, `recommend_rules_for_api`) drive cross-API recommendation; each recommended target stages its own rule, approved individually.

**Tech Stack:** Python 3.11+, pydantic v2, FastAPI, `commerce_common` (import-only), pytest; Next.js 16 / React 19 / Tailwind 4 in `web/atworks-web` on copied `web/web-shared`.

**Spec:** `docs/superpowers/specs/2026-09-05-llm-formats-and-library-design.md`

## Global Constraints

- `refs/` read-only; `commerce_common` import-only.
- **A value-check rule is the API's success criterion, independent of HTTP status** — already true (additive evaluation appends to `failed_rules` regardless of `http_status`); do not change it.
- **Validation only — the system never transforms/masks a value and never touches API code.**
- **Example-verification is mandatory for a raw-pattern format:** at stage time the pattern is compiled and every `pass_example` must `re.fullmatch`, every `fail_example` must not; a misclassified example rejects staging (named error), with ≥ `config.min_format_examples` of each. A rule that passed verification behaves identically at run time (evaluator unchanged).
- **A library format is inert:** it changes no run verdict until an approved rule references it. This is why bulk format seeding is one approval while applying a rule to an API stays per-rule.
- **Host-only approval** for rules AND format batches; the mark is set/cleared by the HTTP route only; chat cannot spend it. `save_format_as` promotion happens only on rule approval; recommendations never fabricate a constraint (only propose a peer's applied rule).
- Caps are `AtworksAgentConfig` fields mirrored as schema `maxItems`/enums (`test_same_config_same_bytes` holds).
- Immutability guarantee from the base feature is preserved (approving a rule/format never re-judges a past run).
- Windows commands, per-task commit with the `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer, `git checkout -- web/atworks-web/next-env.d.ts` after a web build.

## File map

| File | Responsibility |
|---|---|
| `atworks-agent/core/atworks_agent/rules.py` | example fields + validator, `FormatDefinition`, `FormatBatchDraft`, `FormatLibrary`, dedup, `verify_examples` |
| `atworks-agent/core/atworks_agent/types.py` | `ValidationRule` gains examples/save_format_as; `FormatBatchStatus` if a batch is a stored object |
| `atworks-agent/core/atworks_agent/config.py` | `min_format_examples`, `max_format_examples`, `max_format_library`, `max_format_batch` |
| `atworks-agent/core/atworks_agent/backend.py` | ABC: format library + batch + recommendation methods |
| `atworks-agent/core/atworks_agent/gates.py` | format-batch gates (mirror rule gates) |
| `atworks-agent/core/atworks_agent/serialization.py` | `format_record`, `format_batch_record` |
| `atworks-agent/core/atworks_agent/executor.py` | `_stage_rule` examples + format-name resolution; `_stage_format_batch`/apply/discard; `_find_apis_with_param`/`_recommend_rules_for_api` |
| `atworks-agent/core/atworks_agent/tools/registry.py` | `stage_rule` fields; `stage_format_batch`, `apply_format_batch`, `discard_format_batch`, `get_pending_format_batches`, `find_apis_with_param`, `recommend_rules_for_api`, `present_format_batch` |
| `atworks-agent/core/atworks_agent/tools/presentation.py`, `enrichment.py` | `format_batch` card; rule card shows examples |
| `atworks-agent/core/atworks_agent/prompt.py` | example + reuse hard line |
| `atworks-agent/skills/rule-authoring/SKILL.md` | authoring-with-examples, bulk, reuse, recommendation procedures |
| `host/atworks_host/mock_backend.py` | `FormatLibrary` wiring, batch apply/dedup, recommendation scans |
| `host/atworks_host/app.py` | `/formats`, `/format-batches`, `/format-batches/{id}/apply|discard` |
| `web/atworks-web/lib/{types,api}.ts`, `lib/useFormatBatchActions.ts` | rule examples; format-batch types/hook |
| `web/atworks-web/components/generative/{RulePreviewCard,FormatBatchCard}.tsx`, `index.tsx` | cards |
| `web/atworks-web/components/views/{RulesView,FormatsView}.tsx`, `app/page.tsx` | Formats surface, recommendation |
| `scripts/smoke_chat.py`, `CLAUDE.md`, `README.md` | smoke turn, docs |

---

## Part 1 — Example-verified patterns

### Task 1: Example fields, verifier, config

**Files:** Modify `rules.py`, `types.py`, `config.py`; Test `test_rules.py`.

**Interfaces:** Produces `verify_examples(pattern, pass_examples, fail_examples) -> list[str]` (returns misclassification messages, empty = ok); `RuleDraft`/`ValidationRule` gain `pass_examples`, `fail_examples`, and (draft only) `save_format_as`.

- [ ] **Step 1: config** — add to `config.py`:
```python
    min_format_examples: int = Field(default=1, ge=0)
    max_format_examples: int = Field(default=8, ge=1)
    max_format_library: int = Field(default=200, ge=1)
    max_format_batch: int = Field(default=30, ge=1)
```

- [ ] **Step 2: verifier + fields** — in `rules.py`:
```python
def verify_examples(pattern: str, pass_examples: list[str], fail_examples: list[str]) -> list[str]:
    """Empty result = the pattern classifies every example correctly. Never raises."""
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        return [f"pattern does not compile: {error}"]
    bad: list[str] = []
    for value in pass_examples:
        if compiled.fullmatch(value) is None:
            bad.append(f"pass example {value!r} does not match the pattern")
    for value in fail_examples:
        if compiled.fullmatch(value) is not None:
            bad.append(f"fail example {value!r} unexpectedly matches the pattern")
    return bad
```
`RuleDraft`: add `pass_examples: list[str] = Field(default_factory=list)`, `fail_examples: list[str] = Field(default_factory=list)`, `save_format_as: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")`. Extend `_kind_fields` format branch: when a raw `pattern` is used (no built-in `format`), require `len(pass_examples) >= 1` and `len(fail_examples) >= 1` (the runtime uses `config.min_format_examples`, but the draft has no config — enforce ≥1 here and the executor enforces the config floor), and run `verify_examples(pattern, pass_examples, fail_examples)` → raise `ValueError` naming the first misclassification. (A built-in named format needs no examples.) `ValidationRule`: add `pass_examples`, `fail_examples` (persisted for the card/audit) and `save_format_as: str | None = None`.

- [ ] **Step 3: Write failing tests** (`test_rules.py`): `verify_examples` returns [] for a good pattern+examples, and a message when a pass example doesn't match / a fail example matches / the pattern doesn't compile. A `RuleDraft(kind="format", pattern="^\\d+$", pass_examples=["123"], fail_examples=["12a"])` stages; one with `fail_examples=["999"]` (which matches `^\d+$`) raises ValueError naming the example; a raw pattern with no examples raises. A built-in `format="email"` still needs no examples.

- [ ] **Step 4: Run to fail → Step 5: implement → Step 6: run green** (`test_rules.py`, `test_config.py`, full suite, ruff).

- [ ] **Step 7: Commit** `feat(core): example-verified format patterns (pass/fail examples, verifier, config)`.

### Task 2: stage_rule carries examples; rule card shows them

**Files:** Modify `executor.py`, `tools/registry.py`, `enrichment.py`; Test `test_executor.py`, `test_registry.py`, `test_presentation.py`.

- [ ] **Step 1: Failing tests**: `stage_rule` with a raw pattern + examples stages and the `ValidationRule` carries them; below `config.min_format_examples` → named error; the `rule_preview` payload's `format_hint` (or a new `examples` block) includes the pass/fail examples; `stage_rule` schema has `pass_examples`, `fail_examples`, `save_format_as`.

- [ ] **Step 2: fail → Step 3: implement**: executor `_stage_rule` sanitizes and passes `pass_examples`/`fail_examples`/`save_format_as` into the `RuleDraft` dict; enforce the `config.min_format_examples` floor here (named `InvalidToolArgument`) since the draft only guarantees ≥1. `registry.py` `stage_rule` props: `pass_examples`/`fail_examples` (`type: array`, `items: {type: string}`, `maxItems: config.max_format_examples`, descriptions: "format rule with a raw pattern: values that MUST match / MUST NOT match; the system verifies the pattern against them before it can be approved"), `save_format_as` (string, "optional: save this pattern to the shared library under this name on approval"). `enrich_rule_preview`: extend `format_hint` (or add `examples`) with `pass_examples`/`fail_examples`.

- [ ] **Step 4: run green (full suite + ruff) → Step 5: Commit** `feat(core): stage_rule carries pass/fail examples and save_format_as; card shows them`.

---

## Part 2 — Format library

### Task 3: `FormatDefinition`, `FormatLibrary`, resolve a format by name

**Files:** Modify `rules.py`, `backend.py`, `serialization.py`, `mock_backend.py`, `__init__.py`; Test `test_rules.py`, `host/tests/test_formats_backend.py` (new).

**Interfaces:** `FormatDefinition(name, pattern, pass_examples, fail_examples, created_at, created_by, builtin: bool)`; `FormatLibrary` (`get(name)`, `list()`, `add(defn)` dedup, seeded with built-ins); backend `get_format`/`list_formats`/`save_format`; `stage_rule` resolves `format` naming a library entry.

- [ ] **Step 1: `FormatDefinition` + `FormatLibrary`** in `rules.py`:
```python
class FormatDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    pattern: str = Field(max_length=200)
    pass_examples: list[str] = Field(default_factory=list)
    fail_examples: list[str] = Field(default_factory=list)
    builtin: bool = False
    created_at: datetime | None = None
    created_by: str | None = None


class FormatLibrary:
    """이름 붙은 포맷 저장소. 내장 5개는 read-only 씨앗. dedup: 이름 또는 동일 패턴."""
    def __init__(self) -> None:
        self._formats: dict[str, FormatDefinition] = {
            name: FormatDefinition(name=name, pattern=pattern, builtin=True,
                                   pass_examples=[FORMAT_EXAMPLES.get(name)] if FORMAT_EXAMPLES.get(name) else [])
            for name, pattern in NAMED_FORMATS.items()
        }
    def get(self, name: str) -> FormatDefinition | None:
        return self._formats.get(name)
    def list(self) -> list[FormatDefinition]:
        return list(self._formats.values())
    def has_pattern(self, pattern: str) -> str | None:
        return next((f.name for f in self._formats.values() if f.pattern == pattern), None)
    def add(self, defn: FormatDefinition) -> tuple[bool, str | None]:
        """Returns (added, skip_reason). Dedup by name then by identical pattern."""
        if defn.name in self._formats:
            return (False, "name exists")
        if (dup := self.has_pattern(defn.pattern)) is not None:
            return (False, f"same pattern as {dup}")
        self._formats[defn.name] = defn
        return (True, None)
```
Export `FormatDefinition`, `FormatLibrary`. `format_record` in `serialization.py`: `defn.model_dump(mode="json", exclude_none=True)`.

- [ ] **Step 2: backend + resolution** — ABC `get_format`/`list_formats`/`save_format(session, defn)`. Mock: `self.format_library = FormatLibrary()`; implement the three. In `stage_rule`'s draft handling (executor Task 2 path) OR in the backend `stage_rule`, when `draft.format` names a **library** entry (built-in or saved) rather than only the built-in enum, resolve it: the stored rule's `pattern`/`pass_examples`/`fail_examples` come from the library entry and `format` is kept as the name. **Ruling:** resolution happens in the executor `_stage_rule` before building the draft — look up `backend.get_format(session, name)`; if found and it is not a built-in enum value, set the draft's `pattern` from it and clear `format` (so it stores as a pattern rule carrying the library name in `save_format_as`? No — keep `format=<name>` and let evaluate() resolve). **Simpler ruling:** extend `NAMED_FORMATS` resolution in `evaluate()` is NOT enough (library is per-backend). Instead: at stage time the executor resolves the library format into the rule's `pattern` (+ examples) and sets `format=None, pattern=<resolved>, save_format_as=None`, but records the source name in a new `ValidationRule.format_name: str | None` for display. This keeps `evaluate()` pattern-based and backend-independent. Add `format_name` to `ValidationRule`.

- [ ] **Step 3: Tests** (`test_formats_backend.py`): library seeded with 5 built-ins (builtin=True); `save_format` adds a new one; `add` dedups by name and by identical pattern (returns skip reason); `stage_rule` with `format="phone-digits"` (a saved format) resolves the pattern into the rule and evaluates correctly; an unknown format name is a named error.

- [ ] **Step 4: run green → Step 5: Commit** `feat(core,host): FormatLibrary with dedup and built-in seeds; stage_rule resolves a saved format by name`.

### Task 4: Format-batch lifecycle (bulk seed, deduped, one approval)

**Files:** Modify `rules.py` (or a small `formats.py`), `backend.py`, `gates.py`, `types.py`, `executor.py`, `serialization.py`, `mock_backend.py`; Test `test_rules.py`/new, `test_executor.py`.

**Interfaces:** `FormatBatchDraft(formats: list[FormatDefinition-like], summary?)`; a `FormatBatch` stored object with `status`, `entries` (each `{name, pattern, examples, outcome: "new"|"duplicate"|"invalid", reason?}`); backend `stage_format_batch`/`get_pending_format_batches`/`apply_format_batch`/`discard_format_batch`; gates `check_apply_format_batch`/`check_discard_format_batch`.

- [ ] **Step 1: Model + ledger** — a `FormatBatch` (mirror `ValidationRule`'s lifecycle fields: batch_id, status STAGED/APPLIED/DISCARDED, created_*, applied_*, discarded_*) holding `entries`. Staging computes each entry's outcome: `verify_examples` fails → `invalid` (dropped); name/pattern already in library → `duplicate` (skipped); else `new`. A `FormatBatchLedger` (mirror `RuleLedger`) stage/apply/discard; `apply` calls `library.add` for each `new` entry (host-approved) and stamps applied. `types.py`: `FormatBatchStatus` enum, or reuse `RuleStatus`. Put `FormatBatch` in `types.py` (so a session-state field, if any, round-trips — mirror the ValidationRule lesson; but batches may not need session-state storage since the host route re-reads from the backend — decide: store batches backend-side only, remember in session `seen_format_batches: dict[str, FormatBatch]` typed properly).

- [ ] **Step 2: gates + executor + backend** — `check_apply_format_batch`/`check_discard_format_batch` mirror the rule gates (provenance: batch seen; approval: host mark `approved_format_batch_ids`). Executor `_stage_format_batch` (sanitize each format, build the draft, `backend.stage_format_batch`, remember, preview card), `_apply_format_batch`/`_discard_format_batch` (gates + record). Mock backend implements the four; `apply_format_batch` adds only `new` entries to the library and returns the batch with outcomes. Session state gains `seen_format_batches: dict[str, FormatBatch]` (typed), `approved_format_batch_ids`/`host_action_format_batch_ids` (set).

- [ ] **Step 3: Tests**: staging a batch with 3 formats where one duplicates a built-in name, one duplicates a built-in pattern, and one is new → outcomes `duplicate, duplicate, new`; an entry whose examples fail verification → `invalid`; `apply_format_batch` adds only the `new` one to the library; a batch resolving to zero new is reported not applied; apply requires the host mark; the immutability of past runs is untouched (a format add changes no run).

- [ ] **Step 4: run green → Step 5: Commit** `feat(core,host): format-batch lifecycle — bulk library seed, deduped, host-approved`.

### Task 5: `apply_rule` promotes `save_format_as`

**Files:** Modify `executor.py`/`mock_backend.py` (whichever owns apply_rule promotion), `backend.py`; Test `test_rules_backend.py`/`test_executor.py`.

- [ ] **Step 1: Failing test**: staging a raw-pattern rule with `save_format_as="phone-digits"`, then approving it (host mark), adds `phone-digits` to the format library (with the rule's pattern+examples); a later `stage_rule` can reference `format="phone-digits"`. If the name already exists, the promotion is skipped with a guardrail note (no failure).

- [ ] **Step 2: implement**: in `apply_rule` (the ledger/backend path that flips a rule to APPLIED), after applying, if the rule carries `save_format_as`, build a `FormatDefinition` from its pattern+examples and `library.add(...)`; on skip, attach a note. This is host-approved by construction (apply_rule only runs behind the host mark).

- [ ] **Step 3: run green → Step 4: Commit** `feat(core,host): approving a rule promotes its save_format_as into the format library`.

---

## Part 3 — Tools, card, skill, recommendation

### Task 6: `stage_format_batch` tool + `format_batch` card + prompt + skill

**Files:** Modify `tools/registry.py`, `tools/presentation.py`, `enrichment.py`, `prompt.py`, `skills/rule-authoring/SKILL.md`, `scripts/smoke_chat.py`; Test `test_registry.py`, `test_presentation.py`, `test_prompt.py`, `test_skills_load.py`.

- [ ] **Step 1: Failing tests**: tool order adds `stage_format_batch`, `apply_format_batch`, `discard_format_batch`, `get_pending_format_batches` (after the rule tools) and `present_format_batch` (after `present_rule_preview`); the `format_batch` card payload joins the staged batch's entries with outcomes; the prompt carries the example+reuse hard line; `enable_rules=False` removes the new tools too.

- [ ] **Step 2: implement**: presentation `FORMAT_BATCH_TOOL`/`PresentFormatBatchPayload(batch_id, headline?, note?)`; `enrich_format_batch` reads the batch from `state.seen_format_batches`, adds `entries` (with outcomes), `change_id`, `summary counts` (new/duplicate/invalid). Registry `stage_format_batch` input: `formats` (array of `{name, pattern, pass_examples, fail_examples}`, `maxItems: config.max_format_batch`), `summary`; the apply/discard/get tools take `batch_id`/nothing. Prompt hard line: "When you author a format pattern you MUST supply pass and fail examples; the system verifies the pattern against them before approval. Reuse a saved format by name instead of re-authoring it. Bulk-add formats with stage_format_batch; duplicates are skipped." Skill: add authoring-with-examples, bulk-add, and reuse procedures. Smoke: add a bulk-format turn expecting `{"format_batch"}`.

- [ ] **Step 3: run green → Step 4: Commit** `feat(core): stage_format_batch tool, format_batch card, example/reuse prompt, skill updates`.

### Task 7: Recommendation read tools

**Files:** Modify `backend.py`, `executor.py`, `tools/registry.py`, `skills/rule-authoring/SKILL.md`; Test `test_executor.py`, `host/tests/test_mock_backend.py`.

**Interfaces:** backend `find_apis_with_param(session, param) -> list[ApiSpec]`, `recommend_rules_for_api(session, api_id) -> list[RuleRecommendation]` where `RuleRecommendation(param, from_api_id, rule: ValidationRule-like)`.

- [ ] **Step 1: Failing tests**: `find_apis_with_param("contractNo")` returns APIs declaring that param, excluding ones that already have a format rule for it; `recommend_rules_for_api(api_id)` returns, per rule-less param of the target, the applied rules on same-named peer params, and nothing where no peer has one (no fabrication).

- [ ] **Step 2: implement**: Mock scans `self.apis` for the param, and `self.rule_ledger.applied()` for peer rules; executor `_find_apis_with_param`/`_recommend_rules_for_api` fenced read handlers; registry read tools (`find_apis_with_param {param}`, `recommend_rules_for_api {api_id}`), no forced grounding; skill: procedures for outward (after applying a format) and inward ("기준 몰라 → recommend_rules_for_api → 대상별 stage_rule, 개별 승인") recommendation. Each recommended rule is staged individually via `stage_rule` (referencing the library format), approved on the Rules page.

- [ ] **Step 3: run green → Step 4: Commit** `feat(core,host): find_apis_with_param and recommend_rules_for_api read tools`.

---

## Part 4 — Host

### Task 8: Format routes

**Files:** Modify `host/atworks_host/app.py`; Test `host/tests/test_app.py`.

- [ ] **Step 1: Failing tests**: `GET /formats` lists the library (session); `GET /format-batches` lists pending batches; `POST /format-batches/{id}/apply` marks-then-consumes and returns `{ok, change}` with outcomes and the library updated; `.../discard`; applying an unknown batch → `{ok:false}`; the mark cannot be spent by chat.

- [ ] **Step 2: implement**: `format_batch_action` (mirror `rule_action`, using `approved_format_batch_ids`/`host_action_format_batch_ids`), routes `GET /formats`, `GET /format-batches`, `POST /format-batches/{id}/apply|discard`. Separate namespace, never `/changes` or `/rules`.

- [ ] **Step 3: run green → Step 4: Commit** `feat(host): format library + format-batch routes with host-only approval`.

---

## Part 5 — Web

### Task 9: Rule-card examples; format-batch card + hook

**Files:** Modify `web/atworks-web/lib/{types,api}.ts`, `components/generative/{RulePreviewCard,index}.tsx`, `app/page.tsx`/`AssistantPanel.tsx`; new `lib/useFormatBatchActions.ts`, `components/generative/FormatBatchCard.tsx`.

- [ ] **Step 1**: types for `FormatDefinition`, `FormatBatch`, `FormatBatchPayload`, rule `pass_examples`/`fail_examples`/`format_name`. `api.ts`: `fetchFormats`, `fetchFormatBatches`, `actOnFormatBatch` (posts `/format-batches/{id}/{action}`); `useFormatBatchActions` (local hook, mirror `useRuleActions`, posts to `/format-batches`). `RulePreviewCard` renders pass/fail examples (chips) and the source format name when present. `FormatBatchCard`: lists entries with a new/duplicate/invalid badge and a counts summary; `ApproveBar` via `useFormatBatchActions`. `index.tsx` `format_batch` case threads a new `onFormatBatchAction` prop (never `/changes` or `/rules`).

- [ ] **Step 2**: `npm run build` clean; revert next-env churn. **Commit** `feat(web): rule-card examples, format_batch card and hook (posts to /format-batches)`.

### Task 10: Formats surface + recommendation

**Files:** Modify `app/page.tsx`, `components/views/RulesView.tsx`; new `components/views/FormatsView.tsx`.

- [ ] **Step 1**: `FormatsView` lists the library (`fetchFormats`): name, pattern, examples, builtin/saved, who saved. Add a Formats section — either a 6th nav item or a section within the Rules page (decide: a section on the Rules page keeps nav small; the plan uses a **Formats tab** only if nav has room, else a Rules-page section — implementer picks and notes it). Recommendation from `recommend_rules_for_api` surfaces as suggestion chips in chat (the model calls the tool and emits chips) — no dedicated view needed for v1; each chip stages a rule.

- [ ] **Step 2**: `npm run build` clean; revert next-env. **Commit** `feat(web): Formats library surface`.

---

## Part 6 — Docs, review, verification

### Task 11: Decision record, review, live smoke

- [ ] **Step 1: CLAUDE.md** — extend the rule bullets: example-verified formats, the format library (built-ins + saved), bulk seeding (one approval, deduped, inert-until-referenced), the two recommendation directions, the new routes (`/formats`, `/format-batches/{id}/apply|discard`), and reaffirm "a value rule is the API's success criterion independent of HTTP status." Keep every existing line.
- [ ] **Step 2: README** — a short "Format library & recommendation" paragraph.
- [ ] **Step 3: H3 review** — `/review-commerce-agent`; fix mismatched decision-record rows.
- [ ] **Step 4: Live smoke** — author a raw-pattern rule with examples (a bad-example attempt is rejected; a good one stages and shows examples), bulk-add formats (duplicates skipped), reference a saved format by name, and inward-recommend for a rule-less API; approve on the Rules/Formats surface; confirm `/runs/insights` and a report are byte-unchanged before/after any format add or rule approval.
- [ ] **Step 5: Commit** `docs: decision record and README for format library, examples and recommendation`.

---

## Self-review notes

- **Example verification is the safety spine** (Task 1): enforced at the draft layer (≥1 each, correct classification) and at the executor layer (the config floor). A rule cannot reach approval with an unverified pattern.
- **Library format resolution stays pattern-based** (Task 3 ruling): the executor resolves a saved-format name into the rule's `pattern` at stage time and records `format_name` for display, so `evaluate()` remains backend-independent and unchanged — no per-backend lookup in the evaluator.
- **Route namespaces stay separate**: `/changes` (jobs), `/rules` (rules), `/format-batches` (format batches). Each has its own action hook on the web; never reuse `chat.actOnChange`.
- **Session-state typing lesson applied**: `seen_format_batches` is typed with the real `FormatBatch` model (in types.py) so it survives the JSON round-trip, unlike the original rules `dict[str, Any]` bug.
- **Inert-format safety argument** justifies the one-approval bulk seed (Task 4): a library format judges nothing until an approved rule references it; applying a rule to an API stays per-rule.
- **No transformation anywhere**: every example is a validation (fullmatch) check; nothing rewrites a value.
- Type names across tasks: `verify_examples`, `pass_examples`/`fail_examples`/`save_format_as` (T1); schema+card (T2); `FormatDefinition`/`FormatLibrary`/`format_name` (T3); `FormatBatch`/`FormatBatchLedger`/gates/`seen_format_batches` (T4); promotion (T5); `stage_format_batch`/`present_format_batch`/`enrich_format_batch` (T6); `find_apis_with_param`/`recommend_rules_for_api`/`RuleRecommendation` (T7); `format_batch_action`+routes (T8); web hook/cards (T9–10).
