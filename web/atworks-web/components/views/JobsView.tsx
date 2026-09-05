// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef } from "react";
import {
  ApproveBar,
  AskButton,
  type ChangeAction,
  ChangeStatusPill,
  formatDate,
  GuardrailNotes,
  Notice,
  PageHeader,
  Panel,
  plural,
  Skeleton,
  useChangeActions,
  useResource,
} from "web-shared";
import { fetchJobs, reportUrl } from "@/lib/api";
import type { AttachedItem, JobSpec, ScreenFilter, ScreenIntent, ScreenTarget } from "@/lib/types";

function JobRow({
  job,
  onAct,
  onAttach,
}: {
  job: JobSpec;
  onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
}) {
  const { change, busy, error, act, canAct } = useChangeActions(job, onAct);
  return (
    <li data-ref={`job:${change.job_id}`} className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{change.summary}</div>
          <div className="mt-0.5 text-[12px] tabular-nums text-(--ink-soft)">
            {/* 실행 횟수(executions/total_executions)는 두 binding 모두 정확한 값이라 "최대"를
                붙이지 않는다 — 불확실한 쪽은 실행당 건수이고, LATE는 그 상한을 배포 설정이 정한다.
                job_record만으로는 그 숫자(config.max_matrix_size)를 알 수 없으니 문구로만 알린다. */}
            {change.kind} · {change.target_envs.join(", ")} · {change.executions}/{change.total_executions}회
            {change.binding === "LATE" ? " · 최대 (배포 상한)" : ""} · {formatDate(change.created_at)}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <AskButton label="채팅에 첨부" onClick={() => onAttach({ kind: "job", ref_id: change.job_id, label: change.summary })} />
          <ChangeStatusPill status={change.status} />
        </div>
      </div>
      <GuardrailNotes notes={change.guardrail_notes} />
      {change.selection_basis ? <div className="mt-0.5 text-[12px] text-(--ink-soft)">선택 근거: {change.selection_basis}</div> : null}
      {change.status === "applied" && change.run_ids?.length ? (
        <a className="mt-1.5 inline-block text-[12.5px] underline" href={reportUrl(change.job_id)} target="_blank" rel="noreferrer">
          리포트 열기
        </a>
      ) : null}
      <ApproveBar change={change} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </li>
  );
}

export default function JobsView({
  refreshKey,
  onAct,
  onAttach,
  intent,
  onScreen,
}: {
  refreshKey: number;
  onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const { data, failed } = useResource(fetchJobs, [refreshKey]);
  const jobs = data?.jobs ?? [];

  // JobsView has no filter of its own, so there's nothing to apply-once by nonce — just stash the
  // focus target. It's read from a ref (not straight off `intent`) in the scroll effect below,
  // because page.tsx clears `screenIntent` right after this view's first onScreen report — which
  // fires on mount, often before the fetch below has resolved — so `intent` itself can go null
  // before there's anything to scroll to.
  const appliedNonceRef = useRef<number | undefined>(undefined);
  const pendingFocusRef = useRef<{ kind: string; ref_id: string } | null>(null);
  useEffect(() => {
    if (!intent || intent.nonce === appliedNonceRef.current) return;
    appliedNonceRef.current = intent.nonce;
    pendingFocusRef.current = intent.focus ?? null;
  }, [intent]);

  // Scroll to the pending focus target once its row exists. Re-runs whenever the row set changes
  // (i.e. once the fetch resolves), plus a short retry for the case where the DOM hasn't
  // committed the new rows yet when this effect fires.
  useEffect(() => {
    const focus = pendingFocusRef.current;
    if (!focus) return;
    const find = () => document.querySelector(`[data-ref="${focus.kind}:${focus.ref_id}"]`);
    const found = find();
    if (found) {
      found.scrollIntoView({ block: "center", behavior: "smooth" });
      pendingFocusRef.current = null;
      return;
    }
    const timer = setTimeout(() => {
      find()?.scrollIntoView({ block: "center", behavior: "smooth" });
      pendingFocusRef.current = null;
    }, 150);
    return () => clearTimeout(timer);
  }, [jobs]);

  // Report what's actually on screen so api.screenState stays current for the next chat turn.
  useEffect(() => {
    onScreen?.({ visible: jobs.slice(0, 40).map((job) => ({ kind: "job", ref_id: job.job_id, label: job.summary })) });
  }, [jobs, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader title="Jobs" subtitle={data ? plural(jobs.length, "job") : undefined} />
      {failed && !data ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so jobs can&apos;t load.</Notice>
      ) : !data ? (
        <Skeleton className="h-96" />
      ) : jobs.length === 0 ? (
        <Notice>등록된 job이 없습니다.</Notice>
      ) : (
        <Panel>
          <ul className="divide-y divide-(--line)">
            {jobs.map((job) => (
              <JobRow key={job.job_id} job={job} onAct={onAct} onAttach={onAttach} />
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}
