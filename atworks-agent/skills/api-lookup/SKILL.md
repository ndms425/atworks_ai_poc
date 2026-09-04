---
name: api-lookup
description: Finding registered APIs and reading their specs, params, and value rules; answering what an API or a rule is. / API 검색과 스펙·파라미터·검증 규칙 확인.
---

# API lookup

- `search_apis` by text, group, or `updated_after`; `get_api` for one record. Quote paths and names exactly as returned.
- Rules (`has_rules`) are registered in aTworks; describe them from the record and say when an API has none.
- Chips: run this API now (hand-off to schedule-run), show recent runs of it (hand-off to failed-triage).
