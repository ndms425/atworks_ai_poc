// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useState } from "react";
import { AskButton, formatDate, Notice, PageHeader, Panel, Pill, plural, SearchField, Skeleton, useResource } from "web-shared";
import { fetchApis } from "@/lib/api";

export default function ApisView({ refreshKey, onAskAssistant }: { refreshKey: number; onAskAssistant: (text: string) => void }) {
  const [query, setQuery] = useState("");
  const { data, failed } = useResource(() => fetchApis(query), [refreshKey, query]);
  const apis = data?.apis ?? [];

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader title="APIs" subtitle={data ? plural(apis.length, "api") : undefined} />
      <SearchField value={query} onChange={setQuery} placeholder="이름 또는 경로로 검색" label="Search APIs" className="max-w-sm" />

      {failed && !data ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so APIs can&apos;t load.</Notice>
      ) : !data ? (
        <Skeleton className="h-96" />
      ) : apis.length === 0 ? (
        <Notice>일치하는 API가 없습니다.</Notice>
      ) : (
        <Panel bodyClassName="panel-scroll overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="text-left text-[12px] font-semibold text-(--ink-soft)">
                <th className="py-2.5 pl-[18px] pr-3 font-semibold">Method</th>
                <th className="px-3 py-2.5 font-semibold">Path</th>
                <th className="px-3 py-2.5 font-semibold">Name</th>
                <th className="px-3 py-2.5 font-semibold">Updated</th>
                <th className="px-3 py-2.5 font-semibold">Rules</th>
                <th className="py-2.5 pl-3 pr-[18px]" />
              </tr>
            </thead>
            <tbody>
              {apis.map((api) => (
                <tr key={api.api_id} className="border-t border-(--line)">
                  <td className="py-2 pl-[18px] pr-3 text-[12.5px] font-semibold tabular-nums text-(--ink-soft)">{api.method}</td>
                  <td className="px-3 py-2 font-mono text-[12.5px] text-(--ink)">{api.path}</td>
                  <td className="px-3 py-2 text-[13px] text-(--ink)">{api.name}</td>
                  <td className="px-3 py-2 text-[12.5px] tabular-nums text-(--ink-soft)">{formatDate(api.updated_at)}</td>
                  <td className="px-3 py-2">{api.has_rules ? <Pill tone="info">규칙 있음</Pill> : null}</td>
                  <td className="py-2 pl-3 pr-[18px]">
                    <AskButton label="지금 실행 물어보기" onClick={() => onAskAssistant(`${api.path} 지금 실행해줘`)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}
