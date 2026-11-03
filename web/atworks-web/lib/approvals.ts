// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { AssistantChatItem, AssistantSegment } from "web-shared";
import type { PortalViewId } from "@/lib/types";

/**
 * 채팅 거품 안에 승인 카드를 그대로 펼치는 상한.
 *
 * 서버의 `AtworksAgentConfig.max_inline_approvals`와 같은 값이어야 한다 —
 * `host/tests/test_bulk_approve.py`의 드리프트 테스트가 이 줄을 읽어 설정값과 비교한다.
 * 상한을 넘으면 카드를 **몇 건만 남기지 않는다**: 전부 접고, 그 화면으로 보낸다. 남겨 둔
 * 카드 5장은 "여기서 다 할 수 있다"는 잘못된 신호고, 나머지 N건은 화면에만 있기 때문이다.
 */
export const MAX_INLINE_APPROVALS = 5;

type ApprovalView = Extract<PortalViewId, "jobs" | "rules">;

/** 승인 버튼을 단 카드와 그 카드가 속한 화면. 여기 없는 컴포넌트는 접기 대상이 아니다. */
const APPROVAL_CARDS: Record<string, { view: ApprovalView; noun: string }> = {
  job_preview: { view: "jobs", noun: "job" },
  rule_preview: { view: "rules", noun: "검증 규칙" },
  format_batch: { view: "rules", noun: "형식 묶음" },
  profile_preview: { view: "rules", noun: "비교 프로파일" },
};

export interface ApprovalOverflowGroup {
  view: ApprovalView;
  total: number;
  /** 아직 staged인 건수. 다시 연 대화에서는 0일 수 있다. */
  pending: number;
  kinds: { noun: string; total: number }[];
}

export interface ApprovalOverflowPayload {
  total: number;
  pending: number;
  groups: ApprovalOverflowGroup[];
}

export const APPROVAL_OVERFLOW = "approval_overflow";

/**
 * 카드가 지금 승인을 기다리는지. 네 종류 모두 레코드에 `status`를 달고 있고, 다시 연 대화는
 * `change`에 오늘의 레코드가 얹혀 오므로 그쪽을 먼저 본다(카드 컴포넌트들과 같은 순서).
 */
function isPending(payload: unknown): boolean {
  const bag = payload as Record<string, { status?: unknown } | undefined> | null | undefined;
  if (!bag) return false;
  for (const key of ["change", "job", "rule", "batch", "profile"]) {
    const record = bag[key];
    if (record && typeof record.status === "string") return record.status === "staged";
  }
  return false;
}

/**
 * 표시 전용 변환. 한 턴이 승인 카드를 `MAX_INLINE_APPROVALS`건보다 많이 내놓으면 그 카드를
 * **전부** 빼고, 첫 카드 자리에 안내 블록 하나를 넣는다. 세그먼트만 바꿀 뿐 턴의 기록
 * (`chat.items`, 대화 저장본, 감사 로그)은 건드리지 않는다.
 *
 * 턴이 흐르는 중에도 적용한다: 건수는 줄지 않으므로 6번째 카드에서 한 번 접히고 되돌아오지
 * 않는다. 접힌 뒤 응답이 끝나기를 기다렸다가 다시 펼치는 깜빡임은 생기지 않는다.
 */
export function collapseApprovals(item: AssistantChatItem): AssistantChatItem {
  const dests = item.segments.map((segment) =>
    segment.type === "ui" ? (APPROVAL_CARDS[segment.block.component] ?? null) : null,
  );
  const total = dests.filter(Boolean).length;
  if (total <= MAX_INLINE_APPROVALS) return item;

  const groups: ApprovalOverflowGroup[] = [];
  let pending = 0;
  item.segments.forEach((segment, index) => {
    const dest = dests[index];
    if (!dest || segment.type !== "ui") return;
    const live = isPending(segment.block.payload);
    if (live) pending += 1;
    let group = groups.find((candidate) => candidate.view === dest.view);
    if (!group) {
      group = { view: dest.view, total: 0, pending: 0, kinds: [] };
      groups.push(group);
    }
    group.total += 1;
    if (live) group.pending += 1;
    const kind = group.kinds.find((candidate) => candidate.noun === dest.noun);
    if (kind) kind.total += 1;
    else group.kinds.push({ noun: dest.noun, total: 1 });
  });

  const payload: ApprovalOverflowPayload = { total, pending, groups };
  const segments: AssistantSegment[] = [];
  let placed = false;
  item.segments.forEach((segment, index) => {
    if (!dests[index]) {
      segments.push(segment);
      return;
    }
    if (placed) return;
    placed = true;
    segments.push({
      type: "ui",
      slotKey: "approval-overflow",
      status: "final",
      block: { component: APPROVAL_OVERFLOW, payload },
    });
  });
  return { ...item, segments };
}
