// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";
import { AskButton, formatDate, Notice, Panel, Pill, Skeleton } from "web-shared";
import { fetchApi, fetchRulesForApi, fetchRunsForApi } from "@/lib/api";
import { RunBodiesView } from "@/components/BodyView";
import { attachToggle } from "@/lib/attachments";
import { RUN_STATUS } from "@/lib/kinds";
import { methodTone } from "@/lib/method";
import type { ApiField, ApiSpec, AttachedItem, FieldLocation, RunResult, ValidationRule } from "@/lib/types";

const LOCATION_LABEL: Record<FieldLocation, string> = {
  path: "경로 변수",
  query: "쿼리",
  body: "요청 본문",
  response: "응답",
};
const LOCATION_ORDER: FieldLocation[] = ["path", "query", "body", "response"];

/** 타입 표기. 배열이면 원소 타입을 괄호가 아니라 `[]` 로 붙여 한눈에 배열임이 보이게 한다. */
function typeLabel(field: ApiField): string {
  const types = field.types.filter((type) => type !== "null");
  const base = types.length ? types.join(" | ") : "null";
  if (field.types.includes("array")) {
    const inner = field.item_type ?? "?";
    return types.length > 1 ? `${base} · ${inner}[]` : `${inner}[]`;
  }
  return field.types.includes("null") && types.length ? `${base} | null` : base;
}

function FieldRow({ field, rulable, ruleColumn }: { field: ApiField; rulable: boolean; ruleColumn: boolean }) {
  // 그 위치의 모든 호출에 있었으면 "항상", 아니면 몇 번 보였는지. 선언이 아니라 관측이므로
  // "필수"라고 단정하지 않는다 -- 한 번도 생략되지 않았다는 사실만 말한다.
  const always = field.of > 0 && field.seen >= field.of;
  return (
    <li className="flex items-baseline justify-between gap-3 py-1">
      {/* 접히는 건 이름뿐이다. 전에는 "규칙 불가"가 이름과 한 덩이라 긴 이름 뒤에서 같이
          잘려 `·규칙 불…` 로 끝났다 -- 잘린 꼬리표는 아무것도 말해 주지 않는다. */}
      <span className="min-w-0 truncate font-mono text-[12px] text-(--ink)" title={field.name}>{field.name}</span>
      <span className="flex shrink-0 items-baseline gap-2">
        {/* 자리는 그룹에 하나라도 규칙 불가가 있을 때만 잡는다 -- 요청 쪽처럼 전부 걸 수
            있는 그룹에서 빈 열을 끌고 다니지 않는다. */}
        {ruleColumn ? (
          <span className="w-[52px] text-right text-[11px] text-(--ink-faint)">{rulable ? null : "규칙 불가"}</span>
        ) : null}
        <span className="font-mono text-[11.5px] text-(--accent)">{typeLabel(field)}</span>
        <span className="w-16 text-right text-[11px] tabular-nums text-(--ink-faint)">
          {always ? "항상" : `${field.seen}/${field.of}`}
        </span>
      </span>
    </li>
  );
}

/**
 * 관측된 스펙. 위치별로 나눠 그린다 -- 경로 변수·쿼리·요청 본문·응답은 성격이 다른데, 전에는
 * 한 줄에 칩으로 쏟아 놓아서 `id` 가 경로 변수인지 본문 필드인지 구별되지 않았다.
 *
 * 머리말이 "관측"이라고 못 박는 이유: 이 배포에는 OpenAPI 문서가 없다. 한 번도 보내지 않은
 * 선택 필드는 여기 없고, 그게 "그 API 가 안 받는다"는 뜻은 아니다. 숫자(`3/44`)가 그 불확실성을
 * 그대로 드러낸다 -- 44번 중 3번만 보였다는 사실 외에는 아무것도 주장하지 않는다.
 */
