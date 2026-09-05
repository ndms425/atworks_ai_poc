# LLM-authored formats, example-verified patterns, a shared format library, and cross-API recommendation

**Date:** 2026-09-05 · **Status:** design for review · **Builds on:**
`2026-09-05-validation-rules-design.md` (the base validation-rules feature)

## 1. Purpose and confirmed principles

The base feature ships four rule kinds and five hardcoded named formats. That is too narrow: real
constraints ("전화번호는 숫자만", "주민번호는 뒤 6자리 마스킹", "계약번호는 6자리면 2-3-1, 10자리면
2-3-4-1 대시") are not in the list, so today they fall to a raw regex flagged `review_required`. This
spec leans into that path safely and adds reuse and recommendation.

Operator's confirmed decisions (2026-09-05):

- **A value-check rule is the API's success criterion**, independent of the HTTP response code. A run
  whose HTTP status is 200 still **fails** when a value rule fails. (This already holds: rule
  evaluation is additive over the stub and appends to `failed_rules` regardless of `http_status`.)
- **Pattern self-verification must exist** — the safety core below.
- **No value transformation; the system never rewrites a value and never touches API code.** Every
  example the operator gave (masking, dashing) is expressed as **validation** (does the returned value
  match this shape?), never as a transform.
- **Validation-only.**

## 2. The safety core: example-verified patterns

The project's spine is "the model never judges; deterministic code judges." Letting the LLM author a
regex means a wrong regex could become the deterministic judge — which is why raw patterns are flagged
today. The mitigation is to make every authored pattern **prove itself against examples before a human
can approve it.**

- A `format` rule that uses a raw `pattern` (rather than a built-in or library format) MUST carry
  `pass_examples: list[str]` and `fail_examples: list[str]` — at least
  `config.min_format_examples` (default 1) of each.
- At **stage time**, the backend compiles the pattern and runs every example:
  every `pass_example` must `fullmatch`, every `fail_example` must NOT. If any example is
  misclassified, staging is **rejected** (a named `InvalidToolArgument`/held) naming the offending
  example — the model must fix the pattern or the examples and try again. Nothing is stored.
- The examples are stored on the rule and **shown on the approval card**, so the human approver sees
  exactly what passes and fails, not an opaque regex. This turns "trust the LLM's regex" into "the LLM
  demonstrated the boundary and the machine verified it."
- The evaluator itself is unchanged (still `re.fullmatch`), so a rule that passed self-verification
  behaves identically at run time.

This mechanism is the reason the extension is safe; it applies to every LLM-authored pattern.

## 3. LLM-authored formats and the shared format library

### 3.1 Custom formats become named, reusable library entries

`NAMED_FORMATS` (five built-ins) stays as the seed. A new **format library** stores operator-approved
custom formats so they are named and reusable across APIs.

- A `stage_rule` for a raw-pattern format may include `save_format_as: <name>` (optional, kebab/snake,
  e.g. `phone-digits`, `krn-ssn-masked`, `contract-no`). The name plus the pattern plus the examples
  form a `FormatDefinition`.
- The definition is **promoted to the library only when the rule is approved** on the Rules page — it
  piggybacks on the existing host approval, so a format never enters the shared library without a human
  sign-off, and it carries the examples that verified it. No separate format-approval lifecycle in v1.
- A later `stage_rule` may set `format: <library name>` to reference a stored custom format by name
  (alongside the built-in names) — the backend resolves the pattern and examples. The model reuses,
  never re-authors, an established format.
- The library is backend-owned (mirror of the rule ledger). MVP Mock holds it in memory seeded with the
  five built-ins as read-only entries; the REST adapter delegates to aTworks. `format` on `stage_rule`
  accepts any built-in or library name; an unknown name is a named argument error.

### 3.2 Conditional / multi-pattern shapes fit as one regex

The operator's contract-number case ("6자리면 2-3-1, 10자리면 2-3-4-1") is a single format with an
alternation: `^\d{2}-\d{3}-\d{1}$|^\d{2}-\d{3}-\d{4}-\d{1}$`. The SSN-masking case is
`^\d{6}-\d\*{6}$`. Both are ordinary raw patterns under §2 — the LLM authors them with pass/fail
examples, the machine verifies, the human approves. No new rule kind is needed; "digits only", "letters
only", "fixed length", "prefix", and length-conditional shapes are all one `format` rule with a pattern.

## 4. Cross-API recommendation

When an operator applies a format to a param, the same shape usually applies to the same-named param on
other APIs.

- A read tool `find_apis_with_param(param)` returns the APIs (id, method, path) whose declared params
  include `param` and that do **not** already have a `format` rule for it. Deterministic catalogue scan,
  no model judgment in the result.
- The `rule-authoring` skill uses it to offer a recommendation after staging or approving a format
  rule: "이 포맷을 api-005의 contractNo, api-009의 contractId에도 적용할까요?" via suggestion chips or a
  small card.
- **Each recommended target becomes its own staged rule, approved individually** — no bulk auto-apply,
  consistent with the one-approval-per-change discipline. The recommendation proposes; the human
  approves each on the Rules page. Reuse is by library-format reference (§3.1), so the recommended
  rules carry the same verified pattern.

## 5. Model and schema changes

- `RuleDraft` / `ValidationRule` (format kind) gain `pass_examples: list[str]`,
  `fail_examples: list[str]` (bounded by `config.max_format_examples`), and `save_format_as: str | None`
  (draft only). `RuleDraft._kind_fields` gains: a raw-pattern format requires ≥ `min_format_examples`
  of each, and the examples must classify correctly against the compiled pattern (reject otherwise) —
  this is the §2 check at the pydantic layer; the backend re-checks with the resolved pattern for a
  library reference too.
- `render_message` and the existing `format_hint` (example/pattern on the card) extend to show the
  pass/fail examples for a raw pattern.
- New config: `min_format_examples` (default 1), `max_format_examples` (default 5),
  `max_format_library` (a sanity cap on saved formats). `allowed_named_formats` stays the built-in
  seed; the library adds names at runtime.
- `stage_rule` schema: add `pass_examples`, `fail_examples` (`maxItems` = `max_format_examples`),
  `save_format_as`; `format` description notes it accepts a built-in or a saved library name.

## 6. Backend, tools, card, skill

- **Backend** gains `save_format`/`get_format`/`list_formats` (the library) and
  `find_apis_with_param(session, param)`; `stage_rule` resolves a `format` that names a library entry.
  `apply_rule` promotes a `save_format_as` definition into the library (approval-gated, host-only).
- **Tools:** `find_apis_with_param` (read); `stage_rule` extended; the `rule_preview` card shows the
  pattern, the pass/fail examples, and (when saving) the format name. A `format_library` read is
  exposed for the Rules page.
- **Skill `rule-authoring`** procedure extends: for a shape with no built-in format, author a pattern
  AND supply pass/fail examples; prefer an existing library format by name; after approval, offer to
  reuse the format on same-named params via `find_apis_with_param`.
- **Prompt:** one line — "When you author a format pattern, you must supply pass and fail examples; the
  system verifies the pattern against them before it can be approved. Reuse a saved format by name
  rather than re-authoring it."
- **Evaluation is unchanged** (§2). The value rule remains the run's success criterion independent of
  HTTP status (§1).

## 7. Web

- The `rule_preview` card and Rules page show, for a format rule: the pattern, the pass/fail examples
  (as chips or a small table), and the `save_format_as` name when present.
- A **Formats** section (on the Rules page or its own tab) lists saved library formats: name, pattern,
  examples, who saved it — read-only reference for operators.
- Recommendation surfaces as suggestion chips / a small card that stage per-target rules on click; each
  still goes through the Rules-page approval.

## 8. Out of scope (recorded)

- **Value transformation / masking output** — permanently out (operator decision); validation only.
- **A separate format-approval lifecycle** — formats are promoted only via a rule's approval in v1.
- **Fuzzy param similarity** — recommendation matches on exact param name (and simple normalization),
  not semantic similarity, in v1.
- **Bulk apply** — each recommended rule is approved individually.
- **Cross-field / conditional-on-another-param rules** — single param, single (possibly alternation)
  pattern.
- Editing a saved format in place — supersede with a new named format instead.

## 9. Testing

- Example verification: a raw pattern whose pass/fail examples classify correctly stages; one that
  misclassifies is rejected at stage with the offending example named; the `min_format_examples`
  floor is enforced.
- Library: approving a `save_format_as` rule stores the format (host-approved only); a later
  `stage_rule` referencing the name resolves the pattern+examples; an unknown format name is a named
  error; built-ins are present and read-only.
- Recommendation: `find_apis_with_param` returns same-named params lacking a format rule, excludes ones
  that already have it, is a deterministic catalogue scan; each recommended target stages its own rule.
- Immutability/host-approval invariants from the base feature still hold (a saved format changes no past
  run; the library write is approval-gated).
- Web build clean; the card/Formats surfaces render the examples and pattern.
- After implementation, run the H3 review (`/review-commerce-agent`).
