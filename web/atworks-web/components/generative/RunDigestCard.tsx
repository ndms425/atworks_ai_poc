// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { AskButton, DigestList, DigestRow, GenCard, GenCardHeader } from "web-shared";
import { DIGEST_KINDS, POPULATION_FILTER_LABEL } from "@/lib/kinds";
import type { RunDigestPayload } from "@/lib/types";

export default function RunDigestCard({
  payload,
  onPrefill,
  onAttach,
}: {
  payload: RunDigestPayload;
  onPrefill?: (text: string) => void;
  onAttach?: (item: { kind: "run"; ref_id: string; label: string; field?: string; expected?: string }) => void;
}) {
  const scope = `${POPULATION_FILTER_LABEL[payload.population_filter]} ${payload.population}건 중 먼저 볼 ${payload.shown}건`;
  return (
    <GenCard>
      <GenCardHeader
        title={payload.title ?? "먼저 볼 실패"}
        meta={
          <>
            <b className="font-semibold text-(--ink)">{scope}</b>
            {payload.scorer ? <span> · {payload.scorer}</span> : null}
          </>
        }
      />
      <DigestList>
        {payload.items.map((item, i) => {
          const style = DIGEST_KINDS[item.kind];
          // Rendered only inside `context` below (item.run branch), never doubled into `why`.
          const sub = item.run
            ? `${item.api?.method ?? ""} ${item.api?.path ?? item.run.api_id} · ${item.run.status}${item.run.failed_rules?.length ? ` · ${item.run.failed_rules.join(", ")}` : ""}${item.run.http_status ? ` · HTTP ${item.run.http_status}` : ""}`
            : "";
          // `why` is the model's own explanation, falling back to the scorer's top reason;
          // when neither is present the headline alone is enough, so we omit it.
          const why = item.why_it_matters ?? item.rank?.reasons?.[0];
          return (
            <DigestRow
              key={`${item.ref_id ?? "note"}-${i}`}
              icon={style.icon}
              tone={style.tone}
              headline={item.headline}
              why={why}
              context={
                item.run ? (
                  <>
                    <span>{sub}</span>
                    {item.rank ? <span className="tabular-nums text-(--ink-soft)">{item.rank.score.toFixed(1)}</span> : null}
                    <AskButton label="왜?" onClick={() => onPrefill?.(`${item.run!.run_id} 왜 실패했어`)} />
                  </>
                ) : null
              }
              action={
                item.run
                  ? {
                      label: "채팅에 첨부",
                      onClick: () =>
                        onAttach?.({
                          kind: "run",
                          ref_id: item.run!.run_id,
                          label: `${item.api?.method ?? ""} ${item.api?.path ?? ""}`.trim(),
                          field: item.run!.failed_rules?.[0]?.split(/\s/)[0],
                          expected: item.run!.failed_rules?.[0],
                        }),
                    }
                  : null
              }
            />
          );
        })}
      </DigestList>
      {payload.items.some((i) => i.rank?.reasons?.length) ? (
        <p className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">
          순위는 {payload.scorer} 스코어러의 읽기 순서입니다. 나머지 {Math.max(payload.population - payload.shown, 0)}건이 안전하다는 뜻이 아닙니다.
        </p>
      ) : null}
    </GenCard>
  );
}
