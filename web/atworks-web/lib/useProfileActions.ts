// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

/**
 * useFormatBatchActions의 미러 — 다만 이건 /format-batches/ 도 /rules/ 도 아니라
 * /profiles/{id}/{action}으로 나가는 액션을 쥔다 (profile_action은 rule_action/format_batch_action의
 * 미러지 같은 경로가 아니다). 스트리밍된 profile로 시작해서, 조작 결과 API 응답으로 교체되고,
 * 나중 턴이 payload를 다시 쓰면 재동기화된다.
 */

import { useEffect, useState } from "react";
import type { ComparisonProfile } from "./types";

export type ProfileAction = "apply" | "discard";

export function useProfileActions(
  streamed: ComparisonProfile,
  onAct?: (profileId: string, action: ProfileAction) => Promise<ComparisonProfile | null>,
) {
  const [profile, setProfile] = useState<ComparisonProfile>(streamed);
  useEffect(() => {
    setProfile(streamed);
  }, [streamed]);
  const [busy, setBusy] = useState<ProfileAction | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (action: ProfileAction) => {
    if (!onAct || busy) return;
    setBusy(action);
    setError(null);
    const updated = await onAct(profile.change_id, action);
    if (updated) setProfile(updated);
    else setError("That action did not go through. Check the API and try again.");
    setBusy(null);
  };

  return { profile, busy, error, act, canAct: profile.status === "staged" };
}
