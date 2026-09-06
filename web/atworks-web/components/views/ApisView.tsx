// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AskButton, formatDate, Notice, PageHeader, Panel, Pill, plural, SearchField, Skeleton } from "web-shared";
import { fetchApis } from "@/lib/api";
import Pager from "@/components/Pager";
import { usePagedList } from "@/lib/usePagedList";
import { useScreenFocus } from "@/lib/useScreenFocus";
import type { ApiSpec, AttachedItem, ScreenFilter, ScreenIntent, ScreenTarget } from "@/lib/types";

export default function ApisView({
  refreshKey,
  onAskAssistant,
  onAttach,
  intent,
  onScreen,
}: {
  refreshKey: number;
  onAskAssistant: (text: string) => void;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const [query, setQuery] = useState("");
  // The search string IS the reset key: a new query restarts at page one, while a refreshKey bump
  // re-reads the page currently open.
  const load = useCallback((cursor: string | null) => fetchApis(query, cursor), [query]);
  const page = usePagedList<ApiSpec>(load, query, [refreshKey]);
  const apis = page.items;

  // Apply a navigate directive's filter once per nonce (a re-render must never re-apply it).
  // Setting the query here also resets the cursor stack (query is usePagedList's resetKey), which
  // is what a directive means by "filter to X": show me the FIRST page of X.
  // Scroll-to-focus is handled by the shared hook below, independent of this filter application.
  const appliedFilterNonceRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!intent || intent.nonce === appliedFilterNonceRef.current) return;
    appliedFilterNonceRef.current = intent.nonce;
    if (intent.filter?.query != null) setQuery(intent.filter.query);
  }, [intent]);

  useScreenFocus(intent, "api", apis.length > 0);

  // Report what's actually on screen so api.screenState stays current for the next chat turn:
  // the CURRENT PAGE (sliced to 40), which is exactly what the operator can see and point at.
  useEffect(() => {
    onScreen?.({
      filter: { query },
      visible: apis.slice(0, 40).map((api) => ({ kind: "api", ref_id: api.api_id, label: `${api.method} ${api.path}` })),
    });
  }, [apis, query, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      {/* The subtitle is the envelope's `total` — the whole filtered set, not the rows on screen. */}
      <PageHeader title="APIs" subtitle={page.loaded ? plural(page.total, "api") : undefined} />
      <SearchField value={query} onChange={setQuery} placeholder="이름 또는 경로로 검색" label="Search APIs" className="max-w-sm" />

      {page.failed && !page.loaded ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so APIs can&apos;t load.</Notice>
      ) : !page.loaded ? (
        <Skeleton className="h-96" />
      ) : apis.length === 0 ? (
        <Notice>일치하는 API가 없습니다.</Notice>
      ) : (
        <>
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
              {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
              <tbody className="cv-rows">
                {apis.map((api) => (
                  <tr key={api.api_id} data-ref={`api:${api.api_id}`} className="border-t border-(--line)">
                    <td className="py-2 pl-[18px] pr-3 text-[12.5px] font-semibold tabular-nums text-(--ink-soft)">{api.method}</td>
                    <td className="px-3 py-2 font-mono text-[12.5px] text-(--ink)">{api.path}</td>
                    <td className="px-3 py-2 text-[13px] text-(--ink)">{api.name}</td>
                    <td className="px-3 py-2 text-[12.5px] tabular-nums text-(--ink-soft)">{formatDate(api.updated_at)}</td>
                    <td className="px-3 py-2">{api.has_rules ? <Pill tone="info">규칙 있음</Pill> : null}</td>
                    <td className="py-2 pl-3 pr-[18px] flex items-center gap-2">
                      <AskButton label="지금 실행 물어보기" onClick={() => onAskAssistant(`${api.path} 지금 실행해줘`)} />
                      <AskButton
                        label="채팅에 첨부"
                        onClick={() => onAttach({ kind: "api", ref_id: api.api_id, label: `${api.method} ${api.path}` })}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
          <Pager page={page} />
        </>
      )}
    </div>
  );
}
