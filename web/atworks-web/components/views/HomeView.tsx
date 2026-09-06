// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect } from "react";
import { Button, Notice, PageHeader, Panel, plural, Skeleton, StatStrip, StatTile, useResource } from "web-shared";
import { fetchInsights, fetchJobs, fetchRuns } from "@/lib/api";
import BriefingCard from "@/components/BriefingCard";
import InsightPanel from "@/components/InsightPanel";
import type { ScreenFilter, ScreenTarget } from "@/lib/types";

interface HomeCounts {
  fail: number;
  error: number;
  pending: number;
  flaky: number;
}

async function loadCounts(): Promise<HomeCounts | null> {
  const [failRes, errorRes, jobsRes, insightsRes] = await Promise.all([fetchRuns("fail"), fetchRuns("error"), fetchJobs(), fetchInsights()]);
  if (!failRes || !errorRes || !jobsRes) return null;
  return {
    fail: failRes.population,
    error: errorRes.population,
    pending: jobsRes.jobs.filter((job) => job.status === "staged").length,
    flaky: insightsRes?.flaky ?? 0,
  };
}

export default function HomeView({
  refreshKey,
  operatorId,
  onAskAssistant,
  onScreen,
}: {
  refreshKey: number;
  operatorId: string;
  onAskAssistant: (text: string) => void;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const { data, failed } = useResource(loadCounts, [refreshKey]);

  // Home has no list of its own to report — just keep api.screenState's view current.
  useEffect(() => {
    onScreen?.({ visible: [] });
  }, [onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-5">
      <PageHeader title="Home" subtitle="오늘 봐야 할 것" />

      <InsightPanel refreshKey={refreshKey} operatorId={operatorId} onAskAssistant={onAskAssistant} />

      <BriefingCard refreshKey={refreshKey} onAskAssistant={onAskAssistant} />

      {failed && !data ? (
        <Notice>
          The aTworks AI host on port 8010 isn&apos;t reachable. Start it with{" "}
          <code className="rounded bg-(--well) px-1 font-mono text-[13px]">python -m atworks_host.main</code> and reload.
        </Notice>
      ) : !data ? (
        <Skeleton className="h-36" />
      ) : (
        <Panel title="Needs attention" subtitle={plural(data.fail + data.error + data.pending + data.flaky, "item")}>
          <StatStrip>
            <StatTile
              label="실패"
              value={String(data.fail)}
              onClick={() => onAskAssistant("최근 실패한 api 중 risk 있는 것 가져와")}
              ariaLabel="실패: 어시스턴트에게 물어보기"
            />
            <StatTile
              label="에러"
              value={String(data.error)}
              onClick={() => onAskAssistant("최근 에러난 api 가져와")}
              ariaLabel="에러: 어시스턴트에게 물어보기"
            />
            <StatTile
              label="승인 대기"
              value={String(data.pending)}
              onClick={() => onAskAssistant("승인 대기 중인 job 보여줘")}
              ariaLabel="승인 대기: 어시스턴트에게 물어보기"
            />
            <StatTile
              label="불안정"
              value={String(data.flaky)}
              onClick={() => onAskAssistant("요즘 왔다갔다 하는 API 뭐야")}
              ariaLabel="불안정: 어시스턴트에게 물어보기"
            />
          </StatStrip>
        </Panel>
      )}

      <Button variant="primary" icon="spark" className="self-start" onClick={() => onAskAssistant("최근 실패한 api 중 risk 있는 것 가져와")}>
        먼저 볼 것 물어보기
      </Button>
    </div>
  );
}
