// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef, useState } from "react";
import type { ScreenIntent, ScreenTargetKind } from "@/lib/types";

/**
 * Applies a navigate directive's `focus` to this view exactly once per intent.nonce, then scrolls the
 * matching `[data-ref="kind:ref_id"]` row into view once rows exist. The scroll is triggered by a local
 * `focusTick` that bumps on EVERY stash — not by the row-list reference — so a focus-only re-navigate to an
 * already-mounted, unchanged view still scrolls. One ~150ms retry covers rows that render after fetch.
 * A later unrelated refresh never re-scrolls: the stash is cleared after a successful scroll.
 */
export function useScreenFocus(intent: ScreenIntent | null | undefined, kind: ScreenTargetKind, rowsReady: boolean) {
  const lastNonce = useRef<number | null>(null);
  const pending = useRef<string | null>(null); // ref_id awaiting scroll
  const [focusTick, setFocusTick] = useState(0);

  useEffect(() => {
    if (!intent?.focus || intent.focus.kind !== kind || intent.nonce === lastNonce.current) return;
    lastNonce.current = intent.nonce;
    pending.current = intent.focus.ref_id;
    setFocusTick((t) => t + 1); // per-stash trigger
  }, [intent, kind]);

  useEffect(() => {
    const ref = pending.current;
    if (!ref || !rowsReady) return;
    const tryScroll = () => {
      const el = document.querySelector(`[data-ref="${kind}:${ref}"]`);
      if (!el) return false;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      pending.current = null;
      return true;
    };
    if (tryScroll()) return;
    const timer = window.setTimeout(tryScroll, 150);
    return () => window.clearTimeout(timer);
  }, [focusTick, rowsReady, kind]);
}
