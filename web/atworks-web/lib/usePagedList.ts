// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { type DependencyList, useEffect, useState } from "react";
import { useResource } from "web-shared";
import type { Page } from "@/lib/types";

/** One entry of the cursor stack: the cursor that loaded this page, and how many rows the pages
 * before it showed (so "M개 표시" is exact rather than page-index × page-size). */
interface Step {
  cursor: string | null;
  before: number;
}

const FIRST: Step[] = [{ cursor: null, before: 0 }];

export interface PagedList<T> {
  /** The current page only — never an accumulation. A view renders exactly these rows. */
  items: T[];
  /** The count AFTER the filter, straight from the envelope. Never `items.length`. */
  total: number;
  /** Rows shown up to and including this page, along the path actually walked. */
  shown: number;
  hasNext: boolean;
  atFirst: boolean;
  next: () => void;
  /** Previous page = pop the stack. The keyset contract only walks forward, so the cursor that
   * loaded the page before this one is the only way back to it. */
  prev: () => void;
  first: () => void;
  /** Null until the first page lands (the view's skeleton condition). */
  loaded: boolean;
  failed: boolean;
}

/**
 * Cursor paging over one list route, with a stack so "previous page" is a pop rather than a
 * backwards query the keyset contract doesn't offer.
 *
 * `resetKey` identifies the filter: change it (a new search string, a new status segment) and the
 * stack drops back to page one. It is read DURING RENDER rather than in an effect, so the very
 * first load after a filter change already asks for cursor=null — an effect-based reset would fire
 * one wasted request against the old cursor first. `deps` are extra reload triggers (refreshKey)
 * that must NOT rewind the stack: an approval elsewhere on the page re-reads the page you are on.
 *
 * Note (out of scope, Task 10): a `navigate_screen` focus target that lives beyond the current
 * page is not auto-paged-to — the stack has no way to ask "which page holds run X" without a
 * server-side locate. The focus hook simply finds no row and no-ops; a later task can add a
 * `locate` read to the route contract if it turns out to matter.
 */
export function usePagedList<T>(
  load: (cursor: string | null) => Promise<Page<T> | null>,
  resetKey: string,
  deps: DependencyList = [],
): PagedList<T> {
  const [state, setState] = useState<{ key: string; steps: Step[] }>({ key: resetKey, steps: FIRST });
  // Derived during render: on the render where resetKey changed, `steps` is already back at page
  // one, so the load below runs with cursor=null immediately.
  const steps = state.key === resetKey ? state.steps : FIRST;
  const step = steps[steps.length - 1];

  useEffect(() => {
    setState((current) => (current.key === resetKey ? current : { key: resetKey, steps: FIRST }));
  }, [resetKey]);

  const { data, failed } = useResource(() => load(step.cursor), [resetKey, step.cursor, ...deps]);
  const items = data?.items ?? [];
  const shown = step.before + items.length;

  return {
    items,
    total: data?.total ?? 0,
    shown,
    hasNext: Boolean(data?.next_cursor),
    atFirst: steps.length === 1,
    next: () => {
      const cursor = data?.next_cursor;
      if (!cursor) return;
      setState({ key: resetKey, steps: [...steps, { cursor, before: shown }] });
    },
    prev: () => {
      if (steps.length < 2) return;
      setState({ key: resetKey, steps: steps.slice(0, -1) });
    },
    first: () => setState({ key: resetKey, steps: FIRST }),
    loaded: data !== null,
    failed,
  };
}
