// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef } from "react";
import type { PortalViewId, ScreenHighlightPayload } from "@/lib/types";

/**
 * Applies a `screen_highlight` directive's targets to the mounted view's rows: for each target,
 * finds `[data-ref="kind:ref_id"]`, adds class `ac-highlight` and sets `data-highlight-n` to the
 * target's number. Mirrors useScreenFocus's shape — apply immediately, then once more after a
 * short retry (~150ms) so a just-navigated view's rows (which render after fetch) still get
 * boxed. Missing targets are silently skipped, on the first pass and the retry alike.
 *
 * `<tr>` cannot reliably host `position: relative` + `::before` across browsers, so when the
 * matched element is a table row the class/attribute go on its first `<td>` instead (the visible
 * cell content still sits inside a positioned ancestor, so the numbered badge anchors correctly).
 * `<li>` rows (jobs, rules) take the class directly.
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
      for (const target of targets) {
        const el = document.querySelector<HTMLElement>(`[data-ref="${target.kind}:${target.ref_id}"]`);
        if (!el) continue;
        const host = el.tagName === "TR" ? ((el.firstElementChild as HTMLElement | null) ?? el) : el;
        host.classList.add("ac-highlight");
        host.setAttribute("data-highlight-n", String(target.number));
        hosts.push(host);
      }
      applied.current = hosts;
    };

    apply();
    const timer = window.setTimeout(apply, 150);
    return () => {
      window.clearTimeout(timer);
      clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targets, deps.view, deps.refreshKey]);
}
