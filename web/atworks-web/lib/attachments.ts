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
): { label: string; on: boolean; onClick: () => void; icon: "pin"; iconOnly: true } {
  const on = attachedRefs.has(attachKey(item.kind, item.ref_id));
  // 글자 없이 아이콘만. 이 버튼은 거의 모든 목록의 **모든 행**에 반복되는데, 행마다 "채팅에
  // 첨부" 다섯 글자가 들어가면 그 폭이 열 하나를 차지해 정작 읽어야 할 경로·이름을 민다.
  // `label`은 사라지지 않고 aria-label과 툴팁으로 남는다.
  return { label: on ? "첨부 해제" : "채팅에 첨부", on, onClick: () => onAttach(item), icon: "pin", iconOnly: true };
}
