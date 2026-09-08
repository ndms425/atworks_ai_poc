// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";
import { AskButton, Notice, Panel, Pill, Skeleton, useResource } from "web-shared";
import QueryTableView from "@/components/QueryTableView";
import { actOnSavedQuestion, fetchGrowthSummary, fetchSavedQuestions, runSavedQuestion } from "@/lib/api";
import type { QueryResult, SavedQuestion } from "@/lib/types";

/**
 * Home의 "저장 질문" (자가발전 spec §8). 승격은 호스트가 하루 1회 LLM 없이 하고, 이 카드는 그
 * 결과를 읽을 뿐이다 — 제목도 승격 근거(몇 명이 몇 번)도 서버가 낸 값이다.
 *
 * [실행]은 저장된 QuerySpec을 **지금** 다시 계산한다(저장된 답이 아니다). [숨기기]는 사람의
 * 클릭이고 서버에 감사 2행을 남긴다.
 */

/** Home 카드가 보여 주는 최대 개수 (spec §8: ≤5, uses 순). 서버가 이미 그 순서로 정렬해 준다. */
const SHOWN = 5;

function SavedRow({
  question,
  onRun,
  onHide,
  running,
  result,
}: {
  question: SavedQuestion;
  onRun: () => void;
  onHide: () => void;
  running: boolean;
  result: QueryResult | null;
}) {
  return (
    <li className="px-[18px] py-3" data-ref={`saved:${question.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{question.title}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-(--ink-soft)">
            <Pill tone="muted">
              {question.source_users}명 · {question.source_asks}회
            </Pill>
            {question.uses > 0 ? <span>실행 {question.uses}회</span> : null}
          </div>
        </div>
        <span className="inline-flex gap-1.5">
          <AskButton label={running ? "실행 중…" : "실행"} onClick={onRun} />
          <AskButton label="숨기기" onClick={onHide} />
        </span>
      </div>
      {result ? (
        <div className="mt-2.5 border-t border-(--line) pt-2.5">
          <QueryTableView result={result} />
        </div>
      ) : null}
    </li>
  );
}

export default function SavedQuestionsCard({ refreshKey }: { refreshKey: number }) {
  const { data, failed } = useResource(() => fetchSavedQuestions("active", null, SHOWN), [refreshKey]);
  // 빈 상태 문구의 숫자는 호스트 config에서 온다 — 화면에 "3명·5회"를 박아 두면 문턱을 바꾼
  // 배포에서 조용히 거짓말이 된다.
  const { data: summary } = useResource(fetchGrowthSummary, [refreshKey]);
  const [hidden, setHidden] = useState<string[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, QueryResult>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setHidden([]);
    setResults({});
    setError(null);
  }, [refreshKey]);

  // 성장 기능이 꺼진 배포에서는 라우트가 404라 카드 자체가 뜨지 않는다(인사이트 패널과 같은 규칙).
  if (failed && !data) return null;
  if (!data) {
    return (
      <Panel title="저장 질문">
        <div className="p-[18px]">
          <Skeleton className="h-16" />
        </div>
      </Panel>
    );
  }

  const items = data.items.filter((q) => !hidden.includes(q.id));
  const thresholds = summary?.thresholds ?? {};
  const emptyCopy =
    thresholds.min_users && thresholds.min_asks
      ? `아직 승격된 질문이 없습니다 — 같은 질문을 ${thresholds.min_users}명 이상이 ${thresholds.min_asks}회 이상 하면 여기에 올라옵니다.`
      : "아직 승격된 질문이 없습니다 — 같은 질문이 여러 사람에게서 되풀이되면 여기에 올라옵니다.";

  const onRun = async (savedId: string) => {
    setRunning(savedId);
    setError(null);
    const result = await runSavedQuestion(savedId);
    if (result) setResults((prev) => ({ ...prev, [savedId]: result }));
    else setError("질문을 실행하지 못했습니다.");
    setRunning(null);
  };

  const onHide = async (savedId: string) => {
    setError(null);
    const response = await actOnSavedQuestion(savedId, "hide");
    if (response?.ok) setHidden((prev) => [...prev, savedId]);
    else setError("숨기지 못했습니다.");
  };

  return (
    <Panel title="저장 질문" subtitle={`되풀이된 질문 ${data.total.toLocaleString()}개`}>
      {error ? (
        <div className="px-[18px] pt-3">
          <Notice>{error}</Notice>
        </div>
      ) : null}
      {items.length === 0 ? (
        <div className="px-[18px] py-3 text-[12.5px] text-(--ink-soft)">{emptyCopy}</div>
      ) : (
        <ul className="divide-y divide-(--line)">
          {items.map((question) => (
            <SavedRow
              key={question.id}
              question={question}
              running={running === question.id}
              result={results[question.id] ?? null}
              onRun={() => void onRun(question.id)}
              onHide={() => void onHide(question.id)}
            />
          ))}
        </ul>
      )}
    </Panel>
  );
}
