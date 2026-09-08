// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Button, formatDate, formatNumber, Notice, PageHeader, Panel, Pill, Segmented, Skeleton, StatStrip, StatTile, useResource } from "web-shared";
import Pager from "@/components/Pager";
import QueryTableView from "@/components/QueryTableView";
import {
  actOnSavedQuestion,
  actOnVocabulary,
  fetchAskLog,
  fetchGrowthSummary,
  fetchSavedQuestions,
  fetchVocabulary,
  runSavedQuestion,
} from "@/lib/api";
import { ASK_OUTCOME_LABEL, SAVED_QUESTION_STATUS, UNMET_REASON_LABEL, VOCABULARY_STATUS } from "@/lib/kinds";
import { usePagedList } from "@/lib/usePagedList";
import type { AskEntry, QueryResult, SavedQuestion, ScreenFilter, ScreenTarget, VocabularyEntry } from "@/lib/types";

/**
 * Growth — 6번째 포털 뷰 (자가발전 spec §10). 시스템이 **무엇을 배웠는지** 보고, 틀린 것을
 * 사람이 바로잡는 자리다. 네 탭이 각각 하나의 라우트를 읽는다:
 *
 *   배운 어휘   `GET /vocabulary?status&cursor` + `POST /vocabulary/{term}/confirm|reject|delete`
 *   저장 질문   `GET /saved-questions?status&cursor` + `/{id}/run` + `/{id}/hide|unhide`
 *   미충족 질문 `GET /growth/summary?days` 의 `unmet_clusters` + `GET /ask-log?outcome=unmet`
 *   이번 주     `GET /growth/summary?days`
 *
 * **숫자는 하나도 여기서 계산하지 않는다.** 타일도 표도 서버가 낸 COUNT를 그대로 찍고, 비율은
 * 아예 보여 주지 않는다 — answered/partial/unmet은 각각 자기 건수로 선다. 화면이 나눗셈을
 * 시작하면 창(`days`)이 바뀔 때마다 분모가 무엇이었는지 두 곳에서 다르게 기억하게 된다.
 *
 * 어휘의 "의미" 한 줄도 마찬가지로 서버가 만든 `fragment_summary`다(카탈로그 라벨). 포털이
 * `fragment`를 저 나름으로 번역하면 카드 푸터·컨텍스트 블록·이 표가 서로 다른 문장을 읽는다.
 *
 * 성장 기능이 꺼진 배포(`enable_growth=False`)에서는 모든 라우트가 404라 각 탭이 `Notice`로
 * 떨어진다 — 인사이트 패널과 같은 규칙이다.
 */

type Tab = "vocabulary" | "saved" | "unmet" | "week";
type VocabFilter = "confirmed" | "pending" | "rejected" | "all";
type SavedFilter = "active" | "hidden" | "all";

const TABS: { id: Tab; label: string }[] = [
  { id: "vocabulary", label: "배운 어휘" },
  { id: "saved", label: "저장 질문" },
  { id: "unmet", label: "미충족 질문" },
  { id: "week", label: "이번 주" },
];

/** `?days=` — 두 탭(이번 주·미충족)이 같은 `/growth/summary` 창을 읽으므로 상태는 하나다. */
const DAY_OPTIONS: { id: "7" | "30"; label: string }[] = [
  { id: "7", label: "7일" },
  { id: "30", label: "30일" },
];

function Cell({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`py-2 pr-3 align-top text-[12.5px] text-(--ink) ${className}`}>{children}</td>;
}

function HeadCell({ children, className = "" }: { children?: ReactNode; className?: string }) {
  return <th className={`py-1.5 pr-3 text-left text-[11.5px] font-semibold text-(--ink-soft) ${className}`}>{children}</th>;
}

// -- 배운 어휘 -------------------------------------------------------------------------

/** [삭제]가 팔에 닿을 때까지의 시간 — 두 번째 클릭이 진짜 삭제가 되는 창. */
const DELETE_ARM_MS = 4000;

