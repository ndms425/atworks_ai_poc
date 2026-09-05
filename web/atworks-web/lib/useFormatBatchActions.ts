// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

/**
 * useRuleActions의 미러 — 다만 이건 /rules/ 도 /changes/ 도 아니라 /format-batches/{id}/{action}으로
 * 나가는 액션을 쥔다 (format_batch_action은 rule_action의 미러지 같은 경로가 아니다). 스트리밍된
 * batch로 시작해서, 조작 결과 API 응답으로 교체되고, 나중 턴이 payload를 다시 쓰면 재동기화된다.
 */

import { useEffect, useState } from "react";
import type { FormatBatch } from "./types";

export type FormatBatchAction = "apply" | "discard";

export function useFormatBatchActions(
  streamed: FormatBatch,
  onAct?: (batchId: string, action: FormatBatchAction) => Promise<FormatBatch | null>,
) {
  const [batch, setBatch] = useState<FormatBatch>(streamed);
  useEffect(() => {
    setBatch(streamed);
  }, [streamed]);
  const [busy, setBusy] = useState<FormatBatchAction | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (action: FormatBatchAction) => {
    if (!onAct || busy) return;
    setBusy(action);
    setError(null);
    const updated = await onAct(batch.change_id, action);
    if (updated) setBatch(updated);
    else setError("That action did not go through. Check the API and try again.");
    setBusy(null);
  };

  return { batch, busy, error, act, canAct: batch.status === "staged" };
}