function FieldSpec({ fields, params }: { fields: ApiField[]; params: string[] }) {
  if (fields.length === 0) {
    return <p className="text-[12.5px] text-(--ink-soft)">관측된 필드가 없습니다.</p>;
  }
  const rulable = new Set(params);
  const groups = LOCATION_ORDER.map((location) => ({
    location,
    rows: fields.filter((field) => field.location === location),
  })).filter((group) => group.rows.length > 0);
  const requestGroups = groups.filter((group) => group.location !== "response");
  const responseGroups = groups.filter((group) => group.location === "response");
  const both = requestGroups.length > 0 && responseGroups.length > 0;

  return (
    <>
      <p className="mb-2 text-[11.5px] text-(--ink-faint)">
        실제 오간 요청·응답에서 관측한 것입니다. 오른쪽 수는 그 위치의 호출 중 이 필드가 보인 횟수입니다.
      </p>
      {/* 왼쪽 단은 **보내는 것**(경로 변수·쿼리·요청 본문), 오른쪽 단은 **받는 것**(응답).
          위치별 그룹을 한 흐름에 늘어놓으면 1줄짜리 경로 변수 옆에 13줄짜리 요청 본문이
          서고, 24줄짜리 응답은 그 아래 왼쪽 반만 쓰면서 오른쪽을 통째로 비운다. 단을
          성격으로 나누면 그 구멍이 사라지고, 읽는 순서도 요청 → 응답이 된다. */}
      <div className={`grid gap-x-6 gap-y-3 ${both ? "xl:grid-cols-2 xl:items-start" : ""}`}>
        {[requestGroups, responseGroups].map((column, index) =>
          column.length === 0 ? null : (
            <div key={index} className="flex min-w-0 flex-col gap-3">
              {column.map((group) => (
                <div key={group.location} className="min-w-0">
                  <div className="flex items-baseline gap-1.5">
                    <h4 className="text-[11.5px] font-semibold text-(--ink-soft)">{LOCATION_LABEL[group.location]}</h4>
                    <span className="text-[11px] tabular-nums text-(--ink-faint)">{group.rows.length}</span>
                  </div>
                  <ul className="mt-0.5 divide-y divide-(--line)">
                    {group.rows.map((field) => (
                      <FieldRow
                        key={`${field.location}:${field.name}`}
                        field={field}
                        // 검증 규칙은 요청 쪽 이름에만 걸 수 있다(게이트가 `params` 로 거절한다).
                        // 응답 필드에 규칙을 걸 수 없다는 것은 화면에서 미리 보이는 편이 낫다.
                        rulable={rulable.has(field.name)}
                        ruleColumn={group.rows.some((row) => !rulable.has(row.name))}
                      />
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          ),
        )}
      </div>
    </>
  );
}

/** 라벨 위, 값 아래. Jobs 행과 같은 규칙이다 — 성격이 다른 값을 점으로 이어 붙이면 전부 같은
 *  무게로 읽혀서 눈이 어디 멈출지 알 수 없다. 값은 한 크기(12.5px), 구분은 색과 굵기만. */
function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-[10.5px] font-medium tracking-[0.04em] text-(--ink-faint)">{label}</span>
      <div className="flex min-w-0 items-center gap-1.5 text-[12.5px] leading-none text-(--ink)">{children}</div>
    </div>
  );
}

/** 상세 안의 한 구역. 제목 옆의 수는 **서버가 센 전체**지, 아래 보이는 줄 수가 아니다. */
function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="border-t border-(--line) px-[18px] py-3">
      <div className="mb-2 flex items-baseline gap-1.5">
        <h3 className="text-[12px] font-semibold tracking-[0.02em] text-(--ink)">{title}</h3>
        {count != null ? <span className="text-[11.5px] tabular-nums text-(--ink-faint)">{count}건</span> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * 파라미터 하나와 거기 걸린 조건들. 한 파라미터에 규칙이 셋이면 이름을 세 번 적는 대신
 * 머리로 한 번 올리고 조건만 밑에 쌓는다 -- 반복되는 이름이 셋, 실제로 다른 값이 셋인데
 * 한 줄에 섞어 두면 눈이 매번 같은 글자를 지나야 한다.
 *
 * 조건 문구가 파라미터 이름으로 시작하면 그 머리말만 떼어 낸다(`amount <= 10000` →
 * `<= 10000`). 정확히 일치할 때만 떼므로, 다른 모양의 문구는 통째로 그대로 보인다 --
 * 서버가 쓴 문장을 추측해서 고치지 않는다.
 */
function ParamRules({ param, rules }: { param: string; rules: ValidationRule[] }) {
  return (
    <li className="py-2 first:pt-0.5">
      <div className="truncate font-mono text-[12.5px] text-(--ink)">{param}</div>
      <ul className="mt-1 space-y-0.5">
        {rules.map((rule) => {
          const head = `${param} `;
          const condition = rule.message.startsWith(head) ? rule.message.slice(head.length) : rule.message;
          return (
            <li key={rule.rule_id} className="truncate pl-2.5 text-[11.5px] text-(--ink-soft)">
              {condition}
            </li>
          );
        })}
      </ul>
    </li>
  );
}

/** 적용된 규칙을 파라미터별로 묶는다. 순서는 받은 순서 그대로 -- 서버가 정한 정렬이다. */
function groupByParam(rules: ValidationRule[]): { param: string; rules: ValidationRule[] }[] {
  const groups: { param: string; rules: ValidationRule[] }[] = [];
  for (const rule of rules) {
    const found = groups.find((group) => group.param === rule.param);
    if (found) found.rules.push(rule);
    else groups.push({ param: rule.param, rules: [rule] });
  }
  return groups;
}

/**
 * 실행 한 줄. 누르면 그 실행의 요청/응답 본문을 펼친다.
 *
 * 본문은 목록 조회에 실리지 않으므로(어떤 읽기도 `bodies`를 조인하지 않는다) 펼칠 때 따로
 * 불러온다 -- 10줄을 그리느라 본문 10개를 끌어오지 않고, 사람이 연 하나만 올라온다.
 */
function RunLine({ run }: { run: RunResult }) {
  const style = RUN_STATUS[run.status];
  const [open, setOpen] = useState(false);

  return (
    <li className="py-1.5">
      <button
        type="button"
        onClick={() => setOpen((shown) => !shown)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 text-left"
      >
        <span className="min-w-0">
          <span className="block truncate text-[12.5px] tabular-nums text-(--ink)">{formatDate(run.executed_at)}</span>
          <span className="block truncate text-[11.5px] text-(--ink-soft)">
            {run.run_id} · {run.target_env}
            {run.http_status ? ` · HTTP ${run.http_status}` : ""}
            {run.failed_rules?.length ? ` · ${run.failed_rules.join(", ")}` : ""}
          </span>
        </span>
        <Pill tone={style.tone} dot>{style.label}</Pill>
      </button>
      {open ? (
        <div className="mt-2"><RunBodiesView runId={run.run_id} /></div>
      ) : null}
    </li>
  );
}

/**
 * 한 API의 상세. 목록 옆에 붙어 선택된 행을 펼친다.
 *
 * 세 번의 읽기 전부 **서버 필터**다(`/apis/{id}`, `/runs?api_id=`, `/rules?api_id=`). 목록 한
 * 페이지를 받아 여기서 거르면 찾는 행이 그 페이지에 없을 때 못 찾고, 구역 제목의 수도 전체가
 * 아니라 그 페이지의 몫이 된다 — Rules 화면이 상태를 서버로 민 것과 같은 이유다.
 */
export default function ApiDetail({
  apiId,
  onAttach,
  attachedRefs,
  onClose,
}: {
  apiId: string;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  /** 좁은 화면에서 목록으로 돌아간다. 넓은 화면에서는 목록이 옆에 그대로 있다. */
  onClose: () => void;
}) {
  const [spec, setSpec] = useState<ApiSpec | null>(null);
  // 적용된 규칙과 대기 중인 규칙을 **따로** 읽는다. 한 번에 받아 여기서 상태별로 나누면
  // 각 수가 그 페이지의 몫이 되고(Rules 화면이 이미 겪은 문제), 무엇보다 "검증 규칙 N건"이
  // 판정하지 않는 반려 건까지 세게 된다.
  const [applied, setApplied] = useState<{ items: ValidationRule[]; total: number } | null>(null);
  const [staged, setStaged] = useState<{ items: ValidationRule[]; total: number } | null>(null);
  const [runs, setRuns] = useState<{ items: RunResult[]; total: number } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    setSpec(null);
    setApplied(null);
    setStaged(null);
    setRuns(null);
    setFailed(false);
    void (async () => {
      const [detail, appliedPage, stagedPage, runPage] = await Promise.all([
        fetchApi(apiId),
        fetchRulesForApi(apiId, "applied"),
        fetchRulesForApi(apiId, "staged"),
        fetchRunsForApi(apiId),
      ]);
      if (!live) return;
      if (!detail) {
        setFailed(true);
        return;
      }
      setSpec(detail.api);
      setApplied(appliedPage ? { items: appliedPage.items, total: appliedPage.total } : { items: [], total: 0 });
      setStaged(stagedPage ? { items: stagedPage.items, total: stagedPage.total } : { items: [], total: 0 });
      setRuns(runPage ? { items: runPage.items, total: runPage.total } : { items: [], total: 0 });
    })();
    return () => {
      live = false;
    };
  }, [apiId]);

  if (failed) return <Panel><div className="p-[18px]"><Notice>이 API를 읽을 수 없습니다.</Notice></div></Panel>;
  if (!spec) return <Skeleton className="h-96" />;

  const label = `${spec.method} ${spec.path}`;
  return (
    <Panel>
      {/* 머리의 동작은 첨부 하나뿐이다. 전에는 "지금 실행 물어보기"·"실패 원인 물어보기"가
          같이 있었는데 둘 다 입력창에 문장을 미리 채워 주는 것뿐이었다 -- 첨부는 패널을 열고
          커서를 옮기고 이 API를 다음 질문에 붙이므로, 거기에 아무 문장이나 쓰면 그 둘을
          포함한다. 고정 문장 두 개는 다음에 무엇을 물을지에 대한 추측이고, 그 추측은 턴마다
          근거를 갖고 나오는 채팅의 제안 칩이 더 잘한다. */}
      <div className="px-[18px] pb-3 pt-3.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate font-mono text-[13.5px] text-(--ink)">
              <span className={`font-semibold ${methodTone(spec.method)}`}>{spec.method}</span> {spec.path}
            </div>
            {/* 이 배포의 이름은 경로에서 만들어진다 -- 같은 글자를 바로 밑에 한 번 더 적지 않는다. */}
            {spec.name && spec.name !== label && spec.name !== spec.path ? (
              <div className="mt-0.5 truncate text-[12.5px] text-(--ink-soft)">{spec.name}</div>
            ) : null}
          </div>
          {/* 첨부는 경로 줄 오른쪽 끝에 둔다. 아래 따로 한 줄을 차지하면 아이콘 하나가
              빈 줄에 떠 있게 되고, 그만큼 파라미터·규칙·실행이 밀린다. */}
          <div className="flex shrink-0 items-center gap-1.5">
            <AskButton {...attachToggle(attachedRefs, { kind: "api", ref_id: spec.api_id, label }, onAttach)} />
            {/* 넓은 화면에서도 보여야 한다. 전에는 `lg:hidden`이 걸려 있어서, 목록과 상세가
                나란히 뜨는 넓은 화면에서는 한 번 연 상세를 닫을 방법이 아예 없었다. */}
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
          <Meta label="ID">
            <span className="tabular-nums text-(--ink-soft)">{spec.api_id}</span>
          </Meta>
          <Meta label="그룹">
            <span className="font-medium text-(--accent)">{spec.group || "—"}</span>
          </Meta>
          <Meta label="수정">
            <span className="tabular-nums text-(--ink-soft)">{formatDate(spec.updated_at)}</span>
          </Meta>
        </div>
      </div>

      <Section title="스펙" count={spec.fields?.length}>
        <FieldSpec fields={spec.fields ?? []} params={spec.params ?? []} />
      </Section>

      {/* 세는 것도 보여 주는 것도 **적용된 규칙**뿐이다. 반려된 초안은 아무것도 판정하지
          않으므로 "이 API에 걸린 규칙"이 아니다 -- 전부 늘어놓으면 제목의 수가 실제로
          판정하는 수보다 커진다. 반려 이력은 Rules 화면의 원장이 갖고 있다.
          승인 대기만 따로 알린다: 그건 사람이 할 일이 남아 있다는 뜻이라 행동을 부른다. */}
      <Section title="검증 규칙" count={applied?.total}>
        {applied?.items.length ? (
          <ul className="divide-y divide-(--line)">
            {groupByParam(applied.items).map((group) => (
              <ParamRules key={group.param} param={group.param} rules={group.rules} />
            ))}
          </ul>
        ) : (
          <p className="text-[12.5px] text-(--ink-soft)">적용 중인 규칙이 없습니다.</p>
        )}
        {staged?.total ? (
          <p className="mt-2 flex items-center gap-1.5 text-[11.5px] text-(--ink-soft)">
            <Pill tone="warn" dot>승인 대기 {staged.total}건</Pill>
            <span>Rules 화면에서 승인하거나 반려할 수 있습니다.</span>
          </p>
        ) : null}
      </Section>

      {/* 제목의 수는 전체 실행 건수고, 아래는 그중 최신 10건이다. */}
      <Section title="최근 실행" count={runs?.total}>
        {runs?.items.length ? (
          <ul className="divide-y divide-(--line)">
            {runs.items.map((run) => <RunLine key={run.run_id} run={run} />)}
          </ul>
        ) : (
          <p className="text-[12.5px] text-(--ink-soft)">아직 실행 기록이 없습니다.</p>
        )}
      </Section>
    </Panel>
  );
}
