// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef } from "react";
import type { PortalViewId, ScreenHighlightPayload } from "@/lib/types";

/**
 * Applies a `screen_highlight` directive's targets to the mounted view's rows: for each target,
 * finds `[data-ref="kind:ref_id"]`, adds class `ac-highlight` and sets `data-highlight-n` to the
 * target's number. Attempts immediately, then again at 150, 400, 800 and 1500ms so a
 * just-navigated view's rows (which can render well after fetch, e.g. a slow request) still get
 * boxed. Stops scheduling further attempts once every target has been matched, or after the last
 * scheduled attempt. Missing targets are silently skipped on every pass.
 *
 * `<tr>` cannot reliably host `position: relative` + `::before` across browsers, so when the
 * matched element is a table row the class/attribute go on its first `<td>` instead (the visible
 * cell content still sits inside a positioned ancestor, so the numbered badge anchors correctly).
 * `<li>` rows (jobs, rules) take the class directly.
 *
 * Paging (Task 10): "missing targets are silently skipped" now also covers a target on another
 * cursor page — the model highlights what the operator can see, and `screen_state.visible` reports
 * exactly the current page, so a target it grounded in that list IS rendered. Paging TO an
 * off-page target is out of scope (see useScreenFocus).
 */
export function useScreenHighlight(targets: ScreenHighlightPayload["targets"] | null, deps: { view: PortalViewId; refreshKey: number }) {
  const applied = useRef<HTMLElement[]>([]);

  useEffect(() => {
    const clear = () => {
      for (const el of applied.current) {
        el.classList.remove("ac-highlight");
        el.removeAttribute("data-highlight-n");
      }
      applied.current = [];
    };

    if (!targets || targets.length === 0) {
      clear();
      return;
    }

    const apply = () => {
      clear();
      const hosts: HTMLElement[] = [];
      let matched = 0;
      for (const target of targets) {
        const el = document.querySelector<HTMLElement>(`[data-ref="${CSS.escape(`${target.kind}:${target.ref_id}`)}"]`);
        if (!el) continue;
        matched += 1;
        const host = el.tagName === "TR" ? ((el.firstElementChild as HTMLElement | null) ?? el) : el;
        host.classList.add("ac-highlight");
        host.setAttribute("data-highlight-n", String(target.number));
        hosts.push(host);
      }
      applied.current = hosts;
      return matched === targets.length;
    };

    let done = apply();
    const timers: number[] = [];
    // Absolute offsets from the effect firing (not from the previous attempt) so a very slow
    // first fetch still gets a boxed row well after the original 150ms retry would have given up.
    for (const delay of [150, 400, 800, 1500]) {
      if (done) break;
      timers.push(
        window.setTimeout(() => {
          if (done) return;
          done = apply();
        }, delay),
      );
    }

    return () => {
      for (const timer of timers) window.clearTimeout(timer);
      clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targets, deps.view, deps.refreshKey]);
}
