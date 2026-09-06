// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { Button, formatNumber } from "web-shared";
import type { PagedList } from "@/lib/usePagedList";

/**
 * The paging control under every list: "N개 중 M개 표시" where N is the envelope's `total` (the
 * count after the filter, whatever the page size) and M is how many rows the pages walked so far
 * have shown. Both numbers come from the server — nothing here counts a downloaded array.
 *
 * "‹ 이전" pops the cursor stack, "다음 ›" pushes the page's own `next_cursor`, "처음으로" drops
 * back to page one. There is no page-number jump: a keyset cursor has no offset to jump to.
 */
export default function Pager<T>({ page }: { page: PagedList<T> }) {
  // One page and nothing after it: the count is already in the header, so no control is needed.
  if (page.atFirst && !page.hasNext) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 px-[2px] py-1">
      <div className="text-[12.5px] tabular-nums text-(--ink-soft)">
        {formatNumber(page.total)}개 중 {formatNumber(page.shown)}개 표시
      </div>
      <div className="flex items-center gap-2">
        {!page.atFirst ? (
          <Button size="sm" onClick={page.first}>
            처음으로
          </Button>
        ) : null}
        <Button size="sm" onClick={page.prev} disabled={page.atFirst}>
          ‹ 이전
        </Button>
        <Button size="sm" onClick={page.next} disabled={!page.hasNext}>
          다음 ›
        </Button>
      </div>
    </div>
  );
}
