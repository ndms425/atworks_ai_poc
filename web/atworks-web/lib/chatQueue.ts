// The send queue: what an operator typed while a reply was still streaming. Behaviour follows
// open-design's queued chat sends (Apache-2.0, apps/web/src/components/ProjectView.tsx); the code
// is this portal's own.
//
// - A queued send belongs to the conversation (session) it was typed in, and carries the
//   attachments that were staged at that moment. The screen state is read when it is SENT.
// - The visible conversation drains through the transcript (page.tsx). Any other conversation
//   drains here, in the background: each queued send is one /chat call on that conversation's
//   session, read to its end so the host finishes and saves the turn; nothing reaches the screen,
//   and a background turn sends no screen state (the screen shows a different conversation).
// - The queue is kept in localStorage per operator, so a reload does not lose it.

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { AttachedItem } from "./types";

export interface QueuedSend {
  id: string;
  sessionId: string;
  text: string;
  attachments: AttachedItem[];
  createdAt: number;
}

export type DropEdge = "before" | "after";

const KEY_PREFIX = "atworks-queue:";

function load(operatorId: string): QueuedSend[] {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + operatorId);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(parsed) ? (parsed as QueuedSend[]).filter((q) => q && q.id && q.sessionId && q.text) : [];
  } catch {
    return [];
  }
}

function save(operatorId: string, items: QueuedSend[]): void {
  try {
    if (items.length) localStorage.setItem(KEY_PREFIX + operatorId, JSON.stringify(items));
    else localStorage.removeItem(KEY_PREFIX + operatorId);
  } catch {
    // Storage may be unavailable; the queue still works for this page's lifetime.
  }
}

let seq = 0;
const newId = () => `q-${Date.now().toString(36)}-${(seq++).toString(36)}`;

export interface ChatQueue {
  /** Every queued send of this operator, all conversations, in send order. */
  items: QueuedSend[];
  /** Conversations whose turns are running in the background right now. */
  background: ReadonlySet<string>;
  enqueue: (sessionId: string, text: string, attachments: AttachedItem[]) => void;
  remove: (id: string) => void;
  /** Removes and returns one item (edit: it goes back to the composer). */
  take: (id: string) => QueuedSend | null;
  /** Removes and returns a conversation's next item. */
  shift: (sessionId: string) => QueuedSend | null;
  /** Drag-and-drop reorder within one conversation's queue. */
  move: (dragId: string, targetId: string, edge: DropEdge) => void;
  /**
   * Drains a conversation's queue in the background, after `after` settles (a reply that was
   * detached from the screen mid-stream). Idempotent per conversation.
   */
  runInBackground: (sessionId: string, after?: Promise<void> | null) => void;
  /** Conversations that are running in the background or still have queued sends. */
  busySessions: () => Set<string>;
}

export function useChatQueue(operatorId: string, onBackgroundDone: (sessionId: string) => void): ChatQueue {
  const [items, setItems] = useState<QueuedSend[]>([]);
  const itemsRef = useRef<QueuedSend[]>([]);
  const operatorRef = useRef(operatorId);
  const [background, setBackground] = useState<ReadonlySet<string>>(() => new Set());
  const runningRef = useRef<Set<string>>(new Set());
  const doneRef = useRef(onBackgroundDone);
  doneRef.current = onBackgroundDone;

  const commit = useCallback((next: QueuedSend[]) => {
    itemsRef.current = next;
    setItems(next);
    save(operatorRef.current, next);
  }, []);

  // Each operator has their own queue; switching operator swaps it in.
  useEffect(() => {
    operatorRef.current = operatorId;
    const loaded = load(operatorId);
    itemsRef.current = loaded;
    setItems(loaded);
  }, [operatorId]);

  const enqueue = useCallback(
    (sessionId: string, text: string, attachments: AttachedItem[]) => {
      commit([...itemsRef.current, { id: newId(), sessionId, text, attachments, createdAt: Date.now() }]);
    },
    [commit],
  );

  const take = useCallback(
    (id: string) => {
      const found = itemsRef.current.find((q) => q.id === id) ?? null;
      if (found) commit(itemsRef.current.filter((q) => q.id !== id));
      return found;
    },
    [commit],
  );

  const remove = useCallback((id: string) => void take(id), [take]);

  const shift = useCallback(
    (sessionId: string) => {
      const found = itemsRef.current.find((q) => q.sessionId === sessionId) ?? null;
      if (found) commit(itemsRef.current.filter((q) => q.id !== found.id));
      return found;
    },
    [commit],
  );

  const move = useCallback(
    (dragId: string, targetId: string, edge: DropEdge) => {
      if (dragId === targetId) return;
      const list = [...itemsRef.current];
      const from = list.findIndex((q) => q.id === dragId);
      if (from < 0) return;
      const [dragged] = list.splice(from, 1);
      const at = list.findIndex((q) => q.id === targetId);
      if (at < 0 || list[at].sessionId !== dragged.sessionId) return;
      list.splice(edge === "before" ? at : at + 1, 0, dragged);
      commit(list);
    },
    [commit],
  );

  const runInBackground = useCallback(
    (sessionId: string, after?: Promise<void> | null) => {
      if (runningRef.current.has(sessionId)) return;
      runningRef.current.add(sessionId);
      setBackground(new Set(runningRef.current));
      void (async () => {
        try {
          if (after) await after;
          for (;;) {
            const next = shift(sessionId);
            if (!next) break;
            try {
              // Read to the end: the host streams the turn and saves it when the stream ends.
              for await (const event of api.chatStreamFor(sessionId, next.text, next.attachments, null)) {
                void event;
              }
            } catch {
              // The host is unreachable: put the send back at the front and stop, rather than
              // dropping it or retrying in a tight loop. It drains on the next attempt.
              commit([next, ...itemsRef.current]);
              break;
            }
          }
        } finally {
          runningRef.current.delete(sessionId);
          setBackground(new Set(runningRef.current));
          doneRef.current(sessionId);
        }
      })();
    },
    [shift, commit],
  );

  const busySessions = useCallback(() => {
    const busy = new Set(runningRef.current);
    for (const q of itemsRef.current) busy.add(q.sessionId);
    return busy;
  }, []);

  return { items, background, enqueue, remove, take, shift, move, runInBackground, busySessions };
}
