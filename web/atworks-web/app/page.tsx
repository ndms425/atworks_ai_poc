// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistantRail, Inspector, Notice, type PortalNavItem, PortalShell, type Prefill, useMerchantChat, useSession } from "web-shared";
import AssistantPanel from "@/components/AssistantPanel";
import OperatorPicker, { OPERATOR_STORAGE_KEY, readStoredOperatorId } from "@/components/OperatorPicker";
import ScreenHighlightOverlay from "@/components/ScreenHighlightOverlay";
import ApisView from "@/components/views/ApisView";
import GrowthView from "@/components/views/GrowthView";
import HomeView from "@/components/views/HomeView";
import JobsView from "@/components/views/JobsView";
import RulesView from "@/components/views/RulesView";
import RunsView from "@/components/views/RunsView";
import { actOnRule, api, fetchOperators, UNREACHABLE } from "@/lib/api";
import { useScreenHighlight } from "@/lib/useScreenHighlight";
import type { RuleAction } from "@/lib/useRuleActions";
import type {
  AttachedItem,
  JobSpec,
  OperatorProfile,
  OperatorRole,
  PortalViewId,
  ScreenDirective,
  ScreenFilter,
  ScreenHighlightPayload,
  ScreenIntent,
  ScreenNavigatePayload,
  ScreenState,
  ScreenTarget,
  ValidationRule,
} from "@/lib/types";
import { ROLE_KO } from "@/lib/types";

const DEFAULT_OPERATOR_ID = "minseong";

