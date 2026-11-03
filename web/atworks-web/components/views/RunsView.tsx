// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AskButton, formatDateTimeSeconds, Notice, PageHeader, Panel, Pill, plural, Segmented, Skeleton } from "web-shared";
import { fetchRuns } from "@/lib/api";
import RunDetail from "@/components/RunDetail";
import { attachToggle } from "@/lib/attachments";
import Pager from "@/components/Pager";
import { RUN_STATUS } from "@/lib/kinds";
import { methodTone } from "@/lib/method";
import { usePagedList } from "@/lib/usePagedList";
import { useScreenFocus } from "@/lib/useScreenFocus";
import type { AttachedItem, RunResult, ScreenFilter, ScreenIntent, ScreenTarget } from "@/lib/types";

type Filter = "all" | "fail" | "error" | "pass";

export default function RunsView({
  refreshKey,
  attachedCount,
  onAttach,
  attachedRefs,
  intent,
  onScreen,
}: {
  refreshKey: number;
  attachedCount: number;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  // 펼쳐 둔 실행. APIs 와 같은 이유로 목록을 남긴다 -- `screen_state.visible` 과
  // `navigate_screen` 의 focus 계약을 건드리지 않는다.
  const [openId, setOpenId] = useState<string | null>(null);
  // The status segment IS the reset key: switching it restarts at page one, while a refreshKey
  // bump re-reads whichever page is open.
  const load = useCallback((cursor: string | null) => fetchRuns(filter === "all" ? undefined : filter, cursor), [filter]);
  const page = usePagedList<RunResult>(load, filter, [refreshKey]);
  const runs = page.items;

  // The label rides the run record itself (host Task 10: the read joins the API mirror), so this
  // view no longer downloads 500 API specs on mount just to turn an api_id into "GET /v1/orders".
  const labelFor = (run: RunResult): string => (run.api_method && run.api_path ? `${run.api_method} ${run.api_path}` : run.api_id);

  // Apply a navigate directive's filter once per nonce (a re-render must never re-apply it).
  // Scroll-to-focus is handled by the shared hook below, independent of this filter application.
  const appliedFilterNonceRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!intent || intent.nonce === appliedFilterNonceRef.current) return;
    appliedFilterNonceRef.current = intent.nonce;
    // Setting the status here also resets the cursor stack (filter is usePagedList's resetKey) —
    // "show me the failures" means the FIRST page of failures.
    if (intent.filter?.status) setFilter(intent.filter.status);
  }, [intent]);

  // 다른 집합으로 바뀐 뒤에도 남아 있으면 화면에 없는 행의 상세가 혼자 떠 있게 된다.
  useEffect(() => setOpenId(null), [filter]);

  useScreenFocus(intent, "run", runs.length > 0);

  // Report what's actually on screen so api.screenState stays current for the next chat turn:
  // the CURRENT PAGE (sliced to 40), which is exactly what the operator can see and point at.
  useEffect(() => {
    onScreen?.({
      filter: { status: filter },
      visible: runs.slice(0, 40).map((run) => ({ kind: "run", ref_id: run.run_id, label: `${labelFor(run)} ${run.target_env}`, status: run.status })),
    });
  }, [runs, filter, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader
        title="Runs"
        subtitle={page.loaded ? `${plural(page.total, "run")}${attachedCount ? ` · 첨부 ${attachedCount}건` : ""}` : undefined}
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

      {page.failed && !page.loaded ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so runs can&apos;t load.</Notice>
      ) : !page.loaded ? (
        <Skeleton className="h-96" />
      ) : runs.length === 0 ? (
        <Notice>해당하는 실행 기록이 없습니다.</Notice>
      ) : (
        <div className={openId ? "grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,460px)] lg:items-start" : ""}>
        <div className={`flex flex-col gap-4 ${openId ? "hidden lg:flex" : ""}`}>
          <Panel bodyClassName="panel-scroll overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="text-left text-[12px] font-semibold text-(--ink-soft)">
                  <th className="py-2.5 pl-[18px] pr-3 font-semibold">API</th>
                  <th className="px-3 py-2.5 font-semibold">Executed</th>
                  <th className="px-3 py-2.5 font-semibold">Env</th>
                  {/* 데이터(test_data_label) 열을 뺐다: 목록에서 늘 비어 있고, 채워지더라도
                      그 실행 하나를 열었을 때 볼 값이지 132개 행에 매번 비울 열이 아니다.
                      그 자리에 HTTP 와 소요 시간을 둔다 -- 둘 다 실행마다 다르고, `fail` 301건이
                      실은 400·404·409 가 섞인 것임을 여기서만 구별할 수 있다. */}
                  <th className="px-3 py-2.5 font-semibold">HTTP</th>
                  <th className="px-3 py-2.5 text-right font-semibold">소요</th>
                  <th className="px-3 py-2.5 font-semibold">Status</th>
                  <th className="w-[56px] py-2.5 pl-3 pr-[18px]" />
                </tr>
              </thead>
              {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
              <tbody className="cv-rows">
                {runs.map((run) => {
                  const style = RUN_STATUS[run.status];
                  return (
                    <tr
                      key={run.run_id}
                      data-ref={`run:${run.run_id}`}
                      // APIs 목록과 같은 규칙: 행 전체가 상세를 여는 버튼이고, 다시 누르면 닫힌다.
                      tabIndex={0}
                      role="button"
                      aria-expanded={openId === run.run_id}
                      aria-label={`${labelFor(run)} 상세 열기`}
                      onClick={() => setOpenId((current) => (current === run.run_id ? null : run.run_id))}
                      onKeyDown={(event) => {
                        if (event.key !== "Enter" && event.key !== " ") return;
                        event.preventDefault();
                        setOpenId((current) => (current === run.run_id ? null : run.run_id));
                      }}
                      className={`cursor-pointer border-t border-(--line) transition hover:bg-(--well) focus-visible:bg-(--well) focus-visible:outline-none ${
                        openId === run.run_id ? "bg-(--accent-soft)" : ""
                      }`}
                    >
                      <td className="py-2 pl-[18px] pr-3">
                        <div className="font-mono text-[12.5px] text-(--ink)">
                          {run.api_method && run.api_path ? (
                            <>
                              <span className={`font-semibold ${methodTone(run.api_method)}`}>{run.api_method}</span> {run.api_path}
                            </>
                          ) : (
                            labelFor(run)
                          )}
                        </div>
                        <div className="text-[11.5px] tabular-nums text-(--ink-soft)">
                          {run.run_id}
                          {run.failed_rules?.length ? <span> · {run.failed_rules.join(", ")}</span> : null}
                        </div>
                      </td>
                      {/* 초까지. 같은 분 안에 수십 건이 쌓이는 목록이라 분 단위로는 순서도
                          간격도 보이지 않는다. */}
                      <td className="px-3 py-2 text-[12.5px] tabular-nums text-(--ink-soft)">{formatDateTimeSeconds(run.executed_at)}</td>
                      <td className="px-3 py-2 text-[12.5px] text-(--ink-soft)">{run.target_env}</td>
                      <td className="px-3 py-2 text-[12.5px] tabular-nums text-(--ink-soft)">{run.http_status ?? "—"}</td>
                      <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-(--ink-soft)">
                        {run.duration_ms != null ? `${run.duration_ms}ms` : "—"}
                      </td>
                      <td className="px-3 py-2">
                        <Pill tone={style.tone} dot>
                          {style.label}
                        </Pill>
                      </td>
                      <td className="w-[56px] py-2 pl-3 pr-[18px] text-right">
                        <AskButton
                          {...attachToggle(
                            attachedRefs,
                            {
                              kind: "run",
                              ref_id: run.run_id,
                              label: labelFor(run),
                              field: run.failed_rules?.[0]?.split(/\s/)[0],
                              expected: run.failed_rules?.[0],
                            },
                            onAttach,
                          )}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>
          <Pager page={page} />
        </div>
        {openId ? (() => {
          const open = runs.find((run) => run.run_id === openId);
          return open ? (
            <RunDetail run={open} onAttach={onAttach} attachedRefs={attachedRefs} onClose={() => setOpenId(null)} />
          ) : null;
        })() : null}
        </div>
      )}
    </div>
  );
}
