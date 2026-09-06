// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";
import { AskButton, Button, formatDate, Notice, Panel, Pill, Skeleton, useResource } from "web-shared";
import { fetchInsightPanel, refreshInsightPanel } from "@/lib/api";
import { ROLE_KO } from "@/lib/types";
import type { InsightItem, InsightPanelData } from "@/lib/types";

/** Joins up to 5 ids, appending "+n" for the rest. */
function capIds(ids: string[]): string {
  if (ids.length === 0) return "";
  const shown = ids.slice(0, 5).join(", ");
  return ids.length > 5 ? `${shown} +${ids.length - 5}` : shown;
}

function InsightRow({ item, onAskAssistant }: { item: InsightItem; onAskAssistant: (text: string) => void }) {
  const { candidate, narrative } = item;
  const title = narrative?.headline ?? candidate.label;
  const prompt = narrative?.prompt ?? `${candidate.label} 자세히 알려줘`;
  const figures = Object.entries(candidate.figures);
  return (
    <li className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{title}</div>
          {narrative?.why_it_matters ? <p className="mt-1 text-[12.5px] leading-relaxed text-(--ink-soft)">{narrative.why_it_matters}</p> : null}
        </div>
        <AskButton label="물어보기" onClick={() => onAskAssistant(prompt)} />
      </div>
      {figures.length > 0 ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {figures.map(([key, value]) => (
            <Pill key={key} tone="muted">
              {key} {value}
            </Pill>
          ))}
        </div>
      ) : null}
      {candidate.api_ids.length > 0 || candidate.ref_ids.length > 0 ? (
        <div className="mt-1.5 flex flex-col gap-0.5 font-mono text-[11.5px] text-(--ink-faint)">
          {candidate.api_ids.length > 0 ? <div className="truncate">api: {capIds(candidate.api_ids)}</div> : null}
          {candidate.ref_ids.length > 0 ? <div className="truncate">ref: {capIds(candidate.ref_ids)}</div> : null}
        </div>
      ) : null}
    </li>
  );
}

export default function InsightPanel({
  refreshKey,
  operatorId,
  onAskAssistant,
}: {
  refreshKey: number;
  operatorId: string;
  onAskAssistant: (text: string) => void;
}) {
  const { data: loaded, failed } = useResource(fetchInsightPanel, [refreshKey, operatorId]);
  // "새로 분석" replaces the local data without waiting for refreshKey/operatorId to change; a
  // change to either (a new load) drops the override so the next fetch's result shows instead.
  const [override, setOverride] = useState<InsightPanelData | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshError, setRefreshError] = useState(false);

  useEffect(() => {
    setOverride(null);
    setRefreshError(false);
  }, [refreshKey, operatorId]);

  const data = override ?? loaded;

  const onRefreshClick = async () => {
    setBusy(true);
    const next = await refreshInsightPanel();
    if (next) {
      setOverride(next);
      setRefreshError(false);
    } else {
      setRefreshError(true);
    }
    setBusy(false);
  };

  if (failed && !data) {
    return (
      <Panel title="AI 인사이트">
        <div className="px-[18px] py-3">
          <Notice>인사이트 패널을 불러올 수 없습니다 (비활성화되었거나 호스트 미접속).</Notice>
        </div>
      </Panel>
    );
  }
  if (!data) {
    return (
      <Panel title="AI 인사이트">
        <div className="p-[18px]">
          <Skeleton className="h-24" />
        </div>
      </Panel>
    );
  }

  const scope = data.scope_fallback ? "범위: 전체" : `내 범위 API ${data.scope_size}개`;
  const title = `AI 인사이트 — ${data.name} · ${ROLE_KO[data.role]} · ${scope}`;

  return (
    <Panel
      title={title}
      action={
        <>
          <Pill tone={data.generated_by === "agent" ? "accent" : "muted"}>
            {data.generated_by === "agent" ? "AI 작성 · 수치는 결정론" : "결정론"}
          </Pill>
          <span className="text-[12px] text-(--ink-soft)">{formatDate(data.generated_at)}</span>
          <Button size="sm" onClick={onRefreshClick} disabled={busy}>
            새로 분석
          </Button>
        </>
      }
    >
      {refreshError ? (
        <div className="px-[18px] pt-3">
          <Notice>새로 분석에 실패했습니다. 잠시 후 다시 시도하세요.</Notice>
        </div>
      ) : null}
      {data.items.length === 0 ? (
        <div className="px-[18px] py-3">
          <Notice>표시할 인사이트가 없습니다.</Notice>
        </div>
      ) : (
        <ul className="divide-y divide-(--line)">
          {data.items.map((item) => (
            <InsightRow key={item.candidate.candidate_id} item={item} onAskAssistant={onAskAssistant} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
