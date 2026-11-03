// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AskButton, formatDate, Notice, PageHeader, Panel, Pill, plural, SearchField, Skeleton } from "web-shared";
import ApiDetail from "@/components/ApiDetail";
import { fetchApis } from "@/lib/api";
import { attachToggle } from "@/lib/attachments";
import Pager from "@/components/Pager";
import { methodTone } from "@/lib/method";
import { usePagedList } from "@/lib/usePagedList";
import { useScreenFocus } from "@/lib/useScreenFocus";
import type { ApiSpec, AttachedItem, ScreenFilter, ScreenIntent, ScreenTarget } from "@/lib/types";

export default function ApisView({
  refreshKey,
  onAttach,
  attachedRefs,
  intent,
  onScreen,
}: {
  refreshKey: number;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const [query, setQuery] = useState("");
  // 펼쳐 둔 API. 화면 지시어 계약은 건드리지 않는다 -- 목록이 그대로 남으므로
  // `screen_state.visible`은 여전히 보이는 행들이고, `navigate_screen`의 focus도 그대로 쓴다.
  const [openId, setOpenId] = useState<string | null>(null);
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

  // 검색어가 바뀌면 열어 둔 상세를 닫는다. 목록이 다른 집합으로 바뀐 뒤에도 남아 있으면
  // 화면에 없는 행의 상세가 혼자 떠 있게 된다.
  useEffect(() => setOpenId(null), [query]);

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
        // 넓은 화면은 목록 + 상세 2단, 좁은 화면은 상세가 목록을 덮는다(상세에 닫기 버튼).
        // 상세를 통째로 다른 화면으로 만들지 않은 이유: `PortalViewId`에 값을 더하면
        // `screen_state.view`·`navigate_screen`·강조 오버레이가 전부 그 값을 알아야 한다.
        // 목록을 남기면 그 계약을 건드리지 않고, 선택은 기존 focus로 그대로 표현된다.
        <div className={openId ? "grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)] lg:items-start" : ""}>
        <div className={`flex flex-col gap-4 ${openId ? "hidden lg:flex" : ""}`}>
          <Panel bodyClassName="panel-scroll overflow-x-auto">
            {/* 레일에서는 table-fixed. 자동 폭이면 표의 자연 너비가 260px 를 넘겨 경로가
                패널 밖으로 흘러 나간다(가로 스크롤 + 잘린 글자). 폭을 먼저 정하고 넘치는
                글자를 말줄임으로 접는 쪽이 고르는 데 필요한 정보를 더 많이 남긴다. */}
            <table className={`w-full border-collapse ${openId ? "table-fixed" : ""}`}>
              <thead>
                <tr className="text-left text-[12px] font-semibold text-(--ink-soft)">
                  <th className={`py-2.5 pl-[18px] pr-3 font-semibold ${openId ? "w-[72px]" : ""}`}>Method</th>
                  <th className={`px-3 py-2.5 font-semibold ${openId ? "pr-[18px]" : ""}`}>Path</th>
                  {/* 상세를 열면 목록은 **고르는 자리**가 된다. 이름·수정일·규칙·동작은 전부
                      오른쪽 상세에 이미 있고, 여기 남겨 두면 좁아진 열에 765px짜리 표를
                      욱여넣어 가로 스크롤이 생긴다 — 고르기도 읽기도 어려워진다. */}
                  {openId ? null : (
                    <>
                      <th className="px-3 py-2.5 font-semibold">Name</th>
                      <th className="px-3 py-2.5 font-semibold">Updated</th>
                      <th className="px-3 py-2.5 font-semibold">Rules</th>
                      <th className="w-[56px] py-2.5 pl-3 pr-[18px]" />
                    </>
                  )}
                </tr>
              </thead>
              {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
              <tbody className="cv-rows">
                {apis.map((api) => (
                  <tr
                    key={api.api_id}
                    data-ref={`api:${api.api_id}`}
                    // 행 전체가 상세를 여는 버튼이다. 마우스만 쓰는 사람에게는 넓은 과녁이,
                    // 키보드만 쓰는 사람에게는 Tab으로 닿는 자리가 필요해서 둘 다 연다.
                    tabIndex={0}
                    role="button"
                    aria-expanded={openId === api.api_id}
                    aria-label={`${api.method} ${api.path} 상세 열기`}
                    // 열려 있는 행을 다시 누르면 닫힌다 -- 여는 동작과 같은 자리에서 끄는 것이
                    // 가장 찾기 쉽다. 상세의 × 버튼은 그 반대편에서 같은 일을 한다.
                    onClick={() => setOpenId((current) => (current === api.api_id ? null : api.api_id))}
                    onKeyDown={(event) => {
                      if (event.key !== "Enter" && event.key !== " ") return;
                      event.preventDefault();
                      setOpenId((current) => (current === api.api_id ? null : api.api_id));
                    }}
                    className={`cursor-pointer border-t border-(--line) transition hover:bg-(--well) focus-visible:bg-(--well) focus-visible:outline-none ${
                      openId === api.api_id ? "bg-(--accent-soft)" : ""
                    }`}
                  >
                    <td className={`py-2 pl-[18px] pr-3 text-[12.5px] font-semibold tabular-nums ${methodTone(api.method)}`}>{api.method}</td>
                    <td className={`px-3 py-2 font-mono text-[12.5px] text-(--ink) ${openId ? "pr-[18px]" : ""}`}>
                      <span className="block truncate">{api.path}</span>
                      {/* 고르기 모드에서는 이름이 경로 아래로 내려간다 — 열이 사라져도 무엇을
                          고르는지는 보여야 한다. 단, 이름이 "METHOD /path" 그대로면(이 배포의
                          이름은 경로에서 만들어진다) 같은 글자를 한 줄 더 쌓지 않는다. */}
                      {openId && api.name && api.name !== api.path && api.name !== `${api.method} ${api.path}` ? (
                        <span className="block truncate text-[11.5px] text-(--ink-soft)">{api.name}</span>
                      ) : null}
                    </td>
                    {openId ? null : (
                      <>
                        <td className="px-3 py-2 text-[13px] text-(--ink)">{api.name}</td>
                        <td className="px-3 py-2 text-[12.5px] tabular-nums text-(--ink-soft)">{formatDate(api.updated_at)}</td>
                        <td className="px-3 py-2">{api.has_rules ? <Pill tone="info">규칙 있음</Pill> : null}</td>
                        {/* 이 칸의 클릭은 행 선택이 아니다 -- 첨부 버튼을 누른 것이 상세를
                            여는 일까지 겸하면, 버튼 하나에 두 가지 일이 붙는다. */}
                        <td
                          className="w-[56px] py-2 pl-3 pr-[18px] text-right"
                          onClick={(event) => event.stopPropagation()}
                          onKeyDown={(event) => event.stopPropagation()}
                        >
                          <AskButton
                            {...attachToggle(attachedRefs, { kind: "api", ref_id: api.api_id, label: `${api.method} ${api.path}` }, onAttach)}
                          />
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
          <Pager page={page} />
        </div>
        {openId ? (
          <ApiDetail
            apiId={openId}
            onAttach={onAttach}
            attachedRefs={attachedRefs}
            onClose={() => setOpenId(null)}
          />
        ) : null}
        </div>
      )}
    </div>
  );
}
