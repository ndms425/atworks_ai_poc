// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import {
  ApproveBar,
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
import type { JobSpec } from "@/lib/types";

function JobRow({ job, onAct }: { job: JobSpec; onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null> }) {
  const { change, busy, error, act, canAct } = useChangeActions(job, onAct);
  return (
    <li className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{change.summary}</div>
          <div className="mt-0.5 text-[12px] tabular-nums text-(--ink-soft)">
            {change.kind} · {change.target_envs.join(", ")} ·{" "}
            {change.binding === "LATE" ? "최대 " : ""}
            {change.executions}/{change.total_executions}회 · {formatDate(change.created_at)}
          </div>
        </div>
        <ChangeStatusPill status={change.status} />
      </div>
      <GuardrailNotes notes={change.guardrail_notes} />
      {change.status === "applied" && change.run_ids?.length ? (
        <a className="mt-1.5 inline-block text-[12.5px] underline" href={reportUrl(change.job_id)} target="_blank" rel="noreferrer">
          리포트 열기
        </a>
      ) : null}
      <ApproveBar change={change} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </li>
  );
}

export default function JobsView({ refreshKey, onAct }: { refreshKey: number; onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null> }) {
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
              <JobRow key={job.job_id} job={job} onAct={onAct} />
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}