// `PortalViewId`와 같은 집합이어야 한다 — `onScreen`이 여기 값을 그대로 `api.screenState.view`로
// 싣고, 호스트의 `ScreenState.view`가 그걸 검증한다(Growth는 그 Literal에 함께 들어갔다).
type PortalView = PortalViewId;

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
  const [operatorId, setOperatorId] = useState<string>(() => readStoredOperatorId() ?? DEFAULT_OPERATOR_ID);
  const session = useSession(api, { body: { operator_id: operatorId } });
  const [view, setView] = useState<PortalView>("home");
  const [operators, setOperators] = useState<OperatorProfile[]>([]);
  const [staleOperatorNotice, setStaleOperatorNotice] = useState(false);
  // Guards the recovery below to at most one attempt per mount -- if the fallback operator id
  // itself somehow fails to start a session, this must not loop retrying forever.
  const staleOperatorFallbackTried = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void fetchOperators().then((data) => {
      if (!cancelled && data) setOperators(data.operators);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // An unknown or stale `atworks-operator` localStorage value (a deleted/renamed operator id)
  // makes POST /session fail: useSession settles with sessionId === null and operator id was
  // requested. Distinguish that from "still loading" via session.loading (both otherwise look
  // identical), then recover: forget the bad id, fall back to a real operator, and say so (M1).
  useEffect(() => {
    if (session.loading || session.sessionId !== null) return;
    if (staleOperatorFallbackTried.current) return;
    staleOperatorFallbackTried.current = true;
    try {
      localStorage.removeItem(OPERATOR_STORAGE_KEY);
    } catch {
      // Storage may be unavailable; the fallback below still applies for this session.
    }
    const fallbackId = operators[0]?.operator_id ?? DEFAULT_OPERATOR_ID;
    setStaleOperatorNotice(true);
    setOperatorId(fallbackId);
  }, [session.loading, session.sessionId, operators]);

  const [assistantOpen, setAssistantOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [prefill, setPrefill] = useState<Prefill | null>(null);
  const [attached, setAttached] = useState<AttachedItem[]>([]);
  // Bumped whenever a job moves, so every widget re-reads the store the agent or an approval wrote.
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshPortal = useCallback(() => setRefreshKey((value) => value + 1), []);

  // A `navigate` directive turns into an intent the mounted view applies once (nonce-tracked);
  // page.tsx clears it after that view's next onScreen report — no separate onIntentConsumed.
  const [screenIntent, setScreenIntent] = useState<ScreenIntent | null>(null);
  // `highlight` payloads land here; a `navigate` directive's setView never clears them (only a
  // user-initiated nav change or a send does — see onUserViewChange/send below).
  const [highlights, setHighlights] = useState<ScreenHighlightPayload | null>(null);
  // Monotonic counter for screenIntent.nonce: two directives landing within the same millisecond
  // must still get distinct nonces, which Date.now() cannot guarantee.
  const intentSeq = useRef(0);

  const onScreenDirective = useCallback((d: ScreenDirective) => {
    if (d.kind === "navigate") {
      const p = d.payload as unknown as ScreenNavigatePayload;
      setView(p.view);
      setScreenIntent({ focus: p.focus, filter: p.filter, nonce: ++intentSeq.current });
    } else {
      setHighlights(d.payload as unknown as ScreenHighlightPayload);
    }
  }, []);

  const chat = useMerchantChat<JobSpec>(api, {
    ...session,
    unreachable: UNREACHABLE,
    onPortalRefresh: refreshPortal,
    onScreenDirective,
  });

  useScreenHighlight(highlights?.targets ?? null, { view, refreshKey });

  // A nav-bar click by the operator clears stale boxes; a directive-caused navigate never does
  // (its setView call above is raw, untouched by this). Passed to PortalShell as onViewChange —
  // never invoked by the directive path, so no handshake/ref is needed to tell the two apart.
  const onUserViewChange = useCallback((v: PortalView) => {
    setHighlights(null);
    setView(v);
  }, []);

  // The mounted view reports what it actually shows; this pushes that (plus the current view)
  // onto api.screenState, which rides every chat turn as `screen_state`. Clearing screenIntent
  // here (rather than a callback the view calls back) is the "consumed" signal from ruling 1.
  // `onScreen` itself stays stable across intent set/clear (a functional, unconditional
  // setScreenIntent(null) is a no-op when already null) so a mounted view's report effect —
  // which depends on onScreen — never re-fires just because screenIntent changed.
  const onScreen = useCallback(
    (report: { filter?: ScreenFilter; visible: ScreenTarget[] }) => {
      api.screenState = { view, filter: report.filter, visible: report.visible.slice(0, 40) } as ScreenState;
      setScreenIntent(null);
    },
    [view],
  );

  // A bare view switch (before the newly mounted view has reported anything, e.g. its data is
  // still loading) still needs api.screenState.view to be current — with an empty visible list
  // rather than the outgoing view's stale rows. But child effects run before parent effects, so
  // when the newly mounted view's own onScreen report already landed (same view) this must NOT
  // clobber it back to an empty visible list — only reset when the view actually changed.
  useEffect(() => {
    if ((api.screenState as ScreenState | null)?.view !== view) {
      api.screenState = { view, visible: [] } as ScreenState;
    }
  }, [view]);

  // Snapshot of what a send actually carried, so the busy->false cleanup below can tell a sent
  // attachment apart from one dropped in mid-send (see onAttach/effect below).
  const sentRef = useRef<AttachedItem[]>([]);
  const send = useCallback(
    (text: string) => {
      sentRef.current = api.pendingAttachments as AttachedItem[];
      setHighlights(null);
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

  // /changes/ 가 아니라 /rules/{id}/{action}으로 나간다 — AssistantPanel의 onRuleAction과 같은 방식으로
  // {ok, change} 응답에서 change만 꺼내고, 성공하면 다른 위젯도 다시 읽도록 refreshPortal을 부른다.
  const onRuleAct = useCallback(
    async (ruleId: string, action: RuleAction): Promise<ValidationRule | null> => {
      const data = await actOnRule(ruleId, action);
      const change = data?.change ?? null;
      if (change) refreshPortal();
      return change;
    },
    [refreshPortal],
  );

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
      { id: "rules", label: "Rules", icon: "check" },
      // 6번째 — 자가발전 spec §10. 시스템이 배운 어휘/저장 질문과 아직 답하지 못한 질문이 여기 있다.
      { id: "growth", label: "Growth", icon: "message" },
    ],
    [],
  );

  return (
    <>
      <PortalShell
        brand={{ mark: <StoreMark />, name: "aTworks AI", detail: "API 운영 콘솔" }}
        nav={nav}
        view={view}
        onViewChange={onUserViewChange}
        operator={{
          name: session.operator_name ?? session.operator ?? "Operator",
          role: ROLE_KO[session.role as OperatorRole] ?? "운영자",
        }}
        operatorControl={
          <OperatorPicker
            value={operatorId}
            onChange={(id) => {
              setOperatorId(id);
              refreshPortal();
            }}
          />
        }
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
        {/* Also require session.operator to already match the picked operatorId: an operator switch
            restarts the session (useSession's effect), but the old sessionId lingers in `session`
            state until that finishes. Gating on both unmounts the views for that gap instead of
            letting a child's data fetch race out under the stale token — see OperatorPicker. */}
        {session.sessionId && session.operator === operatorId ? (
          <>
            {view === "home" ? (
              <HomeView refreshKey={refreshKey} operatorId={operatorId} onAskAssistant={askAssistant} onScreen={onScreen} />
            ) : null}
            {view === "apis" ? (
              <ApisView refreshKey={refreshKey} onAskAssistant={askAssistant} onAttach={onAttach} intent={screenIntent} onScreen={onScreen} />
            ) : null}
            {view === "runs" ? (
              <RunsView
                refreshKey={refreshKey}
                attachedCount={attached.length}
                onAttach={onAttach}
                intent={screenIntent}
                onScreen={onScreen}
              />
            ) : null}
            {view === "jobs" ? (
              <JobsView refreshKey={refreshKey} onAct={chat.actOnChange} onAttach={onAttach} intent={screenIntent} onScreen={onScreen} />
            ) : null}
            {view === "rules" ? <RulesView refreshKey={refreshKey} onAct={onRuleAct} intent={screenIntent} onScreen={onScreen} /> : null}
            {/* Growth는 화면 지시어의 목적지가 아니다(kind를 넓히지 않는 라운드) — `intent`를 받지
                않고 `onScreen`으로 `visible: []`만 보고한다. */}
            {view === "growth" ? <GrowthView refreshKey={refreshKey} onScreen={onScreen} /> : null}
          </>
        ) : staleOperatorNotice ? (
          <div className="p-6">
            <Notice>선택한 운영자를 찾을 수 없어 기본 운영자로 전환했습니다.</Notice>
          </div>
        ) : null}
      </PortalShell>
      {highlights ? <ScreenHighlightOverlay payload={highlights} onDismiss={() => setHighlights(null)} /> : null}
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
