// Saved conversations: the host's /conversations routes, and the replay of a saved turn's display
// record into the transcript's ChatItems. One session is one conversation, so "new chat" is just a
// fresh session and "open" switches the portal's session id to the chosen one.

import type { AssistantSegment, ChatItem, SentAttachment } from "web-shared";
import { api } from "./api";
import type { AttachedItem } from "./types";

export interface ConversationSummary {
  id: string;
  title: string;
  turn_count: number;
  created_at: string;
  updated_at: string;
  /** True for the conversation the portal's current session is. */
  current?: boolean;
}

type SavedSegment =
  | { type: "text"; text: string }
  | { type: "error"; text: string }
  | { type: "ui"; component: string; payload: unknown };

export interface SavedTurn {
  turn: number;
  user_text: string;
  at: string;
  segments: SavedSegment[];
  suggestions: string[];
  /** 그 턴이 달고 나갔던 첨부(표시용 요약). 첨부 없이 보낸 턴에는 키가 없다. */
  attachments?: SentAttachment[];
}

export const fetchConversations = (query = "") =>
  api.getPage<ConversationSummary>("/conversations", { q: query });

export const fetchConversation = (id: string) =>
  api.get<{ conversation: ConversationSummary; turns: SavedTurn[] }>(`/conversations/${encodeURIComponent(id)}`);

export const renameConversation = (id: string, title: string) =>
  api.patch<{ conversation: ConversationSummary }>(`/conversations/${encodeURIComponent(id)}`, { title });

export const deleteConversation = (id: string) =>
  api.delete<{ ok: boolean; current_deleted: boolean }>(`/conversations/${encodeURIComponent(id)}`);

/** Rebuilds the transcript the portal showed: each card keeps the slot key the live stream would
 *  have given it (`turn-component-ordinal`), and `changeIds` lists the turn's previewed changes so
 *  a later change_update still finds its card. */
export function itemsFromTurns(turns: SavedTurn[]): ChatItem[] {
  const items: ChatItem[] = [];
  for (const turn of turns) {
    items.push({
      kind: "user",
      text: turn.user_text,
      ...(turn.attachments?.length ? { attachments: turn.attachments } : {}),
    });
    const ordinals = new Map<string, number>();
    const changeIds: string[] = [];
    const segments: AssistantSegment[] = turn.segments.map((segment) => {
      if (segment.type !== "ui") return segment;
      const ordinal = ordinals.get(segment.component) ?? 0;
      ordinals.set(segment.component, ordinal + 1);
      const changeId = (segment.payload as { change_id?: unknown } | null)?.change_id;
      if (segment.component === "job_preview" && typeof changeId === "string") changeIds.push(changeId);
      return {
        type: "ui",
        block: { component: segment.component, payload: segment.payload },
        slotKey: `${turn.turn}-${segment.component}-${ordinal}`,
        status: "final",
      };
    });
    items.push({
      kind: "assistant",
      turn: turn.turn,
      segments,
      suggestions: turn.suggestions,
      changeIds: changeIds.length ? changeIds : undefined,
      pending: false,
      tools: [],
    });
  }
  return items;
}

const LAST_KEY_PREFIX = "atworks-conversation:";

/** The conversation an operator last had open, so a reload brings it back. */
export function readLastConversation(operatorId: string): string | null {
  try {
    return localStorage.getItem(LAST_KEY_PREFIX + operatorId);
  } catch {
    return null;
  }
}

export function writeLastConversation(operatorId: string, sessionId: string | null): void {
  try {
    if (sessionId) localStorage.setItem(LAST_KEY_PREFIX + operatorId, sessionId);
    else localStorage.removeItem(LAST_KEY_PREFIX + operatorId);
  } catch {
    // Storage may be unavailable; reopening after a reload is a convenience, not a requirement.
  }
}

const ATTACH_DRAFT_PREFIX = "atworks-attach-draft:";

/** The attachments staged (not yet sent) in a conversation. */
export function readDraftAttachments(sessionId: string): AttachedItem[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(ATTACH_DRAFT_PREFIX + sessionId) ?? "[]") as unknown;
    return Array.isArray(parsed) ? (parsed as AttachedItem[]) : [];
  } catch {
    return [];
  }
}

export function writeDraftAttachments(sessionId: string, items: AttachedItem[]): void {
  try {
    if (items.length) localStorage.setItem(ATTACH_DRAFT_PREFIX + sessionId, JSON.stringify(items));
    else localStorage.removeItem(ATTACH_DRAFT_PREFIX + sessionId);
  } catch {
    // Storage may be unavailable; the attachments simply do not survive a reload.
  }
}
