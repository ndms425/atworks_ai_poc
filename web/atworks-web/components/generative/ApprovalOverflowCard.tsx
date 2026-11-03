// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { Button, GenCard, GenCardHeader, Pill } from "web-shared";
import { type ApprovalOverflowPayload, MAX_INLINE_APPROVALS } from "@/lib/approvals";
import type { PortalViewId } from "@/lib/types";

const VIEW_LABEL: Record<string, string> = { jobs: "Jobs", rules: "Rules" };

/**
 * 승인 카드가 상한을 넘은 턴을 대신하는 안내. 카드 자리에 들어가지만 승인 버튼은 없다 —
 * 일부러다. 여기서 몇 건을 처리할 수 있게 두면 나머지 N건이 조용히 묻힌다. 처리는 전부
 * 한 화면에서 하고(그 화면에는 전체 승인·반려가 있다), 이 블록은 거기로 보내는 일만 한다.
 */
export default function ApprovalOverflowCard({
  payload,
  onOpenView,
}: {
  payload: ApprovalOverflowPayload;
  onOpenView?: (view: PortalViewId) => void;
}) {
  const { total, pending, groups } = payload;
  return (
    <GenCard>
      <GenCardHeader
        title={`승인 대상 ${total}건`}
        meta={
          <>
            {pending > 0 ? (
              <Pill tone="warn" dot>
                대기 {pending}건
              </Pill>
            ) : (
              <Pill tone="muted">모두 처리됨</Pill>
            )}
            <span aria-hidden>·</span>
            <span>채팅에 펼치는 상한 {MAX_INLINE_APPROVALS}건</span>
          </>
        }
      />
      <p className="px-3.5 pb-1 pt-1.5 text-[12.5px] leading-relaxed text-(--ink-soft)">
        {pending > 0
          ? "한 건씩 카드로 처리하기에는 많습니다. 아래 화면에서 목록으로 확인하고 전체 승인·반려할 수 있습니다."
          : "이미 처리된 건이라 카드를 펼치지 않았습니다. 결과는 아래 화면에서 확인하세요."}
      </p>
      <ul>
        {groups.map((group) => (
          <li key={group.view} className="flex items-center justify-between gap-3 border-t border-(--line) px-3.5 py-2.5">
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-(--ink)">{VIEW_LABEL[group.view] ?? group.view}</p>
              <p className="truncate text-[12px] text-(--ink-soft)">
                {group.kinds.map((kind) => `${kind.noun} ${kind.total}건`).join(" · ")}
                {group.pending > 0 ? ` · 대기 ${group.pending}건` : " · 처리 완료"}
              </p>
            </div>
            <Button
              variant={group.pending > 0 ? "accent" : "secondary"}
              size="sm"
              onClick={() => onOpenView?.(group.view)}
            >
              화면에서 처리
            </Button>
          </li>
        ))}
      </ul>
    </GenCard>
  );
}
