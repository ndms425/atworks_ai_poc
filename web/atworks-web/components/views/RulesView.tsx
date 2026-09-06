// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect } from "react";
import { ApproveBar, ChangeStatusPill, formatDate, Notice, PageHeader, Panel, Pill, plural, Skeleton } from "web-shared";
import { fetchRules } from "@/lib/api";
import Pager from "@/components/Pager";
import { usePagedList } from "@/lib/usePagedList";
import { type RuleAction, useRuleActions } from "@/lib/useRuleActions";
import { useScreenFocus } from "@/lib/useScreenFocus";
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
  // One paged read over the whole rule ledger; the three panels below split the CURRENT PAGE by
  // status. The header's count is the envelope's `total`, so it stays honest even though a status
  // group's panel only ever shows this page's share of it.
  const load = useCallback((cursor: string | null) => fetchRules(cursor), []);
  const page = usePagedList<ValidationRule>(load, "rules", [refreshKey]);
  const rules = page.items;
  const staged = rules.filter((rule) => rule.status === "staged");
  const applied = rules.filter((rule) => rule.status === "applied");
  const discarded = rules.filter((rule) => rule.status === "discarded");

  // RulesView has no filter of its own, so there's nothing to apply-once by nonce — the shared
  // hook handles the whole focus/scroll lifecycle.
  useScreenFocus(intent, "rule", rules.length > 0);

  // Report what's actually on screen so api.screenState stays current for the next chat turn:
  // the CURRENT PAGE (sliced to 40), which is exactly what the operator can see and point at.
  useEffect(() => {
    onScreen?.({ visible: rules.slice(0, 40).map((rule) => ({ kind: "rule", ref_id: rule.rule_id, label: rule.message })) });
  }, [rules, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader title="Rules" subtitle={page.loaded ? plural(page.total, "rule") : undefined} />
      {page.failed && !page.loaded ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so rules can&apos;t load.</Notice>
      ) : !page.loaded ? (
        <Skeleton className="h-96" />
      ) : rules.length === 0 ? (
        <Notice>등록된 규칙이 없습니다.</Notice>
      ) : (
        <>
          {staged.length > 0 ? (
            <Panel title="승인 대기">
              {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
              <ul className="cv-rows divide-y divide-(--line)">
                {staged.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onAct={onAct} />
                ))}
              </ul>
            </Panel>
          ) : null}
          {applied.length > 0 ? (
            <Panel title="적용됨">
              {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
              <ul className="cv-rows divide-y divide-(--line)">
                {applied.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onAct={onAct} />
                ))}
              </ul>
            </Panel>
          ) : null}
          {discarded.length > 0 ? (
            <Panel title="기각됨">
              {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
              <ul className="cv-rows divide-y divide-(--line)">
                {discarded.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onAct={onAct} />
                ))}
              </ul>
            </Panel>
          ) : null}
          <Pager page={page} />
        </>
      )}
      <FormatsView refreshKey={refreshKey} />
      <ProfilesView refreshKey={refreshKey} />
    </div>
  );
}
