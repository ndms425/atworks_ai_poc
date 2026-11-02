// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { AttachedItem } from "./types";

/** The identity an attach toggle keys on — the same `kind:ref_id` pair a report's `?attach=` link
 * carries. `order` is deliberately not part of it: the same record attached twice is one
 * attachment, so a second click on a row is a detach rather than a duplicate. */
export function attachKey(kind: AttachedItem["kind"], refId: string): string {
  return `${kind}:${refId}`;
}

/** The AskButton props that make one row's button an attach/detach toggle. Kept here rather than at
 * the six call sites so the key format and the two labels never drift apart between them. */
export function attachToggle(
  attachedRefs: ReadonlySet<string>,
  item: Omit<AttachedItem, "order">,
  onAttach: (item: Omit<AttachedItem, "order">) => void,
): { label: string; on: boolean; onClick: () => void } {
  const on = attachedRefs.has(attachKey(item.kind, item.ref_id));
  return { label: on ? "첨부 해제" : "채팅에 첨부", on, onClick: () => onAttach(item) };
}
