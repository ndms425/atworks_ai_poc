// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { Panel, StatStrip, StatTile, useResource } from "web-shared";
import { briefingUrl, fetchBriefing } from "@/lib/api";

export default function BriefingCard({ refreshKey, onAskAssistant }: { refreshKey: number; onAskAssistant: (text: string) => void }) {
  const { data, failed } = useResource(fetchBriefing, [refreshKey]);
  if (failed || !data) {
    return <p className="text-[13px] text-(--ink-soft)">오늘 브리핑은 09:00에 생성됩니다.</p>;
  }
  return (
    <Panel title={`일일 브리핑 ${data.date}`} subtitle={`${data.window.from.slice(0, 16)} → ${data.window.to.slice(0, 16)}`}>
      <StatStrip>
        <StatTile label="전체" value={String(data.counts.total)} onClick={() => onAskAssistant("어제 실행 결과 요약해줘")} ariaLabel="전체: 어시스턴트에게 물어보기" />
        <StatTile label="실패" value={String(data.counts.fail)} onClick={() => onAskAssistant("어제 실패 원인별로 묶어줘")} ariaLabel="실패: 어시스턴트에게 물어보기" />
        <StatTile label="에러" value={String(data.counts.error)} onClick={() => onAskAssistant("어제 에러난 API 가져와")} ariaLabel="에러: 어시스턴트에게 물어보기" />
        <StatTile label="불안정" value={String(data.insights.flaky)} onClick={() => onAskAssistant("요즘 왔다갔다 하는 API 뭐야")} ariaLabel="불안정: 어시스턴트에게 물어보기" />
      </StatStrip>
      <div className="px-[18px] pb-3 text-[13px]">
        <div className="text-(--ink-soft)">실패 원인 상위</div>
        <ul className="mt-1 list-disc pl-5">
          {data.top_groups.length === 0 ? <li>없음</li> : data.top_groups.map((g) => <li key={g.key} className="font-mono">{g.label} · {g.count}건</li>)}
        </ul>
        <div className="mt-2 text-(--ink-soft)">
          승인 대기 {data.jobs.pending.length}건{data.jobs.stale_pending ? ` · 24시간 초과 ${data.jobs.stale_pending}건` : ""} · 실행된 job {data.jobs.executed.length}건
        </div>
        <a className="mt-2 inline-block underline" href={briefingUrl(data.date)} target="_blank" rel="noreferrer">브리핑 열기</a>
      </div>
    </Panel>
  );
}
