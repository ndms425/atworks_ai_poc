// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useState } from "react";
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
} from "web-shared";
import { countPendingJobs, fetchJobs, reportUrl } from "@/lib/api";
import BulkApproveBar from "@/components/BulkApproveBar";
import { attachToggle } from "@/lib/attachments";
import Pager from "@/components/Pager";
import { usePagedList } from "@/lib/usePagedList";
import { useScreenFocus } from "@/lib/useScreenFocus";
import type { AttachedItem, JobSpec, ScreenFilter, ScreenIntent, ScreenTarget } from "@/lib/types";

/** 사람이 읽는 이름. 원값(`run_now`)은 행에서 보여주지 않는다 — 식별자지 라벨이 아니다. */
const KIND_LABEL: Record<string, string> = { run_now: "즉시 실행", scheduled_run: "예약 실행" };

/**
 * 메타 한 칸. 라벨과 값을 분리해 세로로 쌓는다 — 성격이 다른 값(유형·환경·진행·등록일)을
 * 점으로 이어 붙이면 전부 같은 무게로 읽혀서, 눈이 어디에 멈춰야 할지 알 수 없다.
 *
 * 값은 알약(Pill)에 넣지 않는다. 한 행에 알약이 둘 셋 생기면 상태 뱃지(승인 대기)와
 * 같은 모양이 되어, 정말 상태인 것과 그냥 값인 것이 구별되지 않는다.
 *
 * 그렇다고 글자 크기로 구분하지도 않는다. 한 줄에 놓인 네 값은 전부 12.5px 한 크기다 —
 * 크기가 섞이면(고정폭 12px을 끼워 넣는 것까지 포함해) 줄이 들쭉날쭉해 보이고, 무엇이 더
 * 중요한지 말하지도 못한다. 구분은 색과 굵기만으로 준다: 유형은 굵게, 환경은 강조색,
 * 등록일은 흐리게. 크기는 라벨 tier(10.5px)와 값 tier(12.5px) 둘뿐이다.
 */
function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-[10.5px] font-medium tracking-[0.04em] text-(--ink-faint)">{label}</span>
      <div className="flex min-w-0 items-center gap-1.5 text-[12.5px] leading-none text-(--ink)">{children}</div>
    </div>
  );
}

/**
 * 진행 막대. 횟수가 1회뿐이면(즉시 실행) 막대가 정보를 더하지 않으므로 숫자만 둔다.
 * 실행 횟수는 두 binding 모두 정확한 값이라 "최대"를 붙이지 않는다 — 불확실한 쪽은 실행당
 * 건수이고, LATE는 그 상한을 배포 설정이 정한다. job_record만으로는 그 숫자
 * (config.max_matrix_size)를 알 수 없으니 문구로만 알린다.
 */
function Progress({ done, total, late }: { done: number; total: number; late: boolean }) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <>
      <span className="tabular-nums">{done}/{total}회</span>
      {total > 1 ? (
        <span className="h-1 w-12 overflow-hidden rounded-full bg-(--well)" aria-hidden>
          <span className="block h-full rounded-full bg-(--accent)" style={{ width: `${pct}%` }} />
        </span>
      ) : null}
      {late ? <span className="text-(--ink-faint)">최대 · 배포 상한</span> : null}
    </>
  );
}

