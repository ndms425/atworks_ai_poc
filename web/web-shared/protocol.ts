// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** Event shapes mirror commerce_common/streaming.py. */

export type AgentEventType =
  | "text_delta"
  | "tool_call"
  | "tool_result"
  | "ui"
  | "ui_partial"
  | "cart_update"
  | "change_update"
  | "progress"
  | "turn_complete"
  | "error";

export interface AgentEvent {
  type: AgentEventType;
  data: Record<string, unknown>;
}

/** `tool_call` data. `label` is the model's few words for the person waiting; presentation calls carry none. */
export interface ToolCallData {
  tool: string;
  id: string;
  input: Record<string, unknown>;
  label?: string;
}

/** Mirrors shopping_agent/types.py `Order`. */
export interface Order {
  order_id: string;
  status: string;
  placed_at: string;
  items: { product_id: string; title: string; quantity: number; price: number; option_values?: Record<string, string>; variant_of?: string | null }[];
  total: number;
  currency?: string;
  estimated_delivery?: string | null;
  tracking_url?: string | null;
}

export interface MemoryFact {
  key: string;
  value: string;
  category: string;
  updated_at?: string | null;
}

/** Each app narrows `payload` in its registry. */
export interface UIBlock {
  component: string;
  payload: unknown;
}

/** The two reserved `ui` components a directive turn emits; never rendered as a card. */
export const SCREEN_DIRECTIVE_COMPONENTS = ["screen_navigate", "screen_highlight"] as const;

/** Transport-level shape; apps narrow `payload` against their own typed interfaces. */
export type ScreenDirective = { kind: "navigate" | "highlight"; payload: Record<string, unknown> };

export interface TraceEntry {
  kind: "tool_call" | "tool_result" | "turn_complete" | "error";
  turn: number;
  label: string;
  detail?: string;
  isError?: boolean;
  /** "blocked" when a gate held the call; `reason` names the gate. */
  status?: string;
  reason?: string;
  /** tool_result only: the head of a long result, sent when `detail` is the "ok" summary. */
  excerpt?: string;
  /** performance.now() at arrival. */
  at: number;
  /** turn_complete only. */
  elapsedMs?: number;
}

/** `retrying`: the attempt failed validation; its last frame stays until a retry adopts it. */
export type UISlotStatus = "pending" | "partial" | "retrying" | "final";

export type AssistantSegment =
  | { type: "text"; text: string }
  | { type: "error"; text: string }
  | {
      type: "ui";
      block: UIBlock;
      slotKey: string;
      status: UISlotStatus;
    };

export type UISegment = Extract<AssistantSegment, { type: "ui" }>;

/** 질문과 함께 나간 첨부의 표시용 요약 — 보낸 뒤에도 어떤 턴이 무엇을 달고 갔는지 남긴다. */
export interface SentAttachment {
  kind: "run" | "api" | "job";
  ref_id: string;
  label: string;
}

export interface UserChatItem {
  kind: "user";
  text: string;
  /**
   * 이 질문이 달고 나간 첨부. 전송이 끝나면 입력창 위의 칩은 사라지므로, 그 턴이 첨부를
   * 실어 보냈는지 확인할 수 있는 곳은 여기뿐이다 — 없으면 "첨부가 안 갔나?"를 되짚을
   * 방법이 없고, 모델이 엉뚱한 것을 답했을 때 원인을 가릴 수 없다.
   */
  attachments?: SentAttachment[];
}

export interface AssistantChatItem {
  kind: "assistant";
  turn: number;
  segments: AssistantSegment[];
  suggestions: string[];
  /** Merchant portals: changes previewed in this reply. */
  changeIds?: string[];
  suggestionsStale?: boolean;
  pending: boolean;
  tools: string[];
  /**
   * One row per tool call, in call order — what the step list renders. `tools` stays a bare
   * name list because the diagnostic and the exported log read it; this carries what a person
   * needs to SEE a step: the model's own line for the call, and when it ran.
   */
  steps?: TurnStep[];
  /**
   * Status line for the call in flight; cleared when prose or a card lands, or when the
   * tool a progress line names returns.
   */
  activity?: string;
}

/**
 * A tool call as the operator sees it. `doneAt` is stamped by the tool's own result — never by
 * the model saying it finished — so a step ticks off only when the work actually came back.
 */
export interface TurnStep {
  tool: string;
  /** The line shown for this step: the model's status line for the call, else the stock copy. */
  label: string;
  startedAt: number;
  doneAt?: number;
  /** The result came back an error: the step stops rather than spinning forever. */
  failed?: boolean;
}

export type ChatItem = UserChatItem | AssistantChatItem;