function VocabularyRow({ entry, busy, onAct }: { entry: VocabularyEntry; busy: boolean; onAct: (action: "confirm" | "reject" | "delete") => void }) {
  const status = VOCABULARY_STATUS[entry.status];
  // [삭제]는 [거부] 바로 옆에 있는데 둘의 결과가 다르다: 거부는 되돌릴 수 있고(다시 확인),
  // 삭제는 항과 그 사실(fact)이 함께 사라진다 — 잘못 누르면 되돌릴 화면이 없다. 그래서 한 번
  // 누르면 "정말 삭제"로 팔이 서고, 두 번째 클릭만 실제로 보낸다. 4초 뒤 저절로 내려가므로
  // 다른 행으로 옮겨 간 사이 팔이 계속 서 있지 않는다.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(false), DELETE_ARM_MS);
    return () => clearTimeout(timer);
  }, [armed]);
  return (
    // data-ref는 화면 지시어 오버레이/포커스가 질의하는 앵커와 같은 형식으로 달아 둔다. 다만
    // 지시어 **대상 kind**는 이번 라운드에 넓히지 않는다(spec §10): `ScreenTargetKind`에 "vocab"은
    // 없고, 그래서 모델은 이 행을 지목하지 못한다 — 나중에 kind를 넓히면 앵커가 이미 붙어 있다.
    <tr data-ref={`vocab:${entry.term}`} className="border-t border-(--line)">
      <Cell className="font-mono font-medium whitespace-nowrap">{entry.term}</Cell>
      <Cell className="min-w-[180px]">{entry.fragment_summary}</Cell>
      <Cell className="whitespace-nowrap">
        <Pill tone={status.tone}>{status.label}</Pill>
      </Cell>
      <Cell className="whitespace-nowrap text-(--ink-soft)">
        {entry.proposed_by}
        {entry.confirmed_by ? ` → ${entry.confirmed_by}` : ""}
      </Cell>
      <Cell className="whitespace-nowrap tabular-nums text-(--ink-soft)">
        확정 {entry.confirmations} · 사용 {entry.uses} · 거부 {entry.rejections}
      </Cell>
      <Cell className="whitespace-nowrap tabular-nums text-(--ink-soft)">{formatDate(entry.confirmed_at ?? entry.proposed_at)}</Cell>
      <Cell className="whitespace-nowrap">
        <span className="inline-flex gap-1.5">
          {entry.status !== "confirmed" ? (
            <Button size="sm" disabled={busy} onClick={() => onAct("confirm")}>
              확인
            </Button>
          ) : null}
          {entry.status !== "rejected" ? (
            <Button size="sm" disabled={busy} onClick={() => onAct("reject")}>
              거부
            </Button>
          ) : null}
          <Button
            size="sm"
            disabled={busy}
            onClick={() => {
              if (armed) {
                setArmed(false);
                onAct("delete");
              } else {
                setArmed(true);
              }
            }}
          >
            {armed ? "정말 삭제" : "삭제"}
          </Button>
        </span>
      </Cell>
    </tr>
  );
}

