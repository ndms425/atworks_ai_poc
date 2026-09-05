// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef } from "react";
import { ApproveBar, ChangeStatusPill, formatDate, Notice, PageHeader, Panel, Pill, plural, Skeleton, useResource } from "web-shared";
import { fetchRules } from "@/lib/api";
import { type RuleAction, useRuleActions } from "@/lib/useRuleActions";
import FormatsView from "@/components/views/FormatsView";
import ProfilesView from "@/components/views/ProfilesView";
import type { ScreenFilter, ScreenIntent, ScreenTarget, ValidationRule } from "@/lib/types";

function RuleRow({
  rule,
  onAct,
}: {
  rule: ValidationRule;
  onAct: (id: string, action: RuleAction) => Promise<ValidationRule | null>;
}) {
  const { rule: change, busy, error, act, canAct } = useRuleActions(rule, onAct);
  return (
    <li data-ref={`rule:${change.rule_id}`} className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{change.message}</div>
          <div className="mt-0.5 text-[12px] tabular-nums text-(--ink-soft)">
            {change.api_id} · {change.param}
            {change.status === "applied" ? (
              <>
                {" · "}
                {change.effective_from ? formatDate(change.effective_from) : formatDate(change.applied_at ?? change.created_at)}
                {change.applied_by ? ` · ${change.applied_by}` : ""}
              </>
            ) : (
              <> · {formatDate(change.created_at)}</>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {change.review_required ? <Pill tone="warn">직접 검토 필요</Pill> : null}
          <ChangeStatusPill status={change.status} />
        </div>
      </div>
      <ApproveBar change={change} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </li>
  );
}

export default function RulesView({
  refreshKey,
  onAct,
  intent,
  onScreen,
}: {
  refreshKey: number;
  onAct: (id: string, action: RuleAction) => Promise<ValidationRule | null>;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const { data, failed } = useResource(fetchRules, [refreshKey]);
  const rules = data?.rules ?? [];
  const staged = rules.filter((rule) => rule.status === "staged");
  const applied = rules.filter((rule) => rule.status === "applied");
  const discarded = rules.filter((rule) => rule.status === "discarded");

  // RulesView has no filter of its own, so there's nothing to apply-once by nonce — just stash
  // the focus target. It's read from a ref (not straight off `intent`) in the scroll effect
  // below, because page.tsx clears `screenIntent` right after this view's first onScreen report —
  // which fires on mount, often before the fetch below has resolved — so `intent` itself can go
  // null before there's anything to scroll to.
  const appliedNonceRef = useRef<number | undefined>(undefined);
  const pendingFocusRef = useRef<{ kind: string; ref_id: string } | null>(null);
  useEffect(() => {
    if (!intent || intent.nonce === appliedNonceRef.current) return;
    appliedNonceRef.current = intent.nonce;
    pendingFocusRef.current = intent.focus ?? null;
  }, [intent]);

  // Scroll to the pending focus target once its row exists. Re-runs whenever the row set changes
  // (i.e. once the fetch resolves), plus a short retry for the case where the DOM hasn't
  // committed the new rows yet when this effect fires.
  useEffect(() => {
    const focus = pendingFocusRef.current;
    if (!focus) return;
    const find = () => document.querySelector(`[data-ref="${focus.kind}:${focus.ref_id}"]`);
    const found = find();
    if (found) {
      found.scrollIntoView({ block: "center", behavior: "smooth" });
      pendingFocusRef.current = null;
      return;
    }
    const timer = setTimeout(() => {
      find()?.scrollIntoView({ block: "center", behavior: "smooth" });
      pendingFocusRef.current = null;
    }, 150);
    return () => clearTimeout(timer);
  }, [rules]);

  // Report what's actually on screen so api.screenState stays current for the next chat turn.
  useEffect(() => {
    onScreen?.({ visible: rules.slice(0, 40).map((rule) => ({ kind: "rule", ref_id: rule.rule_id, label: rule.message })) });
  }, [rules, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader title="Rules" subtitle={data ? plural(rules.length, "rule") : undefined} />
      {failed && !data ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so rules can&apos;t load.</Notice>
      ) : !data ? (
        <Skeleton className="h-96" />
      ) : rules.length === 0 ? (
        <Notice>등록된 규칙이 없습니다.</Notice>
      ) : (
        <>
          {staged.length > 0 ? (
            <Panel title="승인 대기">
              <ul className="divide-y divide-(--line)">
                {staged.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onAct={onAct} />
                ))}
              </ul>
            </Panel>
          ) : null}
          {applied.length > 0 ? (
            <Panel title="적용됨">
              <ul className="divide-y divide-(--line)">
                {applied.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onAct={onAct} />
                ))}
              </ul>
            </Panel>
          ) : null}
          {discarded.length > 0 ? (
            <Panel title="기각됨">
              <ul className="divide-y divide-(--line)">
                {discarded.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onAct={onAct} />
                ))}
              </ul>
            </Panel>
          ) : null}
        </>
      )}
      <FormatsView refreshKey={refreshKey} />
      <ProfilesView refreshKey={refreshKey} />
    </div>
  );
}
