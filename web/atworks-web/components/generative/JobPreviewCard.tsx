// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import type { ReactNode } from "react";
import { ApproveBar, type ChangeAction, ChangeStatusPill, formatDate, GenCard, GenCardHeader, GuardrailNotes, useChangeActions } from "web-shared";
import { reportUrl } from "@/lib/api";
import type { JobPreviewPayload, JobSchedule, JobSpec } from "@/lib/types";

const SLOT_LABEL: Record<string, string> = {
  target_envs: "대상 계",
  schedules: "스케줄",
  test_data: "테스트 데이터",
  binding: "선택 고정",
  api_ids: "대상 API",
  runs_total: "총 실행",
  report: "리포트",
};

function scheduleLine(s: JobSchedule): string {
  return s.kind === "daily"
    ? `${s.from_date}부터 매일 ${s.at} × ${s.count}회 (${s.done}/${s.count})`
    : `${s.from_date} ${s.at} 1회 (${s.done}/${s.count})`;
}

export default function JobPreviewCard({ payload, onAct }: { payload: JobPreviewPayload; onAct?: (id: string, action: ChangeAction) => Promise<JobSpec | null> }) {
  // web-shared의 applyChangeUpdate는 후속 change_update를 payload.change에 얹는다 — 있으면 그것이 최신.
  const { change: job, busy, error, act, canAct } = useChangeActions(payload.change ?? payload.job, onAct);
  const low = new Set(payload.low_confidence);
  const m = payload.matrix;
  // LATE는 실행마다 select_where를 재평가하므로 스테이징 시점의 api_ids 개수는 상한일 뿐이다.
  const ceiling = job.binding === "LATE" ? "최대 " : "";
  const rows: Array<[string, ReactNode]> = [
    [
      "target_envs",
      <ul key="envs">
        {job.target_envs.map((env) => (
          <li key={env}>{env}</li>
        ))}
      </ul>,
    ],
    ["api_ids", `${job.api_ids.length}개 (${payload.apis.slice(0, 3).map((a) => a.path).join(", ")}${payload.apis.length > 3 ? " …" : ""})`],
    ["binding", job.binding === "FROZEN" ? "오늘 고른 목록 고정" : "실행 때마다 재선택"],
    [
      "schedules",
      job.schedules.length === 0 ? (
        "즉시 1회"
      ) : (
        <ul key="schedules">
          {job.schedules.map((s) => (
            <li key={`${s.kind}-${s.tz}-${s.from_date}-${s.at}`}>{scheduleLine(s)}</li>
          ))}
        </ul>
      ),
    ],
    [
      "test_data",
      job.test_data.length === 0 ? (
        "바인딩 없음"
      ) : (
        <ul key="test_data">
          {job.test_data.map((d) => (
            <li key={d.label}>
              <details>
                <summary className="cursor-pointer">{d.label}</summary>
                <span className="text-[12px] text-(--ink-soft)">
                  {Object.entries(d.values)
                    .map(([k, v]) => `${k}=${v}`)
                    .join(", ")}
                </span>
              </details>
            </li>
          ))}
        </ul>
      ),
    ],
    ["runs_total", `${ceiling}${m.runs_per_execution}건 × ${m.executions}회 = ${ceiling}${m.runs_total}건`],
    ["report", job.report ? "남김" : "안 남김"],
  ];
  return (
    <GenCard>
      <GenCardHeader
        title={payload.headline ?? job.summary}
        meta={
          <>
            <ChangeStatusPill status={job.status} />
            <span>{job.kind}</span>
            <span aria-hidden>·</span>
            <span>{formatDate(job.created_at)}</span>
          </>
        }
      />
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] text-(--ink-soft)">{payload.note}</p> : null}
      <table className="mx-3.5 my-2 w-[calc(100%-28px)] text-[13px]">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k} className={low.has(k) ? "bg-(--warn-soft)" : ""}>
              <td className="py-1 pr-3 align-top text-(--ink-soft)">
                {SLOT_LABEL[k] ?? k}
                {low.has(k) ? (
                  <span className="ml-1 text-(--warn)" title="확신 낮음 — 확인 필요">
                    ●
                  </span>
                ) : null}
              </td>
              <td className="py-1 font-medium">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {job.assumptions?.length ? (
        <ul className="mx-3.5 mb-2 list-disc pl-4 text-[12px] text-(--ink-soft)">
          {job.assumptions.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      ) : null}
      <GuardrailNotes notes={job.guardrail_notes} />
      {job.status === "applied" && job.run_ids?.length ? (
        <a className="mx-3.5 mb-2 inline-block text-[12.5px] underline" href={reportUrl(job.job_id)} target="_blank" rel="noreferrer">
          리포트 열기
        </a>
      ) : null}
      <ApproveBar change={job} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </GenCard>
  );
}
