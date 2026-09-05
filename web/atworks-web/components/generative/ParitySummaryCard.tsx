// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { GenCard, GenCardHeader, Pill } from "web-shared";
import type { DiffCluster, ParitySummaryPayload } from "@/lib/types";

function ClusterRow({ cluster }: { cluster: DiffCluster }) {
  return (
    <li className="px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {cluster.paths.length === 0 ? (
          <span className="text-[12px] text-(--ink-soft)">(diff 없음)</span>
        ) : (
          cluster.paths.map((path) => (
            <span key={path} className="rounded-full bg-(--well) px-2 py-0.5 font-mono text-[11.5px] text-(--ink)">
              {path}
            </span>
          ))
        )}
      </div>
      <p className="mt-1 text-[12px] text-(--ink-soft)">
        이 필드 빼면 {cluster.count}개 정리 · {cluster.row_keys.length}개 행
      </p>
    </li>
  );
}

export default function ParitySummaryCard({ payload }: { payload: ParitySummaryPayload }) {
  // clusters는 서버가 이미 count 내림차순으로 정렬해서 보낸다 — 클라이언트에서 재정렬하지 않는다.
  const clusters = payload.clusters ?? payload.parity.clusters;
  return (
    <GenCard>
      <GenCardHeader
        title={payload.title ?? "값 병행 비교"}
        meta={
          <>
            <Pill tone="danger" dot>
              값 불일치 {payload.value_diff_count}
            </Pill>
            <Pill tone="warn" dot>
              상태 불일치 {payload.status_diff_count}
            </Pill>
          </>
        }
      />
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] text-(--ink-soft)">{payload.note}</p> : null}
      {clusters.length === 0 ? (
        <p className="px-3.5 py-3 text-[12.5px] text-(--ink-soft)">불일치 필드 클러스터가 없습니다.</p>
      ) : (
        <ul className="mx-3.5 my-2 divide-y divide-(--line) rounded-[11px] border border-(--line)">
          {clusters.map((cluster, i) => (
            <ClusterRow key={`${cluster.paths.join(",")}-${i}`} cluster={cluster} />
          ))}
        </ul>
      )}
      <p className="px-3.5 pb-3.5 text-[11.5px] text-(--ink-faint)">전체 그리드는 리포트 페이지에서 확인하세요.</p>
    </GenCard>
  );
}
