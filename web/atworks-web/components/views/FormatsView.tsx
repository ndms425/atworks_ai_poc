// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { Notice, Panel, Pill, Skeleton, useResource } from "web-shared";
import { fetchFormats } from "@/lib/api";
import type { FormatDefinition } from "@/lib/types";

function FormatRow({ format }: { format: FormatDefinition }) {
  return (
    <li className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{format.name}</div>
          <div className="mt-0.5 truncate font-mono text-[12px] text-(--ink-soft)">{format.pattern}</div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Pill tone={format.builtin ? "muted" : "info"}>{format.builtin ? "내장" : "저장됨"}</Pill>
          {!format.builtin && format.created_by ? <Pill tone="muted">{format.created_by}</Pill> : null}
        </div>
      </div>
      {format.pass_examples.length > 0 || format.fail_examples.length > 0 ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {format.pass_examples.map((example) => (
            <Pill key={`pass-${example}`} tone="ok">
              {example}
            </Pill>
          ))}
          {format.fail_examples.map((example) => (
            <Pill key={`fail-${example}`} tone="danger">
              {example}
            </Pill>
          ))}
        </div>
      ) : null}
    </li>
  );
}

export default function FormatsView({ refreshKey }: { refreshKey: number }) {
  const { data, failed } = useResource(fetchFormats, [refreshKey]);
  const formats = data?.formats ?? [];

  if (failed && !data) {
    return (
      <Panel title="저장된 포맷">
        <div className="px-[18px] py-3">
          <Notice>The aTworks AI host isn&apos;t reachable, so formats can&apos;t load.</Notice>
        </div>
      </Panel>
    );
  }
  if (!data) {
    return (
      <Panel title="저장된 포맷">
        <div className="p-[18px]">
          <Skeleton className="h-24" />
        </div>
      </Panel>
    );
  }
  if (formats.length === 0) {
    return (
      <Panel title="저장된 포맷">
        <div className="px-[18px] py-3">
          <Notice>등록된 포맷이 없습니다.</Notice>
        </div>
      </Panel>
    );
  }
  return (
    <Panel title="저장된 포맷" subtitle={`${formats.length}개`}>
      <ul className="divide-y divide-(--line)">
        {formats.map((format) => (
          <FormatRow key={format.name} format={format} />
        ))}
      </ul>
    </Panel>
  );
}
