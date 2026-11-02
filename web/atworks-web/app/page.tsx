// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistantRail, Inspector, Notice, type PortalNavItem, PortalShell, type Prefill, useMerchantChat, useSession } from "web-shared";
import AssistantPanel from "@/components/AssistantPanel";
import ConversationMenu from "@/components/ConversationMenu";
import QueuedSendStack from "@/components/QueuedSendStack";
import QuotedRefs, { withQuotes } from "@/components/QuotedRefs";
import OperatorPicker, { OPERATOR_STORAGE_KEY, readStoredOperatorId } from "@/components/OperatorPicker";
import ScreenHighlightOverlay from "@/components/ScreenHighlightOverlay";
import ApisView from "@/components/views/ApisView";
import GrowthView from "@/components/views/GrowthView";
import HomeView from "@/components/views/HomeView";
import JobsView from "@/components/views/JobsView";
import RulesView from "@/components/views/RulesView";
import RunsView from "@/components/views/RunsView";
import { actOnRule, api, fetchOperators, UNREACHABLE } from "@/lib/api";
import { attachKey } from "@/lib/attachments";
import { type QueuedSend, useChatQueue } from "@/lib/chatQueue";
import {
  fetchConversation,
  itemsFromTurns,
  readDraftAttachments,
  readLastConversation,
  writeDraftAttachments,
  writeLastConversation,
} from "@/lib/conversations";
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
  const [composerFocus, setComposerFocus] = useState(0);
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

  // Set once the conversation helpers below exist; the chat hook calls it after a reconnect finds
  // the turn already finished.
  const resumeMissedRef = useRef<(() => void) | null>(null);
  const chat = useMerchantChat<JobSpec>(api, {
    ...session,
    unreachable: UNREACHABLE,
    onPortalRefresh: refreshPortal,
    onScreenDirective,
    onResumeMissed: () => resumeMissedRef.current?.(),
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
  // Parts of earlier replies the operator quoted (QuoteBar); they lead the next question.
  const [quotes, setQuotes] = useState<string[]>([]);
  const quotesRef = useRef(quotes);
  quotesRef.current = quotes;
  // Quotes belong to the conversation they were picked from.
  useEffect(() => setQuotes([]), [session.sessionId]);
  const takeQuoted = useCallback((text: string) => {
    const quoted = withQuotes(quotesRef.current, text);
    if (quotesRef.current.length) setQuotes([]);
    return quoted;
  }, []);
  const send = useCallback(
    (text: string) => {
      sentRef.current = api.pendingAttachments as AttachedItem[];
      setHighlights(null);
      return chat.send(takeQuoted(text));
    },
    [chat.send, takeQuoted],
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

  // One button, both directions: attaching a record that is already attached takes it back off,
  // which is what the row's own label now promises. The identity is `kind:ref_id`, never `order` —
  // `order` is positional, has to stay 1..n without gaps (the send/cleanup effect below keys on it)
  // and is therefore renumbered on the way out. `api.pendingAttachments` is the copy the next send
  // actually reads, so it is rewritten in both directions, never left behind.
  const onAttach = useCallback((item: Omit<AttachedItem, "order">) => {
    setAssistantOpen(true);
    // Straight into the chat field, so the operator can type the question about what they attached.
    setComposerFocus((n) => n + 1);
    setAttached((current) => {
      const key = attachKey(item.kind, item.ref_id);
      const kept = current.filter((a) => attachKey(a.kind, a.ref_id) !== key);
      const next =
        kept.length === current.length
          ? [...current, { ...item, order: current.length + 1 }]
          : kept.map((a, index) => ({ ...a, order: index + 1 }));
      api.pendingAttachments = next;
      return next;
    });
  }, []);

  // What the rows' toggles read to label and fill themselves. `attached` holds a handful of items
  // at most, so rebuilding the set on every change costs nothing.
  const attachedRefs = useMemo(() => new Set(attached.map((a) => attachKey(a.kind, a.ref_id))), [attached]);

  // Taking one back off. `order` is the identity the send/cleanup effect below also keys on, and
  // it has to stay 1..n without gaps, so the survivors are renumbered — and `api.pendingAttachments`
  // is the copy the next send actually reads, so it is rewritten here too, never left behind.
  const onRemoveAttachment = useCallback((order: number) => {
    setAttached((current) => {
      const next = current.filter((item) => item.order !== order).map((item, index) => ({ ...item, order: index + 1 }));
      api.pendingAttachments = next;
      return next;
    });
  }, []);

  // -- Saved conversations: one session is one conversation (host /conversations). ---------------
  // Switching away mid-reply hands that reply (and the conversation's queued sends) to the
  // background: the stream is read to its end off-screen, so the host finishes and saves the turn.
  const clearAttachments = useCallback(() => {
    api.pendingAttachments = [];
    setAttached([]);
  }, []);
  const { resetTranscript, detach, resume: resumeTurn } = chat;
  const { restart: restartSession, adopt: adoptSession } = session;
  const visibleSessionRef = useRef(session.sessionId);
  visibleSessionRef.current = session.sessionId;

  // A background run of the conversation on screen just finished: show what it saved.
  const reloadIfVisible = useCallback(
    async (sessionId: string) => {
      if (visibleSessionRef.current !== sessionId) return;
      const data = await fetchConversation(sessionId);
      if (data && visibleSessionRef.current === sessionId) resetTranscript(itemsFromTurns(data.turns));
      refreshPortal();
    },
    [resetTranscript, refreshPortal],
  );
  const queue = useChatQueue(operatorId, (sessionId) => void reloadIfVisible(sessionId));
  resumeMissedRef.current = () => {
    if (visibleSessionRef.current) void reloadIfVisible(visibleSessionRef.current);
  };
  const { runInBackground } = queue;

  // What a conversation that went to the background is called until the host has saved its first
  // turn (and so its title): the first thing asked in it.
  const handedOffTitles = useRef(new Map<string, string>());
  const handOffToBackground = useCallback(() => {
    const current = visibleSessionRef.current;
    if (!current) return;
    const replying = detach();
    if (replying || queue.items.some((q) => q.sessionId === current)) {
      const first = chat.items.find((item) => item.kind === "user");
      if (first?.kind === "user") handedOffTitles.current.set(current, first.text);
      runInBackground(current, replying);
    }
  }, [detach, queue.items, runInBackground, chat.items]);

  const startNewConversation = useCallback(() => {
    handOffToBackground();
    setHighlights(null);
    resetTranscript([]);
    writeLastConversation(operatorId, null);
    restartSession?.();
  }, [handOffToBackground, resetTranscript, operatorId, restartSession]);

  const openConversation = useCallback(
    async (id: string): Promise<boolean> => {
      // Read with the CURRENT session first: the host answers only for this operator's own ids.
      const data = await fetchConversation(id);
      if (!data) return false;
      handOffToBackground();
      setHighlights(null);
      adoptSession?.(id);
      resetTranscript(itemsFromTurns(data.turns));
      writeLastConversation(operatorId, id);
      // A turn still running for it (a reload mid-reply, or one started elsewhere) is picked up
      // and shown; one this page runs in the background stays there and reloads when done.
      if (!queue.background.has(id)) void resumeTurn();
      return true;
    },
    [handOffToBackground, adoptSession, resetTranscript, operatorId, queue.background, resumeTurn],
  );

  // -- Draft attachments, kept per conversation like the composer's text. -------------------------
  // `attachedOwner` is the conversation the current `attached` belongs to; a switch saves nothing
  // under the wrong id because the load below runs first and re-owns the state.
  const attachedOwner = useRef<string | null>(null);
  useEffect(() => {
    const sessionId = session.sessionId;
    if (!sessionId || attachedOwner.current === sessionId) return;
    const firstLoad = attachedOwner.current === null;
    attachedOwner.current = sessionId;
    const saved = readDraftAttachments(sessionId);
    setAttached((current) => {
      // On the very first load keep what arrived before the session did (a ?attach= link).
      const merged = firstLoad
        ? [...saved, ...current.filter((a) => !saved.some((b) => attachKey(b.kind, b.ref_id) === attachKey(a.kind, a.ref_id)))]
        : saved;
      const ordered = merged.map((a, index) => ({ ...a, order: index + 1 }));
      api.pendingAttachments = ordered;
      return ordered;
    });
  }, [session.sessionId]);
  useEffect(() => {
    if (attachedOwner.current) writeDraftAttachments(attachedOwner.current, attached);
  }, [attached]);

  // -- The send queue (open-design's queued sends). ------------------------------------------------
  const visibleQueue = useMemo(
    () => queue.items.filter((q) => q.sessionId === session.sessionId),
    [queue.items, session.sessionId],
  );
  const runningHere = session.sessionId ? queue.background.has(session.sessionId) : false;

  // Typed while a reply streams: queued with the attachments staged right now; the screen state
  // is read when it is actually sent.
  const onQueue = useCallback(
    (text: string) => {
      const sessionId = visibleSessionRef.current;
      if (!sessionId) return;
      queue.enqueue(sessionId, takeQuoted(text), attached);
      clearAttachments();
    },
    [queue, attached, clearAttachments, takeQuoted],
  );

  // Edit: the text and its attachments go back to the composer, and the item leaves the queue.
  const onEditQueued = useCallback(
    (item: QueuedSend) => {
      const taken = queue.take(item.id);
      if (!taken) return;
      setAttached((current) => {
        const have = new Set(current.map((a) => attachKey(a.kind, a.ref_id)));
        const merged = [...current, ...taken.attachments.filter((a) => !have.has(attachKey(a.kind, a.ref_id)))].map(
          (a, index) => ({ ...a, order: index + 1 }),
        );
        api.pendingAttachments = merged;
        return merged;
      });
      setAssistantOpen(true);
      setPrefill({ text: taken.text, nonce: Date.now() });
    },
    [queue],
  );

  // The conversation on screen drains through the transcript, one send per finished reply.
  const [drainTick, setDrainTick] = useState(0);
  const drainingRef = useRef(false);
  useEffect(() => {
    const sessionId = session.sessionId;
    if (!sessionId || session.loading || chat.busy || drainingRef.current || queue.background.has(sessionId)) return;
    const next = queue.shift(sessionId);
    if (!next) return;
    drainingRef.current = true;
    // chatStream reads api.pendingAttachments synchronously as the send starts; the queued item's
    // own attachments go out with it, and whatever is staged in the composer stays staged.
    const staged = api.pendingAttachments;
    api.pendingAttachments = next.attachments;
    setHighlights(null);
    void chat.send(next.text);
    api.pendingAttachments = staged;
    window.setTimeout(() => {
      drainingRef.current = false;
      setDrainTick((n) => n + 1);
    }, 0);
  }, [session.sessionId, session.loading, chat.busy, chat.send, queue, drainTick]);

  // Every other conversation with queued sends drains in the background (also after a reload).
  useEffect(() => {
    for (const q of queue.items) {
      if (q.sessionId !== session.sessionId && !queue.background.has(q.sessionId)) runInBackground(q.sessionId);
    }
  }, [queue.items, queue.background, session.sessionId, runInBackground]);

  const lockedConversations = useMemo(() => {
    const locked = new Set<string>(queue.background);
    for (const q of queue.items) locked.add(q.sessionId);
    if (chat.busy && session.sessionId) locked.add(session.sessionId);
    return locked;
  }, [queue.background, queue.items, chat.busy, session.sessionId]);
  // Busy conversations the host may not list yet (no saved turn): shown at the top of the list.
  const pendingConversations = useMemo(
    () =>
      [...lockedConversations]
        .filter((id) => id !== session.sessionId)
        .map((id) => ({
          id,
          title:
            handedOffTitles.current.get(id) ?? queue.items.find((q) => q.sessionId === id)?.text ?? "답변 중인 대화",
        })),
    [lockedConversations, session.sessionId, queue.items],
  );
  const conversationActivity = useMemo(() => {
    const activity = new Map<string, string>();
    for (const q of queue.items) {
      activity.set(q.sessionId, `대기 ${queue.items.filter((x) => x.sessionId === q.sessionId).length}`);
    }
    for (const id of queue.background) activity.set(id, "백그라운드 답변 중");
    if (chat.busy && session.sessionId) activity.set(session.sessionId, "답변 중");
    return activity;
  }, [queue.items, queue.background, chat.busy, session.sessionId]);

  // A finished reply (and only that: not a session switch) makes this session the one a reload
  // brings back.
  // A reply starting counts too: a reload mid-reply must come back to THIS conversation, where the
  // running turn is then picked up again (chat.resume).
  useEffect(() => {
    if (chat.busy && session.sessionId) writeLastConversation(operatorId, session.sessionId);
  }, [chat.busy, session.sessionId, operatorId]);
  const completedSeen = useRef(chat.completed);
  useEffect(() => {
    if (chat.completed === completedSeen.current) return;
    completedSeen.current = chat.completed;
    if (session.sessionId) writeLastConversation(operatorId, session.sessionId);
  }, [chat.completed, session.sessionId, operatorId]);

  // Once per operator (first load, or after switching operator), reopen their last conversation.
  const restoredFor = useRef<string | null>(null);
  useEffect(() => {
    if (!session.sessionId || session.loading || !session.operator) return;
    if (restoredFor.current === session.operator) return;
    const operator = session.operator;
    restoredFor.current = operator;
    const last = readLastConversation(operator);
    if (!last || last === session.sessionId) return;
    void openConversation(last).then((ok) => {
      if (!ok) writeLastConversation(operator, null);
    });
  }, [session.sessionId, session.loading, session.operator, openConversation]);

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
              // The old operator's chat must not stay on screen under the new one; a reply in
              // flight finishes in the background like any conversation switch.
              handOffToBackground();
              resetTranscript([]);
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
                chat={runningHere ? { ...chatForPanel, busy: true } : chatForPanel}
                prefill={prefill}
                composerFocus={composerFocus}
                headerActions={
                  <ConversationMenu
                    currentId={session.sessionId}
                    locked={lockedConversations}
                    activity={conversationActivity}
                    pending={pendingConversations}
                    canStartNew={chat.items.length > 0}
                    refreshKey={chat.completed}
                    onNew={startNewConversation}
                    onOpen={(id) => void openConversation(id)}
                    onCurrentDeleted={startNewConversation}
                  />
                }
                queueSlot={
                  <>
                    <QueuedSendStack
                      items={visibleQueue}
                      running={runningHere}
                      onEdit={onEditQueued}
                      onRemove={queue.remove}
                      onMove={queue.move}
                    />
                    <QuotedRefs
                      quotes={quotes}
                      onRemove={(index) => setQuotes((list) => list.filter((_, i) => i !== index))}
                      onClear={() => setQuotes([])}
                    />
                  </>
                }
                onQuote={(text) => {
                  setQuotes((list) => (list.includes(text) ? list : [...list, text]));
                  setComposerFocus((n) => n + 1);
                }}
                onQueue={onQueue}
                busyPlaceholder="답변 중입니다 · 지금 보내면 대기열에 추가됩니다"
                draftKey={session.sessionId}
                onPrefill={askAssistant}
                onAttach={onAttach}
                attachedRefs={attachedRefs}
                attachments={attached}
                onRemoveAttachment={onRemoveAttachment}
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
              <ApisView
                refreshKey={refreshKey}
                onAskAssistant={askAssistant}
                onAttach={onAttach}
                attachedRefs={attachedRefs}
                intent={screenIntent}
                onScreen={onScreen}
              />
            ) : null}
            {view === "runs" ? (
              <RunsView
                refreshKey={refreshKey}
                attachedCount={attached.length}
                onAttach={onAttach}
                attachedRefs={attachedRefs}
                intent={screenIntent}
                onScreen={onScreen}
              />
            ) : null}
            {view === "jobs" ? (
              <JobsView
                refreshKey={refreshKey}
                onAct={chat.actOnChange}
                onAttach={onAttach}
                attachedRefs={attachedRefs}
                intent={screenIntent}
                onScreen={onScreen}
              />
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
      {highlights ? (
        <ScreenHighlightOverlay
          payload={highlights}
          view={view}
          onDismiss={() => setHighlights(null)}
          onGo={(target, focus) => {
            // Same path a navigate directive takes: switch view, then the view scrolls to `focus`.
            // The boxes stay: useScreenHighlight redraws them on the new view.
            setView(target);
            setScreenIntent({ focus, nonce: ++intentSeq.current });
          }}
        />
      ) : null}
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
