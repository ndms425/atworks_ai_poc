// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";
import { fetchRunBodies } from "@/lib/api";
import type { RunBodies } from "@/lib/types";

/**
 * 본문 한 덩이.
 *
 * 가려진 경로가 있으면 그 사실을 함께 적는다 -- `***`만 보고는 "가려진 값"과 "원래 ***였던
 * 값"을 구별할 수 없고, 구별할 근거는 캡처 시점에 기록한 이 목록뿐이다. 마스킹은 단방향이라
 * 저장된 본문을 나중에 훑어 복원할 방법이 없다.
 */
export function BodyBlock({
  title,
  body,
  maskedPaths,
}: {
  title: string;
  body: Record<string, unknown> | null;
  maskedPaths: string[];
}) {
  return (
    <div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-[11px] font-semibold tracking-[0.02em] text-(--ink-soft)">{title}</span>
        {maskedPaths.length ? (
          <span className="text-[11px] text-(--ink-faint)" title={maskedPaths.join(", ")}>
            가려진 항목 {maskedPaths.length}개
          </span>
        ) : null}
      </div>
      {body ? (
        <pre className="mt-1 max-h-72 overflow-auto rounded-lg bg-(--well) px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-(--ink)">
          {JSON.stringify(body, null, 2)}
        </pre>
      ) : (
        <p className="mt-1 text-[11.5px] text-(--ink-faint)">본문 없음</p>
      )}
    </div>
  );
}

/**
 * 한 실행의 요청·응답 본문을 불러와 나란히 보여 준다.
 *
 * 본문은 어떤 목록 조회에도 실리지 않으므로(읽기 경로가 `bodies`를 조인하지 않는다) 여기서
 * 따로 불러온다 -- 목록 10줄을 그리느라 본문 10개를 끌어오지 않고, 사람이 연 하나만 올라온다.
 */
export function RunBodiesView({ runId }: { runId: string }) {
  const [bodies, setBodies] = useState<RunBodies | null | "none">(null);

  useEffect(() => {
    let live = true;
    setBodies(null);
    void fetchRunBodies(runId).then((found) => {
      if (live) setBodies(found ?? "none");
    });
    return () => {
      live = false;
    };
  }, [runId]);

  if (bodies === null) return <p className="text-[11.5px] text-(--ink-faint)">본문을 읽는 중…</p>;
  if (bodies === "none") {
    // 404 는 두 가지를 뜻한다: 애초에 안 담겼거나(그룹이 본문 캡처를 끔), 보관 기간이 지나
    // 지워졌거나. 라우트가 어느 쪽인지 말해 주지 않으므로 단정하지 않는다.
    return <p className="text-[11.5px] text-(--ink-faint)">저장된 본문이 없습니다 (미캡처이거나 보관 기간 경과).</p>;
  }
  return (
    <div className="grid gap-3 xl:grid-cols-2">
      <BodyBlock title="요청" body={bodies.request} maskedPaths={bodies.request_masked_paths} />
      <BodyBlock title="응답" body={bodies.response} maskedPaths={bodies.response_masked_paths} />
    </div>
  );
}
