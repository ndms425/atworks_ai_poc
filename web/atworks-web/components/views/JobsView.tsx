// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

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
import type { AttachedItem, JobSpec } from "@/lib/types";

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
    <li className="px-[18px] py-3">
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
}: {
  refreshKey: number;
  onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
}) {
  const { data, failed } = useResource(fetchJobs, [refreshKey]);
  const jobs = data?.jobs ?? [];

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
