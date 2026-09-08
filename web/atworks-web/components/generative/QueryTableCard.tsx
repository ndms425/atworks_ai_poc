// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useState } from "react";
import { AskButton, formatDate, GenCard, GenCardHeader, Pill } from "web-shared";
import { actOnVocabulary, sendFeedback } from "@/lib/api";
import { QUERY_SOURCE_LABEL } from "@/lib/kinds";
import { QueryTable } from "@/components/QueryTableView";
import type { AttachedItem, QueryTablePayload } from "@/lib/types";

/**
 * 자가발전 질의(query_runs) 결과 표. 숫자는 하나도 이 파일에서 계산하지 않는다 — 열 정의도, 값도,
 * 모집단도, 창도 전부 서버가 QueryResult에서 채워 보낸 payload 그대로다.
 */

/**
 * "‘결제 계열’을 경로 접두사 /v1/payment로 해석했습니다 — 맞나요? [예] [아니오]".
 * 확인되면 그 용어는 팀 전체의 컨텍스트에 들어가고(조직 공용), 거부되면 쿨다운 동안 다시 제안되지
 * 않는다. 문구는 서버가 카탈로그 라벨로 만든 것이라 이 파일은 뜻을 해석하지 않는다.
 */
function PendingAliasRow({ alias }: { alias: { term: string; fragment_summary: string } }) {
  const [state, setState] = useState<"asking" | "busy" | "confirmed" | "rejected" | "failed">("asking");
  const act = async (action: "confirm" | "reject") => {
    if (state === "busy") return;
    setState("busy");
    const data = await actOnVocabulary(alias.term, action);
    setState(data?.ok ? (action === "confirm" ? "confirmed" : "rejected") : "failed");
  };
  if (state === "confirmed" || state === "rejected") {
    return (
      <p className="px-3.5 pb-2 text-[12px] text-(--ink-soft)">
        <b className="font-semibold text-(--ink)">‘{alias.term}’</b>{" "}
        {state === "confirmed" ? "저장됨 · 팀 공용" : "거부됨"}
      </p>
    );
  }
  return (
    <p className="px-3.5 pb-2 text-[12px] text-(--ink-soft)">
      <b className="font-semibold text-(--ink)">‘{alias.term}’</b>을(를) {alias.fragment_summary}(으)로
      해석했습니다 — 맞나요?
      <span className="ml-1.5 inline-flex gap-1.5">
        <AskButton label="예" onClick={() => void act("confirm")} />
        <AskButton label="아니오" onClick={() => void act("reject")} />
      </span>
      {state === "failed" ? <span className="ml-1.5 text-(--danger)">저장하지 못했습니다.</span> : null}
    </p>
  );
}

/**
 * 👍/👎 한 줄 (spec §9). 표는 이 답 하나에 대한 평가지 승인이 아니다 — 감사 로그도, 승인 마크도,
 * 원장도 움직이지 않는다. 서버 쪽에서 👍는 답한 턴을 회귀 eval 케이스로 만들고, 👎는 그 턴이
 * 실제로 썼던 어휘에 거부를 센다. 마음이 바뀌면 다시 누를 수 있고, 마지막 표만 남는다.
 */