function VocabularyTab({ refreshKey }: { refreshKey: number }) {
  const [status, setStatus] = useState<VocabFilter>("all");
  // 확인/거부/삭제가 성공하면 이 값이 오르고, 그것이 usePagedList의 `deps`라 **보고 있던 페이지를**
  // 다시 읽는다(리셋 키가 아니므로 1페이지로 되감기지 않는다). 응답의 entry로 행을 갈아끼우지
  // 않는 이유: 삭제는 행이 사라지고 total도 줄어든다 — 서버가 센 숫자를 다시 받는 편이 정직하다.
  const [seq, setSeq] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback((cursor: string | null) => fetchVocabulary(status === "all" ? undefined : status, cursor), [status]);
  const page = usePagedList<VocabularyEntry>(load, status, [refreshKey, seq]);

  const act = async (term: string, action: "confirm" | "reject" | "delete") => {
    setBusy(term);
    setError(null);
    const response = await actOnVocabulary(term, action);
    setBusy(null);
    if (response?.ok) setSeq((n) => n + 1);
    else setError(`'${term}' 항목을 바꾸지 못했습니다.`);
  };

  if (page.failed && !page.loaded) return <Notice>어휘 목록을 읽지 못했습니다 — 호스트가 닿지 않거나 성장 기능이 꺼져 있습니다.</Notice>;
  if (!page.loaded) return <Skeleton className="h-72" />;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented<VocabFilter>
          label="어휘 상태 필터"
          value={status}
          onChange={setStatus}
          options={[
            { id: "confirmed", label: "확정" },
            { id: "pending", label: "제안됨" },
            { id: "rejected", label: "거부됨" },
            { id: "all", label: "전체" },
          ]}
        />
        <span className="text-[12.5px] tabular-nums text-(--ink-soft)">{formatNumber(page.total)}개</span>
      </div>
      {error ? <Notice>{error}</Notice> : null}
      {page.items.length === 0 ? (
        <Notice>
          {status === "all"
            ? "아직 배운 어휘가 없습니다 — 채팅에서 쓰는 말이 반복되면 여기에 제안으로 올라옵니다."
            : "해당 상태의 어휘가 없습니다."}
        </Notice>
      ) : (
        <>
          <Panel title="조직 공용 어휘" subtitle="확정된 항목만 다른 운영자의 대화에 실립니다">
            <div className="panel-scroll overflow-x-auto px-[18px] pb-3">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    <HeadCell>용어</HeadCell>
                    <HeadCell>의미</HeadCell>
                    <HeadCell>상태</HeadCell>
                    <HeadCell>제안자 → 확정자</HeadCell>
                    <HeadCell>확정·사용·거부</HeadCell>
                    <HeadCell>마지막 시각</HeadCell>
                    <HeadCell />
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((entry) => (
                    <VocabularyRow key={entry.term} entry={entry} busy={busy === entry.term} onAct={(action) => void act(entry.term, action)} />
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
          <Pager page={page} />
        </>
      )}
    </div>
  );
}

// -- 저장 질문 -------------------------------------------------------------------------

function SavedRow({
  question,
  running,
  result,
  onRun,
  onToggle,
}: {
  question: SavedQuestion;
  running: boolean;
  result: QueryResult | null;
  onRun: () => void;
  onToggle: () => void;
}) {
  const status = SAVED_QUESTION_STATUS[question.status];
  return (
    // Home 카드와 같은 앵커 형식. 여기서도 지시어 kind는 넓히지 않는다(위 VocabularyRow 참고).
    <li data-ref={`saved:${question.id}`} className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{question.title}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] tabular-nums text-(--ink-soft)">
            <Pill tone="muted">
              {question.source_users}명 · {question.source_asks}회
            </Pill>
            <span>실행 {question.uses}회</span>
            {question.last_used_at ? <span>· 마지막 사용 {formatDate(question.last_used_at)}</span> : null}
            <Pill tone={status.tone}>{status.label}</Pill>
          </div>
        </div>
        <span className="inline-flex gap-1.5">
          {/* 숨긴 질문의 [실행]은 서버에서 언제나 404다(`POST /saved-questions/{id}/run`은 active만
              돈다) — 누를 수 있게 두면 "질문을 실행하지 못했습니다"라는 거짓 고장으로 읽힌다.
              복원이 먼저다. */}
          <Button size="sm" disabled={running || question.status === "hidden"} onClick={onRun}>
            {running ? "실행 중…" : "실행"}
          </Button>
          <Button size="sm" onClick={onToggle}>
            {question.status === "hidden" ? "복원" : "숨기기"}
          </Button>
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

function SavedTab({ refreshKey }: { refreshKey: number }) {
  const [status, setStatus] = useState<SavedFilter>("active");
  const [seq, setSeq] = useState(0);
  const [running, setRunning] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, QueryResult>>({});
  const [error, setError] = useState<string | null>(null);
  // `status=all`도 서버가 아는 값이라 라우트에 그대로 넘긴다 — 한 페이지를 클라이언트에서 쪼개면
  // 3페이지의 숨긴 질문이 "숨김" 세그먼트에서 그냥 안 보인다(RulesView와 같은 이유).
  const load = useCallback((cursor: string | null) => fetchSavedQuestions(status, cursor), [status]);
  const page = usePagedList<SavedQuestion>(load, status, [refreshKey, seq]);
  const { data: summary } = useResource(() => fetchGrowthSummary(), [refreshKey]);

  // 실행 결과는 **그때의 창으로 지금 계산된** 표다. 세그먼트를 바꾸거나 새로고침을 누르면
  // 목록은 다시 읽히는데 표만 남아 있으면, 화면의 두 반쪽이 서로 다른 시점을 말한다.
  useEffect(() => {
    setResults({});
    setError(null);
  }, [refreshKey, status]);

  const onRun = async (savedId: string) => {
    setRunning(savedId);
    setError(null);
    const result = await runSavedQuestion(savedId);
    setRunning(null);
    if (result) setResults((prev) => ({ ...prev, [savedId]: result }));
    else setError("질문을 실행하지 못했습니다.");
  };

  const onToggle = async (question: SavedQuestion) => {
    setError(null);
    const response = await actOnSavedQuestion(question.id, question.status === "hidden" ? "unhide" : "hide");
    if (response?.ok) setSeq((n) => n + 1);
    else setError("상태를 바꾸지 못했습니다.");
  };

  if (page.failed && !page.loaded) return <Notice>저장 질문을 읽지 못했습니다 — 호스트가 닿지 않거나 성장 기능이 꺼져 있습니다.</Notice>;
  if (!page.loaded) return <Skeleton className="h-72" />;

  // 빈 상태의 문턱 숫자는 호스트 config에서 온다(`thresholds`) — 화면에 "3명·5회"를 박아 두면
  // 문턱을 바꾼 배포에서 조용히 거짓말이 된다. Home의 저장 질문 카드와 같은 규칙.
  const thresholds = summary?.thresholds ?? {};
  const emptyCopy =
    status !== "active"
      ? "해당 상태의 저장 질문이 없습니다."
      : thresholds.min_users && thresholds.min_asks
        ? `아직 승격된 질문이 없습니다 — 같은 질문을 ${thresholds.min_users}명 이상이 ${thresholds.min_asks}회 이상 하면 여기에 올라옵니다.`
        : "아직 승격된 질문이 없습니다 — 같은 질문이 여러 사람에게서 되풀이되면 여기에 올라옵니다.";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented<SavedFilter>
          label="저장 질문 상태 필터"
          value={status}
          onChange={setStatus}
          options={[
            { id: "active", label: "표시 중" },
            { id: "hidden", label: "숨김" },
            { id: "all", label: "전체" },
          ]}
        />
        <span className="text-[12.5px] tabular-nums text-(--ink-soft)">{formatNumber(page.total)}개</span>
      </div>
      {error ? <Notice>{error}</Notice> : null}
      {page.items.length === 0 ? (
        <Notice>{emptyCopy}</Notice>
      ) : (
        <>
          <Panel title="승격된 저장 질문" subtitle="[실행]은 저장된 답이 아니라 지금의 창으로 다시 계산합니다">
            <ul className="divide-y divide-(--line)">
              {page.items.map((question) => (
                <SavedRow
                  key={question.id}
                  question={question}
                  running={running === question.id}
                  result={results[question.id] ?? null}
                  onRun={() => void onRun(question.id)}
                  onToggle={() => void onToggle(question)}
                />
              ))}
            </ul>
          </Panel>
          <Pager page={page} />
        </>
      )}
    </div>
  );
}

// -- 미충족 질문 -----------------------------------------------------------------------

function UnmetTab({ refreshKey, days, onDays }: { refreshKey: number; days: number; onDays: (days: number) => void }) {
  const { data: summary, failed } = useResource(() => fetchGrowthSummary(days), [refreshKey, days]);
  const load = useCallback((cursor: string | null) => fetchAskLog("unmet", cursor), []);
  const page = usePagedList<AskEntry>(load, "unmet", [refreshKey]);
  const clusters = summary?.unmet_clusters ?? [];

  if (failed && !summary) return <Notice>미충족 질문을 읽지 못했습니다 — 호스트가 닿지 않거나 성장 기능이 꺼져 있습니다.</Notice>;
  if (!summary) return <Skeleton className="h-72" />;

  return (
    <div className="flex flex-col gap-3">
      <DaysPicker days={days} onDays={onDays} />
      <Panel title="답하지 못한 질문 군집" subtitle={`최근 ${days}일 · 큰 군집부터`}>
        {clusters.length === 0 ? (
          <div className="px-[18px] py-3 text-[12.5px] text-(--ink-soft)">이 창에서 답하지 못한 질문이 없습니다.</div>
        ) : (
          <div className="panel-scroll overflow-x-auto px-[18px] pb-3">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <HeadCell>사유</HeadCell>
                  <HeadCell>건수</HeadCell>
                  <HeadCell>마지막 시각</HeadCell>
                  <HeadCell>예시 (마스킹된 요약)</HeadCell>
                </tr>
              </thead>
              <tbody>
                {clusters.map((cluster) => (
                  <tr key={cluster.cluster_key} className="border-t border-(--line)">
                    <Cell className="whitespace-nowrap">
                      <Pill tone="warn">{cluster.reason ? (UNMET_REASON_LABEL[cluster.reason] ?? cluster.reason) : "사유 없음"}</Pill>
                    </Cell>
                    <Cell className="tabular-nums">{formatNumber(cluster.count)}</Cell>
                    <Cell className="whitespace-nowrap tabular-nums text-(--ink-soft)">{formatDate(cluster.last_at)}</Cell>
                    <Cell className="min-w-[220px]">
                      {cluster.example}
                      {cluster.wanted ? <span className="block text-[11.5px] text-(--ink-soft)">원한 것: {cluster.wanted}</span> : null}
                    </Cell>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {page.failed && !page.loaded ? (
        <Notice>질문 기록을 읽지 못했습니다.</Notice>
      ) : !page.loaded ? (
        <Skeleton className="h-40" />
      ) : page.items.length === 0 ? (
        <Notice>기록에 남은 미충족 질문이 없습니다.</Notice>
      ) : (
        <>
          {/* 위 군집 표는 `days` 창을 쓰지만 이 기록은 `GET /ask-log?outcome=unmet` — 창 인자가
              없는 전체 기간이다. 같은 화면에 "최근 7일" 옆에 창 없는 목록을 놓고 부제를 비워
              두면, 7일이 이 표에도 걸린 것처럼 읽힌다. */}
          <Panel title="미충족 질문 기록" subtitle={`${formatNumber(page.total)}건 · 전체 기간 · 최신순`}>
            <div className="panel-scroll overflow-x-auto px-[18px] pb-3">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    <HeadCell>시각</HeadCell>
                    <HeadCell>질문 (마스킹된 요약)</HeadCell>
                    <HeadCell>결과</HeadCell>
                    <HeadCell>사유</HeadCell>
                    <HeadCell>운영자</HeadCell>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((entry) => {
                    // 결과 열은 라우트의 필터(`outcome=unmet`)가 아니라 **행이 들고 온 값**을 찍는다 —
                    // 필터가 무엇이든 행은 서버가 채점한 그대로를 말한다.
                    const outcome = ASK_OUTCOME_LABEL[entry.outcome];
                    return (
                      <tr key={entry.turn_id} className="border-t border-(--line)">
                        <Cell className="whitespace-nowrap tabular-nums text-(--ink-soft)">{formatDate(entry.at)}</Cell>
                        <Cell className="min-w-[220px]">{entry.question}</Cell>
                        <Cell className="whitespace-nowrap">
                          {outcome ? <Pill tone={outcome.tone}>{outcome.label}</Pill> : entry.outcome}
                        </Cell>
                        <Cell className="whitespace-nowrap">
                          {entry.unmet_reason ? <Pill tone="warn">{UNMET_REASON_LABEL[entry.unmet_reason] ?? entry.unmet_reason}</Pill> : "—"}
                        </Cell>
                        <Cell className="whitespace-nowrap text-(--ink-soft)">{entry.operator}</Cell>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Panel>
          <Pager page={page} />
        </>
      )}
    </div>
  );
}

// -- 이번 주 ---------------------------------------------------------------------------

function DaysPicker({ days, onDays }: { days: number; onDays: (days: number) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Segmented<"7" | "30"> label="집계 창" value={String(days) as "7" | "30"} onChange={(value) => onDays(Number(value))} options={DAY_OPTIONS} />
      <span className="text-[12.5px] text-(--ink-soft)">서버가 이 창으로 센 숫자입니다</span>
    </div>
  );
}

function WeekTab({ refreshKey, days, onDays }: { refreshKey: number; days: number; onDays: (days: number) => void }) {
  const { data, failed } = useResource(() => fetchGrowthSummary(days), [refreshKey, days]);

  if (failed && !data) return <Notice>성장 요약을 읽지 못했습니다 — 호스트가 닿지 않거나 성장 기능이 꺼져 있습니다.</Notice>;
  if (!data) return <Skeleton className="h-56" />;

  // 전부 COUNT다. answered/partial/unmet을 asks_total로 나눈 **비율은 일부러 보여 주지 않는다** —
  // 화면이 나눗셈을 시작하면 분모(창·필터)를 서버와 두 곳에서 다르게 기억하게 되고, 그 순간
  // "숫자는 결정론 코드가 낸다"가 깨진다. 창은 `data.window_days`(서버가 되돌려준 값)로 읽는다.
  const windowDays = data.window_days ?? days;
  return (
    <div className="flex flex-col gap-3">
      <DaysPicker days={days} onDays={onDays} />
      {/* 두 줄로 나눠 놓은 것은 취향이 아니라 `StatStrip`의 계약이다: 그 그리드는 4칸 한 줄을
          전제로 테두리를 넣는다(xl에서 `[&>*+*]:border-l`, 그 아래에서 3번째부터 `border-t`).
          8개를 한 스트립에 넣으면 xl에서 두 번째 줄에 가로줄이 없고 5번째 칸에 세로줄이 생겨
          두 줄이 한 줄처럼 이어져 보인다. web-shared는 두 역할이 함께 쓰는 코드라 여기서
          고치지 않는다 — 부르는 쪽이 4개씩 부른다. 나뉜 자리도 뜻이 있다: 위는 질문의 결말,
          아래는 그 결과 시스템이 배운 것. */}
      <Panel title={`최근 ${windowDays}일`} subtitle="전부 서버가 센 건수입니다 (비율 없음)">
        <StatStrip>
          <StatTile label="질문 수" value={formatNumber(data.asks_total)} />
          <StatTile label="답변" value={formatNumber(data.answered)} />
          <StatTile label="부분" value={formatNumber(data.partial)} />
          <StatTile label="미충족" value={formatNumber(data.unmet)} />
        </StatStrip>
        <div className="border-t border-(--line)">
          <StatStrip>
            <StatTile label="👍" value={formatNumber(data.up)} />
            <StatTile label="👎" value={formatNumber(data.down)} />
            <StatTile label="새 어휘" value={formatNumber(data.new_terms)} />
            <StatTile label="새 저장 질문" value={formatNumber(data.new_saved)} />
          </StatStrip>
        </div>
      </Panel>
      {data.asks_total === 0 ? <Notice>이 창에는 기록된 질문이 없습니다.</Notice> : null}
    </div>
  );
}

// -- 뷰 --------------------------------------------------------------------------------

export default function GrowthView({
  refreshKey,
  onScreen,
}: {
  refreshKey: number;
  onScreen?: (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => void;
}) {
  const [tab, setTab] = useState<Tab>("vocabulary");
  const [days, setDays] = useState(7);
  // 성장 기능이 꺼진 배포에서는 이 뷰의 네 라우트가 모두 404다. 탭마다 제 Notice를 띄우면
  // 사람이 네 번 눌러 네 번 같은 실패를 읽는다 — 한 번만 말하고, 탭 자체를 내린다. 네비게이션은
  // 그대로 둔다(포털은 호스트 config를 모른다): 들어와서 왜 비었는지 읽는 편이, 눌리지 않는
  // 메뉴보다 정직하다.
  const { data: probe, failed: probeFailed } = useResource(() => fetchGrowthSummary(days), [refreshKey]);

  // Growth의 행(어휘 term·저장 질문 id)은 `ScreenTargetKind`에 없는 종류라 **보고할 대상이 없다**:
  // 빈 목록을 보내 api.screenState의 view만 현재로 유지한다(HomeView와 같은 자리, 같은 이유).
  // 지시어 대상 kind를 넓히는 것은 이번 라운드 밖이다(spec §10).
  useEffect(() => {
    onScreen?.({ visible: [] });
  }, [onScreen]);

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader title="Growth" subtitle="시스템이 배운 것과, 아직 답하지 못한 것">
        {probeFailed && !probe ? null : <Segmented<Tab> label="Growth 탭" value={tab} onChange={setTab} options={TABS} />}
      </PageHeader>
      {probeFailed && !probe ? (
        <Notice>성장 기능이 꺼져 있습니다 — 호스트의 `enable_growth`가 꺼져 있거나 호스트에 닿지 않습니다.</Notice>
      ) : (
        <>
          {tab === "vocabulary" ? <VocabularyTab refreshKey={refreshKey} /> : null}
          {tab === "saved" ? <SavedTab refreshKey={refreshKey} /> : null}
          {tab === "unmet" ? <UnmetTab refreshKey={refreshKey} days={days} onDays={setDays} /> : null}
          {tab === "week" ? <WeekTab refreshKey={refreshKey} days={days} onDays={setDays} /> : null}
        </>
      )}
    </div>
  );
}
