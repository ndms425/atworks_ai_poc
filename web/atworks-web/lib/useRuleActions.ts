// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

/**
 * web-shared의 useChangeActions 미러 — 다만 이건 /changes/ 가 아니라 /rules/{id}/{action}으로
 * 나가는 액션을 쥔다 (rule_action은 job_action의 미러지 같은 경로가 아니다). 스트리밍된 rule로
 * 시작해서, 조작 결과 API 응답으로 교체되고, 나중 턴이 payload를 다시 쓰면 재동기화된다.
 */

import { useEffect, useState } from "react";
import type { ValidationRule } from "./types";

export type RuleAction = "apply" | "discard";

export function useRuleActions(
  streamed: ValidationRule,
  onAct?: (ruleId: string, action: RuleAction) => Promise<ValidationRule | null>,
) {
  const [rule, setRule] = useState<ValidationRule>(streamed);
  useEffect(() => {
    setRule(streamed);
  }, [streamed]);
  const [busy, setBusy] = useState<RuleAction | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (action: RuleAction) => {
    if (!onAct || busy) return;
    setBusy(action);
    setError(null);
    const updated = await onAct(rule.change_id, action);
    if (updated) setRule(updated);
    else setError("That action did not go through. Check the API and try again.");
    setBusy(null);
  };

  return { rule, busy, error, act, canAct: rule.status === "staged" };
}
