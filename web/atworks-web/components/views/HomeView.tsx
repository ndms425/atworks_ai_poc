// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect } from "react";
import { Button, Notice, PageHeader, Panel, plural, Skeleton, StatStrip, StatTile, useResource } from "web-shared";
import { fetchHomeSummary } from "@/lib/api";
import BriefingCard from "@/components/BriefingCard";
import InsightPanel from "@/components/InsightPanel";
import type { ScreenFilter, ScreenTarget } from "@/lib/types";

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
  // ONE call (Task 10). This used to be four parallel reads whose counts came from the LISTS they
  // returned: /runs?status=fail and /runs?status=error each downloaded a run page to read its
  // population, and the pending tile counted staged jobs in a full /jobs download. Every number
  // below is now a count query the host ran.
  const { data, failed } = useResource(fetchHomeSummary, [refreshKey]);
  const total = data ? data.counts.fail + data.counts.error + data.counts.pending_jobs + data.insights.flaky : 0;

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
        <Panel title="Needs attention" subtitle={plural(total, "item")}>
          <StatStrip>
            <StatTile
              label="실패"
              value={String(data.counts.fail)}
              onClick={() => onAskAssistant("최근 실패한 api 중 risk 있는 것 가져와")}
              ariaLabel="실패: 어시스턴트에게 물어보기"
            />
            <StatTile
              label="에러"
              value={String(data.counts.error)}
              onClick={() => onAskAssistant("최근 에러난 api 가져와")}
              ariaLabel="에러: 어시스턴트에게 물어보기"
            />
            <StatTile
              label="승인 대기"
              value={String(data.counts.pending_jobs)}
              onClick={() => onAskAssistant("승인 대기 중인 job 보여줘")}
              ariaLabel="승인 대기: 어시스턴트에게 물어보기"
            />
            <StatTile
              label="불안정"
              value={String(data.insights.flaky)}
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
