// The assistant header's conversation controls: "new chat" and the saved-conversation list
// (search, open, rename, delete). Flow follows open-design's chat-pane conversation menu
// (Apache-2.0, apps/web/src/components/ChatPane.tsx); the component is this portal's own.

"use client";

import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { formatDateTime, Icon, type IconName } from "web-shared";
import {
  type ConversationSummary,
  deleteConversation,
  fetchConversations,
  renameConversation,
} from "@/lib/conversations";

const titleOf = (text: string) => {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > 40 ? `${flat.slice(0, 39)}…` : flat;
};

function HeaderButton({
  icon,
  label,
  onClick,
  disabled = false,
  active = false,
}: {
  icon: IconName;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      aria-pressed={active || undefined}
      className={`grid h-[30px] w-[30px] shrink-0 place-items-center rounded-lg transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
        active ? "bg-(--ground) text-(--ink)" : "text-(--ink-soft) hover:bg-(--ground) hover:text-(--ink)"
      }`}
    >
      <Icon name={icon} size={17} />
    </button>
  );
}

function Row({
  conversation,
  current,
  locked,
  status,
  onOpen,
  onRenamed,
  onDeleted,
}: {
  conversation: ConversationSummary;
  current: boolean;
  locked: boolean;
  status?: string;
  onOpen: () => void;
  onRenamed: (next: ConversationSummary) => void;
  onDeleted: (currentDeleted: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conversation.title);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [working, setWorking] = useState(false);

  // An armed delete disarms itself, so a stray second click minutes later deletes nothing.
  useEffect(() => {
    if (!confirmDelete) return;
    const timer = window.setTimeout(() => setConfirmDelete(false), 5000);
    return () => window.clearTimeout(timer);
  }, [confirmDelete]);

  const saveTitle = async () => {
    const title = draft.trim();
    setEditing(false);
    if (!title || title === conversation.title) return setDraft(conversation.title);
    setWorking(true);
    const data = await renameConversation(conversation.id, title);
    setWorking(false);
    if (data) onRenamed({ ...data.conversation, current: conversation.current });
    else setDraft(conversation.title);
  };

  const remove = async () => {
    setWorking(true);
    const data = await deleteConversation(conversation.id);
    setWorking(false);
    setConfirmDelete(false);
    if (data?.ok) onDeleted(data.current_deleted);
  };

  const onKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && !event.nativeEvent.isComposing) void saveTitle();
    if (event.key === "Escape") {
      event.stopPropagation();
      setDraft(conversation.title);
      setEditing(false);
    }
  };

  return (
    <li
      className={`group relative flex items-center gap-1 rounded-[9px] pr-1 ${
        current ? "bg-(--accent-soft)" : "hover:bg-(--ground)"
      }`}
    >
      {current ? <span aria-hidden className="absolute inset-y-2 left-0 w-[3px] rounded-full bg-(--accent)" /> : null}
      {editing ? (
        <input
          autoFocus
          value={draft}
          maxLength={80}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKey}
          onBlur={() => void saveTitle()}
          aria-label="대화 이름"
          className="m-1 min-w-0 flex-1 rounded-md border border-(--line-strong) bg-(--card) px-2 py-1.5 text-[13px] text-(--ink) outline-none focus:border-(--accent)"
        />
      ) : (
        <button
          type="button"
          onClick={onOpen}
          disabled={working}
          className="min-w-0 flex-1 px-3 py-2 text-left disabled:cursor-not-allowed"
        >
          <span className="block truncate text-[13px] font-medium text-(--ink)">{conversation.title}</span>
          <span className="mt-0.5 block text-[11.5px] tabular-nums text-(--ink-soft)">
            {current ? "지금 대화 · " : ""}
            {conversation.turn_count ? `${conversation.turn_count}턴 · ${formatDateTime(conversation.updated_at)}` : "첫 답변 저장 전"}
          </span>
          {status ? (
            <span className="mt-1 inline-flex items-center gap-1 rounded-full bg-(--info-soft) px-1.5 py-px text-[10.5px] font-medium text-(--info)">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-(--info)" aria-hidden />
              {status}
            </span>
          ) : null}
        </button>
      )}
      {!editing ? (
        <div className={`flex shrink-0 items-center gap-0.5 ${confirmDelete ? "" : "opacity-0 group-focus-within:opacity-100 group-hover:opacity-100"}`}>
          {confirmDelete ? (
            <button
              type="button"
              onClick={() => void remove()}
              disabled={working}
              className="rounded-md bg-(--danger) px-2 py-1 text-[11.5px] font-semibold text-white"
            >
              삭제 확인
            </button>
          ) : (
            <>
              <button
                type="button"
                aria-label="이름 바꾸기"
                title="이름 바꾸기"
                onClick={() => {
                  setDraft(conversation.title);
                  setEditing(true);
                }}
                className="grid h-7 w-7 place-items-center rounded-md text-(--ink-soft) hover:bg-(--card) hover:text-(--ink)"
              >
                <Icon name="edit" size={14} />
              </button>
              <button
                type="button"
                aria-label="삭제"
                title={locked ? "답변이 끝난 뒤 삭제할 수 있습니다" : "삭제"}
                onClick={() => setConfirmDelete(true)}
                disabled={locked}
                className="grid h-7 w-7 place-items-center rounded-md text-(--ink-soft) hover:bg-(--card) hover:text-(--danger) disabled:opacity-40"
              >
                <Icon name="trash" size={14} />
              </button>
            </>
          )}
        </div>
      ) : null}
    </li>
  );
}

