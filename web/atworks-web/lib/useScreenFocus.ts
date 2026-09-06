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
 *
 * Paging (Task 10): views now render one cursor page at a time, so a focus target can be a real
 * entity that simply is not on the page in front of the operator. That is a no-op here, by design —
 * both attempts find no `[data-ref]`, `pending` stays set, and nothing scrolls or throws. Auto-
 * paging TO the target is out of scope: the keyset contract has no "which page holds run X" read,
 * so it would take a new `locate` route. Noted for a later task; the directive's own `note` already
 * names the item in chat, and the operator can page or filter to it.
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
      const el = document.querySelector(`[data-ref="${CSS.escape(`${kind}:${ref}`)}"]`);
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
