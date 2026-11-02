// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { AgentApi } from "./api";

export interface Session {
  /** Null while login is in flight or after it failed. */
  sessionId: string | null;
  /** True until the current profile/body's session request has settled (success or failure). A
   *  caller can only tell "still loading" apart from "settled and failed" (both have
   *  sessionId === null) by checking this. */
  loading?: boolean;
  /** The signed-in operator's name, on merchant sessions. */
  operator?: string;
  /** The signed-in operator's display name, on merchant sessions. */
  operator_name?: string;
  /** The signed-in operator's role, on merchant sessions. */
  role?: string;
  /** The signed-in shopper, on storefront sessions. */
  shopper?: { name: string; tier?: string };
  /** Starts a brand-new session for the same profile/body ("new chat"); the old one stays. */
  restart?: () => void;
  /** Switches to an existing session id the caller has already verified (a reopened chat). */
  adopt?: (sessionId: string) => void;
}

const generations = new WeakMap<AgentApi, number>();

/**
 * Only the newest start installs its token. Key the caller on the profile so per-session
 * state resets with it. When `body` is provided it is sent INSTEAD of `{ user_id: profile }` —
 * used by a merchant portal to restart the session under a chosen `operator_id`.
 */
export function useSession(
  api: AgentApi,
  options: { profile?: string; body?: Record<string, unknown> } = {},
): Session {
  const { profile, body } = options;
  const bodyKey = body ? JSON.stringify(body) : undefined;
  const [session, setSession] = useState<Session>({ sessionId: null, loading: true });
  // Bumped by `restart`: the start effect below re-runs with the same profile/body.
  const [restarts, setRestarts] = useState(0);

  useEffect(() => {
    const generation = (generations.get(api) ?? 0) + 1;
    generations.set(api, generation);
    const current = () => generations.get(api) === generation;
    setSession((prev) => ({ ...prev, loading: true }));
    void (async () => {
      const started = await api.startSession(body ?? (profile ? { user_id: profile } : undefined));
      if (!current()) return;
      api.session = started?.sessionId ?? null;
      setSession({
        sessionId: started?.sessionId ?? null,
        loading: false,
        operator: started?.operator,
        operator_name: started?.operatorName,
        role: started?.role,
        shopper: started?.shopper,
      });
    })();
    return () => {
      generations.set(api, (generations.get(api) ?? 0) + 1);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, profile, bodyKey, restarts]);

  const restart = useCallback(() => setRestarts((n) => n + 1), []);
  const adopt = useCallback(
    (sessionId: string) => {
      // Any start still in flight must not install its token over the one adopted here.
      generations.set(api, (generations.get(api) ?? 0) + 1);
      api.session = sessionId;
      setSession((prev) => ({ ...prev, sessionId, loading: false }));
    },
    [api],
  );

  return useMemo(() => ({ ...session, restart, adopt }), [session, restart, adopt]);
}
