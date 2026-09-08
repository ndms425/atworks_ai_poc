---
name: api-lookup
description: Finding registered APIs and reading their specs, params, and value rules; answering what an API or a rule is. / API 검색과 스펙·파라미터·검증 규칙 확인.
---

# API lookup

- `search_apis` by text, group, or `updated_after`; `get_api` for one record. Quote paths and names exactly as returned.
- 'api-001 상세 보여줘' is `navigate_screen{view: apis, focus}`, not a card.
- Rules (`has_rules`) are registered in aTworks; describe them from the record and say when an API has none.
- Chips: run this API now (hand-off to schedule-run), show recent runs of it (hand-off to failed-triage).

## Beyond the fixed axes
- When the operator asks for figures over a set of APIs and the grouping or filter is outside `aggregate_runs`' five axes — by endpoint segment, by HTTP method, by API group, by week or by day, by operator, two axes at once, or this window against the one before it — call `query_runs` with that spec and show the result with `present_query_table`. `aggregate_runs` stays the tool for the axes it already covers.
- When the catalogue cannot express it either, call `note_unmet_ask(reason, summary, wanted)` FIRST, then say in one sentence what data or axis would make it answerable. An answer that ends on "지원하지 않습니다 / not supported" without `note_unmet_ask` is a rule violation.
- When you read one of the operator's own words as a filter value ("결제 계열" → a `path_prefix`), say so in one clause so they can correct it.
