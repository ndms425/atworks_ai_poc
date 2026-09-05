// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AskButton, formatDate, Notice, PageHeader, Panel, Pill, plural, Segmented, Skeleton, useResource } from "web-shared";
import { fetchApis, fetchRuns } from "@/lib/api";
import { RUN_STATUS } from "@/lib/kinds";
import { useScreenFocus } from "@/lib/useScreenFocus";
import type { ApiSpec, AttachedItem, ScreenFilter, ScreenIntent, ScreenTarget } from "@/lib/types";

type Filter = "all" | "fail" | "error" | "pass";

export default function RunsView({
  refreshKey,
  attachedCount,
  onAttach,
  intent,
  onScreen,
}: {
  refreshKey: number;
  attachedCount: number;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const { data: apiData } = useResource(() => fetchApis(), [refreshKey]);
  const { data, failed } = useResource(() => fetchRuns(filter === "all" ? undefined : filter), [refreshKey, filter]);
  const apiById = useMemo(() => new Map((apiData?.apis ?? []).map((a) => [a.api_id, a])), [apiData]);
  const runs = data?.runs ?? [];

  const labelFor = (apiId: string): string => {
    const api: ApiSpec | undefined = apiById.get(apiId);
    return api ? `${api.method} ${api.path}` : apiId;
  };

  // Apply a navigate directive's filter once per nonce (a re-render must never re-apply it).
  // Scroll-to-focus is handled by the shared hook below, independent of this filter application.
  const appliedFilterNonceRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!intent || intent.nonce === appliedFilterNonceRef.current) return;
    appliedFilterNonceRef.current = intent.nonce;
    if (intent.filter?.status) setFilter(intent.filter.status);
  }, [intent]);

  useScreenFocus(intent, "run", runs.length > 0);

  // Report what's actually on screen so api.screenState stays current for the next chat turn.
  useEffect(() => {
    onScreen?.({
      filter: { status: filter },
      visible: runs.slice(0, 40).map((run) => ({ kind: "run", ref_id: run.run_id, label: `${labelFor(run.api_id)} ${run.target_env}` })),
    });
  }, [runs, filter, apiById, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader
        title="Runs"
        subtitle={data ? `${plural(data.population, "run")}${attachedCount ? ` · 첨부 ${attachedCount}건` : ""}` : undefined}
      >
        <Segmented<Filter>
          label="Filter runs"
          value={filter}
          onChange={setFilter}
          options={[
            { id: "all", label: "전체" },
            { id: "fail", label: "실패" },
            { id: "error", label: "에러" },
            { id: "pass", label: "성공" },
          ]}
        />
      </PageHeader>

      {failed && !data ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so runs can&apos;t load.</Notice>
      ) : !data ? (
        <Skeleton className="h-96" />
      ) : runs.length === 0 ? (
        <Notice>해당하는 실행 기록이 없습니다.</Notice>
      ) : (
        <Panel bodyClassName="panel-scroll overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="text-left text-[12px] font-semibold text-(--ink-soft)">
                <th className="py-2.5 pl-[18px] pr-3 font-semibold">API</th>
                <th className="px-3 py-2.5 font-semibold">Executed</th>
                <th className="px-3 py-2.5 font-semibold">Env</th>
                <th className="px-3 py-2.5 font-semibold">데이터</th>
                <th className="px-3 py-2.5 font-semibold">Status</th>
                <th className="py-2.5 pl-3 pr-[18px]" />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const style = RUN_STATUS[run.status];
                return (
                  <tr key={run.run_id} data-ref={`run:${run.run_id}`} className="border-t border-(--line)">
                    <td className="py-2 pl-[18px] pr-3">
                      <div className="font-mono text-[12.5px] text-(--ink)">{labelFor(run.api_id)}</div>
                      <div className="text-[11.5px] tabular-nums text-(--ink-soft)">
                        {run.run_id}
                        {run.failed_rules?.length ? <span> · {run.failed_rules.join(", ")}</span> : null}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-[12.5px] tabular-nums text-(--ink-soft)">{formatDate(run.executed_at)}</td>
                    <td className="px-3 py-2 text-[12.5px] text-(--ink-soft)">{run.target_env}</td>
                    <td className="px-3 py-2 text-[12.5px] text-(--ink-soft)">{run.test_data_label ?? "—"}</td>
                    <td className="px-3 py-2">
                      <Pill tone={style.tone} dot>
                        {style.label}
                      </Pill>
                    </td>
                    <td className="py-2 pl-3 pr-[18px]">
                      <AskButton
                        label="채팅에 첨부"
                        onClick={() =>
                          onAttach({
                            kind: "run",
                            ref_id: run.run_id,
                            label: labelFor(run.api_id),
                            field: run.failed_rules?.[0]?.split(/\s/)[0],
                            expected: run.failed_rules?.[0],
                          })
                        }
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}