function FeedbackRow({ turnId, initial }: { turnId: string; initial?: "up" | "down" | null }) {
  const [vote, setVote] = useState<"up" | "down" | null>(initial ?? null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const cast = async (next: "up" | "down") => {
    // 이미 눌린 버튼을 다시 누르면 **지운다**(토글). 그러지 않으면 잘못 누른 표를 되돌릴 자리가
    // 없고, 같은 버튼의 두 번째 클릭은 서버에 아무 뜻도 없는 두 번째 POST가 된다.
    const target = vote === next ? null : next;
    // 진행 중에는 더 받지 않는다 — 연타가 같은 표를 여러 번 보내면 화면과 원장이 어긋난다.
    if (busy) return;
    setBusy(true);
    setFailed(false);
    // 낙관적으로 먼저 칠한다 — 실패하면 되돌리고 한 줄로 말한다.
    const previous = vote;
    setVote(target);
    const data = await sendFeedback(turnId, target);
    if (!data?.ok) {
      setVote(previous);
      setFailed(true);
    }
    setBusy(false);
  };
  return (
    <p className="px-3.5 pb-2 text-[12px] text-(--ink-soft)">
      이 답이 도움이 됐나요?
      <span className="ml-1.5 inline-flex gap-1.5">
        <AskButton label={vote === "up" ? "👍 도움됨" : "👍"} onClick={() => void cast("up")} />
        <AskButton label={vote === "down" ? "👎 아쉬움" : "👎"} onClick={() => void cast("down")} />
      </span>
      {failed ? <span className="ml-1.5 text-(--danger)">저장하지 못했습니다.</span> : null}
    </p>
  );
}

export default function QueryTableCard({
  payload,
  onPrefill,
  onAttach,
}: {
  payload: QueryTablePayload;
  onPrefill?: (text: string) => void;
  onAttach?: (item: Omit<AttachedItem, "order">) => void;
}) {
  const scope = `${payload.population.toLocaleString()}건 · 그룹 ${payload.total_groups.toLocaleString()}개 · ${QUERY_SOURCE_LABEL[payload.source] ?? payload.source}`;
  const hasSamples = payload.rows.some((r) => r.run_ids.length > 0);
  return (
    <GenCard>
      <GenCardHeader title={payload.title} meta={<b className="font-semibold text-(--ink)">{scope}</b>} />
      <p className="px-3.5 pb-2 text-[11.5px] text-(--ink-soft)">
        {formatDate(payload.window.since)} ~ {formatDate(payload.window.until)}
        {payload.compare ? <span className="ml-1.5"><Pill tone="violet">이전 기간 비교</Pill></span> : null}
      </p>
      <div className="panel-scroll overflow-x-auto px-3.5 pb-2">
        {/* 표 자체는 QueryTableView의 `QueryTable` 하나뿐이다 — 저장 질문 화면과 같은 구현.
            data-component 표식은 여기 없다: web-shared/Transcript가 이 블록을 감싸며 이미 달고
            있고, 표에 한 번 더 달면 어긋날 수 있는 표식이 둘이 된다. */}
        <QueryTable
          columns={payload.columns}
          rows={payload.rows}
          trailingHeader={hasSamples}
          renderTrailing={
            hasSamples
              ? (row) =>
                  row.run_ids[0] ? (
                    <AskButton
                      label="채팅에 첨부"
                      onClick={() =>
                        onAttach?.({
                          kind: "run",
                          ref_id: row.run_ids[0],
                          label: Object.values(row.keys).map((v) => v ?? "—").join(" · ") || row.run_ids[0],
                        })
                      }
                    />
                  ) : null
              : undefined
          }
        />
      </div>
      {payload.rows.length === 0 ? (
        <p className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">조건에 맞는 그룹이 없습니다.</p>
      ) : null}
      {payload.compare && payload.compare_note ? (
        <p className="px-3.5 pb-2 text-[11.5px] text-(--ink-soft)">{payload.compare_note}</p>
      ) : null}
      {payload.note ? <p className="px-3.5 pb-2 text-[12px] text-(--ink-soft)">{payload.note}</p> : null}
      {/* 어휘 확인 (spec §7). 확정은 오직 이 클릭에서 일어난다 — 채팅에 "그래"라고 써도 아무것도
          저장되지 않는다. 바로 아래가 이 답에 대한 표(spec §9)로, 서로 다른 것을 묻는다:
          위는 "이 용어를 이렇게 읽는 게 맞나", 아래는 "이 답이 도움이 됐나". */}
      {payload.pending_alias ? <PendingAliasRow alias={payload.pending_alias} /> : null}
      {payload.turn_id ? <FeedbackRow turnId={payload.turn_id} initial={payload.feedback} /> : null}
      <div className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">
        <p>
          {payload.spec_summary}
          {onPrefill ? (
            <span className="ml-1.5">
              <AskButton label="조건 바꿔서 다시" onClick={() => onPrefill(`${payload.spec_summary} 조건을 바꿔서 다시 보여줘`)} />
            </span>
          ) : null}
        </p>
        {/* 스펙과 함께 서버가 실제로 해석한 창·소스를 싣는다. 위 헤더의 날짜는 사람이 읽는
            형식이라 "이 표가 정확히 어느 구간을 센 것인가"를 되짚을 수 없다 — 창은 이 카드에서
            가장 무거운 값이고, 그 값을 기계가 읽을 수 있는 자리는 여기뿐이다. */}
        <details className="mt-1.5">
          <summary className="cursor-pointer">실행된 질의(JSON)</summary>
          <pre className="panel-scroll mt-1 overflow-x-auto whitespace-pre font-mono text-[11.5px] text-(--ink-soft)">
            {JSON.stringify({ spec: payload.spec, window: payload.window, source: payload.source }, null, 2)}
          </pre>
        </details>
      </div>
    </GenCard>
  );
}