export default function ConversationMenu({
  currentId,
  locked,
  activity,
  pending,
  canStartNew,
  refreshKey,
  onNew,
  onOpen,
  onCurrentDeleted,
}: {
  currentId: string | null;
  /** Conversations with a reply running or sends queued: they cannot be deleted until done. */
  locked: ReadonlySet<string>;
  /** A short live status per conversation ("백그라운드 답변 중", "대기 2"). */
  activity: ReadonlyMap<string, string>;
  /** Busy conversations the host may not list yet (no turn saved): listed until it does. */
  pending: ReadonlyArray<{ id: string; title: string }>;
  /** False while the current chat is still empty — a second blank chat would only clutter. */
  canStartNew: boolean;
  /** Bumped when a turn finishes, so an open list picks up the new title and count. */
  refreshKey: number;
  onNew: () => void;
  onOpen: (id: string) => void;
  onCurrentDeleted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<ConversationSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  // Host rows first; a busy conversation the host does not list yet goes on top as a placeholder.
  const rows: ConversationSummary[] | null =
    items === null
      ? null
      : [
          ...pending
            .filter((p) => !items.some((c) => c.id === p.id))
            .filter((p) => !query || p.title.includes(query))
            .map((p) => ({ id: p.id, title: titleOf(p.title), turn_count: 0, created_at: "", updated_at: "" })),
          ...items,
        ];

  const load = useCallback(async (q: string) => {
    const page = await fetchConversations(q);
    if (!page) return;
    setItems(page.items);
    setTotal(page.total);
  }, []);

  // Searching asks the host (titles live there); a short pause keeps typing from firing per key.
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void load(query), query ? 200 : 0);
    return () => window.clearTimeout(timer);
  }, [open, query, load, refreshKey, currentId]);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    // Not `relative`: the list anchors to the panel header (which is), so it always fits inside the
    // panel — the rail clips anything that would spill past its edge.
    <div ref={rootRef} className="flex items-center gap-0.5">
      <HeaderButton icon="chat-plus" label="새 대화" onClick={onNew} disabled={!canStartNew} />
      <HeaderButton icon="history" label="대화 기록" onClick={() => setOpen((value) => !value)} active={open} />
      {open ? (
        <div className="absolute right-2 top-[calc(100%+6px)] z-50 flex w-[min(360px,calc(100%-16px))] flex-col rounded-[14px] border border-(--line) bg-(--card) shadow-(--shadow-lg)">
          <div className="flex items-center justify-between gap-2 px-3.5 pb-2 pt-3">
            <span className="text-[13px] font-semibold text-(--ink)">대화 기록</span>
            <span className="text-[11.5px] tabular-nums text-(--ink-soft)">{total}개</span>
          </div>
          <label className="mx-3 flex items-center gap-2 rounded-[9px] border border-(--line-strong) px-2.5 py-1.5 focus-within:border-(--accent)">
            <Icon name="search" size={14} className="text-(--ink-faint)" />
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="대화 제목 검색"
              aria-label="대화 제목 검색"
              className="min-w-0 flex-1 bg-transparent text-[13px] text-(--ink) outline-none placeholder:text-(--ink-faint)"
            />
          </label>
          <ul className="panel-scroll mt-2 max-h-[min(60vh,420px)] overflow-y-auto px-1.5 pb-1.5" aria-label="저장된 대화">
            {rows === null ? (
              <li className="px-3 py-6 text-center text-[12.5px] text-(--ink-soft)">불러오는 중…</li>
            ) : rows.length === 0 ? (
              <li className="px-3 py-6 text-center text-[12.5px] leading-relaxed text-(--ink-soft)">
                {query ? "검색과 맞는 대화가 없습니다." : "저장된 대화가 없습니다. 질문을 하면 여기에 남습니다."}
              </li>
            ) : (
              rows.map((conversation) => (
                <Row
                  key={conversation.id}
                  conversation={conversation}
                  current={conversation.id === currentId}
                  locked={locked.has(conversation.id)}
                  status={activity.get(conversation.id)}
                  onOpen={() => {
                    setOpen(false);
                    if (conversation.id !== currentId) onOpen(conversation.id);
                  }}
                  onRenamed={(next) => setItems((list) => list?.map((c) => (c.id === next.id ? next : c)) ?? null)}
                  onDeleted={(currentDeleted) => {
                    setItems((list) => list?.filter((c) => c.id !== conversation.id) ?? null);
                    setTotal((n) => Math.max(0, n - 1));
                    if (currentDeleted) onCurrentDeleted();
                  }}
                />
              ))
            )}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
