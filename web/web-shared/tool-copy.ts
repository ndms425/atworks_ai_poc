// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

const TOOL_COPY: Record<string, string> = {
  search_products: "Searching the catalog…",
  get_product_details: "Reading the details…",
  search_policies: "Checking the policies…",
  get_orders: "Looking up your orders…",
  get_order_status: "Looking up your orders…",
  get_fulfillment_options: "Checking delivery and pickup options…",
  get_cart: "Checking the cart…",
  add_to_cart: "Updating the cart…",
  update_cart_item: "Updating the cart…",
  remove_from_cart: "Updating the cart…",
  checkout: "Preparing your order summary…",
  get_preferences: "Reading your profile…",
  recall_memories: "Recalling what you've told me…",
  save_memory: "Saving that for next time…",
  load_skill: "Loading…",
  web_search: "Searching the web…",
  get_business_snapshot: "Reading the business snapshot…",
  query_metrics: "Querying metrics…",
  search_listings: "Searching the listings…",
  get_listing: "Reading the listing…",
  get_inventory_alerts: "Checking inventory alerts…",
  get_order_issues: "Checking order issues…",
  get_pricing_context: "Reading the pricing context…",
  get_campaign_performance: "Reading campaign performance…",
  get_pending_changes: "Checking pending changes…",
  run_analysis: "Running the analysis…",
  apply_change: "Applying the change…",
  discard_change: "Discarding the change…",
};

// aTworks' own 42 tools. The map above is the reference commerce agent's and names none of
// them, so without these every aTworks call fell through to the English catch-alls below.
const ATWORKS_COPY: Record<string, string> = {
  load_skill: "스킬을 불러오는 중…",
  search_apis: "API를 찾는 중…",
  get_api: "API 명세를 읽는 중…",
  list_runs: "실행 이력을 읽는 중…",
  get_run: "실행 기록을 읽는 중…",
  rank_failed_runs: "위험도를 매기는 중…",
  aggregate_runs: "집계하는 중…",
  query_runs: "질의를 돌리는 중…",
  note_unmet_ask: "답하지 못한 질문을 기록하는 중…",
  propose_alias: "용어 해석을 확인받는 중…",
  get_pending_jobs: "승인 대기 job을 확인하는 중…",
  stage_job: "실행 계획을 짜는 중…",
  apply_job: "job을 적용하는 중…",
  discard_job: "job을 버리는 중…",
  stage_rule: "검증 규칙 초안을 쓰는 중…",
  apply_rule: "규칙을 적용하는 중…",
  discard_rule: "규칙을 버리는 중…",
  get_pending_rules: "승인 대기 규칙을 확인하는 중…",
  find_apis_with_param: "같은 파라미터를 쓰는 API를 찾는 중…",
  recommend_rules_for_api: "규칙을 추천하는 중…",
  stage_format_batch: "형식 묶음을 짜는 중…",
  apply_format_batch: "형식 묶음을 적용하는 중…",
  discard_format_batch: "형식 묶음을 버리는 중…",
  get_pending_format_batches: "승인 대기 형식을 확인하는 중…",
  stage_profile: "비교 프로파일을 짜는 중…",
  apply_profile: "프로파일을 적용하는 중…",
  discard_profile: "프로파일을 버리는 중…",
  get_pending_profiles: "승인 대기 프로파일을 확인하는 중…",
  recommend_ignore_paths: "무시할 필드를 추리는 중…",
  present_run_digest: "실행 요약을 만드는 중…",
  present_run_groups: "원인별로 묶는 중…",
  present_query_table: "표를 그리는 중…",
  present_job_preview: "실행 계획 카드를 만드는 중…",
  present_rule_preview: "규칙 카드를 만드는 중…",
  present_format_batch: "형식 카드를 만드는 중…",
  present_parity_summary: "비교 결과를 정리하는 중…",
  present_profile_preview: "프로파일 카드를 만드는 중…",
  present_question_form: "물어볼 것을 정리하는 중…",
  navigate_screen: "화면을 옮기는 중…",
  highlight_screen: "화면에서 가리키는 중…",
  present_plan: "단계를 알리는 중…",
  present_suggestions: "다음 질문을 고르는 중…",
};

const QUERY_TOOLS = new Set([
  "search_products",
  "search_policies",
  "search_listings",
  "web_search",
  "search_apis",
]);

export function describeToolCall(tool: string, input: Record<string, unknown> = {}): string {
  const copy = ATWORKS_COPY[tool] ?? TOOL_COPY[tool];
  const query = typeof input.query === "string" ? input.query.trim() : "";
  if (copy && query && QUERY_TOOLS.has(tool)) {
    return `${copy.replace(/…$/, "")} · “${query}”`;
  }
  if (copy) return copy;
  if (tool.startsWith("present_")) return "답을 정리하는 중…";
  if (tool.startsWith("stage_")) return "승인받을 변경을 짜는 중…";
  return `${tool.replaceAll("_", " ")}…`;
}
