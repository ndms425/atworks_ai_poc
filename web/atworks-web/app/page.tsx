// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistantRail, Inspector, type PortalNavItem, PortalShell, type Prefill, useMerchantChat, useSession } from "web-shared";
import AssistantPanel from "@/components/AssistantPanel";
import ApisView from "@/components/views/ApisView";
import HomeView from "@/components/views/HomeView";
import JobsView from "@/components/views/JobsView";
import RunsView from "@/components/views/RunsView";
import { api, UNREACHABLE } from "@/lib/api";
import type { AttachedItem, JobSpec } from "@/lib/types";

type PortalView = "home" | "apis" | "runs" | "jobs";

function StoreMark() {
  return (
    <span
      aria-hidden
      className="grid h-[34px] w-[34px] shrink-0 place-items-center rounded-[10px] bg-(--ink) text-[15px] font-bold text-(--brand) shadow-[inset_0_-3px_0_rgba(0,0,0,0.18)]"
    >
      AT
    </span>
  );
}

export default function PortalPage() {
  const session = useSession(api);
  const [view, setView] = useState<PortalView>("home");
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [prefill, setPrefill] = useState<Prefill | null>(null);
  const [attached, setAttached] = useState<AttachedItem[]>([]);
  // Bumped whenever a job moves, so every widget re-reads the store the agent or an approval wrote.
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshPortal = useCallback(() => setRefreshKey((value) => value + 1), []);

  const chat = useMerchantChat<JobSpec>(api, {
    ...session,
    unreachable: UNREACHABLE,
    onPortalRefresh: refreshPortal,
  });

  // Snapshot of what a send actually carried, so the busy->false cleanup below can tell a sent
  // attachment apart from one dropped in mid-send (see onAttach/effect below).
  const sentRef = useRef<AttachedItem[]>([]);
  const send = useCallback(
    (text: string) => {
      sentRef.current = api.pendingAttachments as AttachedItem[];
      return chat.send(text);
    },
    [chat.send],
  );
  // The composer, starter chips, and suggestion chips all call `chat.send` directly (inside
  // web-shared), so we hand the panel a chat object whose `send` is wrapped instead of patching
  // every call site.
  const chatForPanel = useMemo(() => ({ ...chat, send }), [chat, send]);

  // The rail is part of the default layout on wide screens; narrow screens open it on demand.
  useEffect(() => {
    setAssistantOpen(window.innerWidth >= 1024);
  }, []);

  const askAssistant = useCallback((text: string) => {
    setAssistantOpen(true);
    setPrefill({ text, nonce: Date.now() });
  }, []);

  const onAttach = useCallback((item: Omit<AttachedItem, "order">) => {
    setAssistantOpen(true);
    setAttached((current) => {
      const next = [...current, { ...item, order: current.length + 1 }];
      api.pendingAttachments = next;
      return next;
    });
  }, []);

  // A report page links back here with ?attach=run:<id>; take it once, then clean the URL.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get("attach");
    if (!raw) return;
    const [kind, ref] = raw.split(":", 2);
    if ((kind === "run" || kind === "api" || kind === "job") && ref) {
      onAttach({ kind, ref_id: ref, label: ref });
    }
    params.delete("attach");
    const clean = `${window.location.pathname}${params.toString() ? `?${params}` : ""}`;
    window.history.replaceState(null, "", clean);
  }, [onAttach]);

  // The attachments ride the next chatStream call and are consumed there; once a reply
  // finishes, drop only the items that actually went out with that send (sentRef, set by
  // `send` above) so an item attached while the previous turn was still streaming survives.
  useEffect(() => {
    if (chat.busy) return;
    const sent = sentRef.current;
    sentRef.current = [];
    if (sent.length === 0) return;
    const sentKeys = new Set(sent.map((item) => `${item.ref_id}:${item.order}`));
    setAttached((current) => {
      if (current.length === 0) return current;
      const remaining = current
        .filter((item) => !sentKeys.has(`${item.ref_id}:${item.order}`))
        .map((item, index) => ({ ...item, order: index + 1 }));
      if (remaining.length === current.length) return current;
      api.pendingAttachments = remaining;
      return remaining;
    });
  }, [chat.busy]);

  const nav = useMemo<PortalNavItem<PortalView>[]>(
    () => [
      { id: "home", label: "Home", icon: "home" },
      { id: "apis", label: "APIs", icon: "signal" },
      { id: "runs", label: "Runs", icon: "chart" },
      { id: "jobs", label: "Jobs", icon: "calendar" },
    ],
    [],
  );

  return (
    <>
      <PortalShell
        brand={{ mark: <StoreMark />, name: "aTworks AI", detail: "API 운영 콘솔" }}
        nav={nav}
        view={view}
        onViewChange={setView}
        operator={{ name: session.operator ?? "Operator", role: "운영자" }}
        assistantOpen={assistantOpen}
        assistantBusy={chat.busy}
        onToggleAssistant={() => setAssistantOpen((open) => !open)}
        rail={
          <AssistantRail open={assistantOpen} storageKey="atworks-ai-merchant-panel-width" onClose={() => setAssistantOpen(false)}>
            {(rail) => (
              <AssistantPanel
                chat={chatForPanel}
                prefill={prefill}
                onPrefill={askAssistant}
                onAttach={onAttach}
                newMemoryCount={chat.newMemoryKeys.size}
                onOpenActivity={() => setActivityOpen(true)}
                {...rail}
              />
            )}
          </AssistantRail>
        }
      >
        {session.sessionId ? (
          <>
            {view === "home" ? <HomeView refreshKey={refreshKey} onAskAssistant={askAssistant} /> : null}
            {view === "apis" ? <ApisView refreshKey={refreshKey} onAskAssistant={askAssistant} onAttach={onAttach} /> : null}
            {view === "runs" ? <RunsView refreshKey={refreshKey} attachedCount={attached.length} onAttach={onAttach} /> : null}
            {view === "jobs" ? <JobsView refreshKey={refreshKey} onAct={chat.actOnChange} onAttach={onAttach} /> : null}
          </>
        ) : null}
      </PortalShell>
      {activityOpen ? (
        <Inspector
          turnCount={chat.turnCount}
          streaming={chat.streaming}
          trace={chat.trace}
          memory={chat.memory}
          newMemoryKeys={chat.newMemoryKeys}
          memoryTitle="Assistant memory"
          onClose={() => setActivityOpen(false)}
        />
      ) : null}
    </>
  );
}