function JobRow({
  job,
  onAct,
  onAttach,
  attachedRefs,
}: {
  job: JobSpec;
  onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
}) {
  const { change, busy, error, act, canAct } = useChangeActions(job, onAct);
  return (
    <li data-ref={`job:${change.job_id}`} className="px-[18px] py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1 text-[14px] font-medium leading-snug text-(--ink)">{change.summary}</div>
        <div className="flex shrink-0 items-center gap-2">
          <AskButton {...attachToggle(attachedRefs, { kind: "job", ref_id: change.job_id, label: change.summary }, onAttach)} />
          <ChangeStatusPill status={change.status} />
        </div>
      </div>
      <div className="mt-2.5 flex flex-wrap items-start gap-x-6 gap-y-3">
        <Meta label="유형">
          <span className="font-medium">{KIND_LABEL[change.kind] ?? change.kind}</span>
        </Meta>
        <Meta label="대상 환경">
          {/* 크기도 서체도 그대로 두고 색만 바꾼다 — 값 tier에서 벗어나지 않으면서 "이건 서버
              이름"이라는 것만 말한다. */}
          <span className="truncate font-medium text-(--accent)">{change.target_envs.join(" · ")}</span>
        </Meta>
        <Meta label="진행">
          <Progress done={change.executions} total={change.total_executions} late={change.binding === "LATE"} />
        </Meta>
        <Meta label="등록">
          <span className="tabular-nums text-(--ink-soft)">{formatDate(change.created_at)}</span>
        </Meta>
      </div>
      <GuardrailNotes notes={change.guardrail_notes} />
      {change.selection_basis ? <div className="mt-0.5 text-[12.5px] text-(--ink-soft)">선택 근거: {change.selection_basis}</div> : null}
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
  attachedRefs,
  intent,
  onScreen,
  onRefresh,
}: {
  refreshKey: number;
  onAct: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  intent?: ScreenIntent | null;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
  /** 일괄 승인이 끝난 뒤 포털 전체를 다시 읽게 한다(목록·Home 타일이 같이 갱신된다). */
  onRefresh?: () => void;
}) {
  // Jobs has no filter of its own, so the reset key is constant: the stack only ever moves by the
  // Pager's own controls, and a refreshKey bump (an approval landing) re-reads the page you are on
  // instead of throwing you back to page one.
  const load = useCallback((cursor: string | null) => fetchJobs(cursor), []);
  const page = usePagedList<JobSpec>(load, "jobs", [refreshKey]);
  const jobs = page.items;

  // 승인 대기 건수는 화면에 보이는 행이 아니라 원장에서 읽는다: 대기가 여러 페이지에 걸쳐 있어도
  // "전체 승인"이 덮는 수와 화면에 적힌 수가 어긋나지 않아야 하고, 그 수가 곧 서버에 보내는
  // 경쟁 상태 검사값이다.
  const [pending, setPending] = useState(0);
  const readPending = useCallback(() => {
    void countPendingJobs().then((envelope) => setPending(envelope?.total ?? 0));
  }, []);
  useEffect(readPending, [readPending, refreshKey, page.total]);

  // JobsView has no filter of its own, so there's nothing to apply-once by nonce — the shared hook
  // handles the whole focus/scroll lifecycle.
  useScreenFocus(intent, "job", jobs.length > 0);

  // Report what's actually on screen so api.screenState stays current for the next chat turn:
  // the CURRENT PAGE (sliced to 40), which is exactly what the operator can see and point at.
  useEffect(() => {
    onScreen?.({ visible: jobs.slice(0, 40).map((job) => ({ kind: "job", ref_id: job.job_id, label: job.summary, status: job.status })) });
  }, [jobs, onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      {/* The subtitle is the envelope's `total` — every job in the ledger, not the rows on screen. */}
      <PageHeader title="Jobs" subtitle={page.loaded ? plural(page.total, "job") : undefined}>
        <BulkApproveBar
          surface="changes"
          pending={pending}
          noun="job"
          onDone={() => {
            readPending();
            onRefresh?.();
          }}
        />
      </PageHeader>
      {page.failed && !page.loaded ? (
        <Notice>The aTworks AI host isn&apos;t reachable, so jobs can&apos;t load.</Notice>
      ) : !page.loaded ? (
        <Skeleton className="h-96" />
      ) : jobs.length === 0 ? (
        <Notice>등록된 job이 없습니다.</Notice>
      ) : (
        <>
          <Panel>
            {/* cv-rows: content-visibility hint for off-screen rows (globals.css). */}
            <ul className="cv-rows divide-y divide-(--line)">
              {jobs.map((job) => (
                <JobRow key={job.job_id} job={job} onAct={onAct} onAttach={onAttach} attachedRefs={attachedRefs} />
              ))}
            </ul>
          </Panel>
          <Pager page={page} />
        </>
      )}
    </div>
  );
}
