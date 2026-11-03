// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useState } from "react";
import { Button, Pill } from "web-shared";
import { bulkAct, type BulkResult } from "@/lib/api";

/**
 * 대기 중인 항목 전체를 한 번에 승인하거나 반려한다.
 *
 * 서버가 대기 큐를 열거해 단건 승인 경로를 건건이 되풀이하므로, 승인 하나하나는 평소와 똑같이
 * 마크가 붙었다 떨어지고 감사 2행을 남긴다. 여기는 그 라우트를 부르는 버튼일 뿐이다.
 *
 * 확인 대화상자를 거치는 이유: 되돌릴 수 없다. 승인된 job은 스케줄러가 바로 집어갈 수 있어
 * 일괄 반려로 되돌려지지 않는다. `pending`을 함께 보내 서버의 지금 큐와 다르면 409를 받고
 * 아무것도 승인하지 않는다 — 모델이 방금 올린 항목이 "전체"에 섞여 들어가지 않게 한다.
 */
export default function BulkApproveBar({
  surface,
  pending,
  noun,
  onDone,
}: {
  surface: "changes" | "rules" | "profiles" | "format-batches";
  /** 승인 대기 건수. 0이면 아무것도 렌더하지 않는다. */
  pending: number;
  /** 확인 문구에 쓰는 이름 — "대기 중인 12건의 job을 모두 승인합니다". */
  noun: string;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState<"apply" | "discard" | null>(null);
  const [result, setResult] = useState<string | null>(null);

  if (pending === 0) return null;

  const run = async (action: "apply" | "discard") => {
    const verb = action === "apply" ? "승인" : "반려";
    if (!window.confirm(`대기 중인 ${noun} ${pending}건을 모두 ${verb}합니다. 되돌릴 수 없습니다. 계속할까요?`)) return;
    setBusy(action);
    setResult(null);
    try {
      const data: BulkResult | null = await bulkAct(surface, action, pending);
      if (!data) {
        // 409를 포함해 서버가 거절한 경우. 큐가 바뀌었으니 목록을 다시 읽는 것이 맞는 응답이다.
        setResult(`${verb}하지 못했습니다. 목록을 다시 읽습니다.`);
      } else {
        const parts = [`${data.applied}건 ${verb}`];
        if (data.failed.length) parts.push(`${data.failed.length}건 실패`);
        if (data.remaining) parts.push(`${data.remaining}건 남음 (1회 상한 ${data.cap}건)`);
        setResult(parts.join(" · "));
      }
    } finally {
      setBusy(null);
      onDone();
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Pill tone="warn" dot>승인 대기 {pending}건</Pill>
      <Button variant="accent" size="sm" icon="check" disabled={busy !== null} onClick={() => void run("apply")}>
        {busy === "apply" ? "승인 중…" : "전체 승인"}
      </Button>
      <Button variant="secondary" size="sm" disabled={busy !== null} onClick={() => void run("discard")}>
        {busy === "discard" ? "반려 중…" : "전체 반려"}
      </Button>
      {result ? <span className="text-[12.5px] text-(--ink-soft)">{result}</span> : null}
    </div>
  );
}
