// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { AskButton, formatDateTimeSeconds, Panel, Pill } from "web-shared";
import { RunBodiesView } from "@/components/BodyView";
import { attachToggle } from "@/lib/attachments";
import { RUN_STATUS } from "@/lib/kinds";
import { methodTone } from "@/lib/method";
import type { AttachedItem, RunResult } from "@/lib/types";

/** 라벨 위, 값 아래. 값은 한 크기(12.5px)고 구분은 색과 굵기만 -- Jobs 행·API 상세와 같은 규칙. */
function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-[10.5px] font-medium tracking-[0.04em] text-(--ink-faint)">{label}</span>
      <div className="flex min-w-0 items-center gap-1.5 text-[12.5px] leading-none text-(--ink)">{children}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-(--line) px-[18px] py-3">
      <h3 className="mb-2 text-[12px] font-semibold tracking-[0.02em] text-(--ink)">{title}</h3>
      {children}
    </section>
  );
}

/**
 * 실행 한 건의 상세.
 *
 * 목록이 열 하나씩 내줄 수 없는 것들이 여기 모인다: 정확한 시각(초까지), HTTP 코드, 소요
 * 시간, 실행자, 어느 job 이 냈는지, 테스트 데이터 바인딩, 그리고 요청·응답 본문. 특히
 * 테스트 데이터는 목록에서는 대부분 비어 있는 열이었는데, 채워질 때는 이 실행이 **무엇을
 * 넣고 돌았는지**라 본문과 나란히 읽혀야 하는 값이다.
 *
 * 상태는 여기서도 코드가 낸 판정을 그대로 보여줄 뿐 다시 재지 않는다.
 */
export default function RunDetail({
  run,
  onAttach,
  attachedRefs,
  onClose,
}: {
  run: RunResult;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  onClose: () => void;
}) {
  const style = RUN_STATUS[run.status];
  const label = `${run.api_method ?? ""} ${run.api_path ?? run.api_id}`.trim();
  return (
    <Panel>
      <div className="px-[18px] pb-3 pt-3.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate font-mono text-[13.5px] text-(--ink)">
              {run.api_method ? <span className={`font-semibold ${methodTone(run.api_method)}`}>{run.api_method}</span> : null}{" "}
              {run.api_path ?? run.api_id}
            </div>
            <div className="mt-0.5 truncate text-[12.5px] tabular-nums text-(--ink-soft)">{run.run_id}</div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <Pill tone={style.tone} dot>{style.label}</Pill>
            <AskButton {...attachToggle(attachedRefs, { kind: "run", ref_id: run.run_id, label }, onAttach)} />
            <button
              type="button"
              onClick={onClose}
              aria-label="상세 닫기"
              className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-(--ink-faint) transition hover:bg-(--well) hover:text-(--ink)"
            >
              ×
            </button>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-start gap-x-6 gap-y-3">
          {/* 초까지. 같은 분 안에 수십 건이 쌓이므로 분 단위로는 어느 것이 먼저인지 모른다. */}
          <Meta label="수행 시각">
            <span className="tabular-nums text-(--ink-soft)">{formatDateTimeSeconds(run.executed_at)}</span>
          </Meta>
          <Meta label="HTTP">
            <span className="tabular-nums">{run.http_status ?? "—"}</span>
          </Meta>
          <Meta label="소요">
            <span className="tabular-nums text-(--ink-soft)">{run.duration_ms != null ? `${run.duration_ms}ms` : "—"}</span>
          </Meta>
          <Meta label="대상 환경">
            <span className="font-medium text-(--accent)">{run.target_env}</span>
          </Meta>
          <Meta label="API">
            <span className="tabular-nums text-(--ink-soft)">{run.api_id}</span>
          </Meta>
          <Meta label="실행자">
            <span className="text-(--ink-soft)">{run.executed_by ?? "—"}</span>
          </Meta>
          <Meta label="테스트 데이터">
            <span className="text-(--ink-soft)">{run.test_data_label ?? "바인딩 없음"}</span>
          </Meta>
          <Meta label="job">
            <span className="tabular-nums text-(--ink-soft)">{run.job_id ?? "—"}</span>
          </Meta>
        </div>
      </div>

      {run.failed_rules?.length ? (
        <Section title="실패한 검증 규칙">
          <ul className="space-y-0.5">
            {run.failed_rules.map((rule) => (
              <li key={rule} className="font-mono text-[12px] text-(--danger)">{rule}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title="본문">
        <RunBodiesView runId={run.run_id} />
      </Section>
    </Panel>
  );
}
