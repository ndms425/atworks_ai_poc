---
name: rule-authoring
description: Turning a natural-language validation ask into a structured rule draft for one API parameter — compare, membership, required, or format — staged for approval on the Rules page. / 값 검증 규칙을 자연어로 받아 구조화된 규칙 초안을 만든다.
---

# Author a validation rule

A rule is a structured draft, not a judgment. Staging it evaluates nothing; it becomes effective
only after the operator approves it on the Rules page, and only for runs executed after that —
past runs are never re-evaluated.

## Resolve the API and the parameter
- `get_api` for the API the operator named (or the one attached to the turn). `param` must be one it lists; if the operator's wording does not match a param name exactly, pick the closest one and say which you picked.
- If the API or the parameter cannot be resolved from this session's tool results, ask instead of guessing.

## Classify the ask
- A bound ("0 이상", "최대 100", "이후") → `compare`: op is one of `>=,>,<=,<,==,!=`, `value` is the bound.
- A closed set ("dev/stg/prod 중 하나", "다음 상태값만") → `membership`: `op` is `in` or `not_in`, `values` is the list.
- "값이 있어야 한다 / 필수" → `required`: no op or value needed.
- A shape ("이메일 형식", "날짜 형식", "UUID") → `format`. Prefer a named `format` (`email`, `date`, `iso8601`, `uuid`, `number`) over a raw `pattern`. A raw pattern is flagged for review (`review_required`) — say so when you use one.

## Author a raw pattern with examples
- A raw `pattern` needs at least one `pass_examples` entry (a value that MUST match) and one `fail_examples` entry (a value that MUST NOT match) — the system verifies the pattern against them before it can reach approval. A bad example set (one that does not classify correctly) is rejected, not silently accepted; fix the pattern or the examples and try again.
- Check the format library first (a saved or built-in format may already fit) before inventing a new pattern.

## Reuse a saved format
- Before authoring a new pattern, consider whether the shape is already in the format library — a built-in (`email`, `date`, `iso8601`, `uuid`, `number`) or one an operator saved earlier. Put the library name in `format`; the system resolves its pattern and examples for you, and the card shows the source name instead of re-verifying anything. Reuse a saved format by name instead of re-authoring it.

## Bulk-add formats (stage_format_batch)
- When the operator wants several patterns seeded at once ("이 포맷들 한번에 등록해줘"), use `stage_format_batch` with one entry per pattern (`name`, `pattern`, `pass_examples`, `fail_examples`) instead of calling `stage_rule` repeatedly.
- An entry whose name or pattern already exists in the library — or repeats an earlier entry in the same batch — is marked `duplicate` and skipped; an entry whose examples do not verify is marked `invalid`; everything else is `new`.
- Present the result with `present_format_batch`, which shows every entry's outcome and the new/duplicate/invalid counts. Nothing is added to the library until the operator approves the batch on the Formats page.

## Fill or ask the missing slots
- Common gaps: which param on a multi-param API, the exact bound or code list, whether the format should be a named one or a custom pattern.
- One missing fact → default it, set its `confidence` below 0.5, and name it in `assumptions`. More than one → `present_question_form`, then end the turn.

## Stage
- `stage_rule` with `api_id`, `param`, `kind`, the kind's fields, and `summary` in the operator's language.
- One sentence after staging: it is a draft, and it takes effect only once approved on the Rules page — nothing about past runs changes.

## Offer the format to other APIs (outward recommendation)
- After a `stage_rule`/`apply_rule` round trip applies a `format` rule, call `find_apis_with_param` with that same `param` name. It returns every API that declares the param and does not already carry an applied format rule for it.
- If it returns any, offer to extend the same format to them ("이 파라미터, 다른 API에도 있는데 같은 포맷을 적용할까요?") — do not stage anything for them without the operator's say-so; a param name match is not itself a request.

## Recommend rules when the operator does not know the shape (inward recommendation)
- 기준을 모를 때 ("이 파라미터에 뭘 걸어야 할지 모르겠다" 류) → `recommend_rules_for_api` with the target `api_id`. For each of its rule-less params it returns the applied rule(s) peer APIs already carry on a same-named param — never a fabricated constraint from the param name alone; a param with no peer rule gets no suggestion, and that is a valid, complete answer (say so instead of inventing one).
- Present each suggestion (param, source API, the rule) and let the operator pick which to adopt.
- Stage each adopted suggestion individually with `stage_rule`, referencing the peer's format by name — never bulk-copy. Each staged rule goes through its own preview and its own approval (개별 승인) on the Rules page; adopting three suggestions means three separate approvals, not one.
