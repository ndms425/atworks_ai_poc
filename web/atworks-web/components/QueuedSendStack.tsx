// The queued sends of the visible conversation, stacked above the composer: drag to reorder, edit
// (the text and its attachments go back to the composer), delete. Follows open-design's
// QueuedSendStack (Apache-2.0, apps/web/src/components/chat/QueuedSendStack.tsx); open-design's
// "steer this turn" is not here, because this host's turn loop cannot take input mid-turn.

"use client";

import { type DragEvent, useState } from "react";
import { Icon } from "web-shared";
import type { DropEdge, QueuedSend } from "@/lib/chatQueue";

/** Five cards fit; more scroll inside the stack, with the sixth half-visible as the cue. */
const MAX_STACK_HEIGHT = "max-h-[218px]";

export default function QueuedSendStack({
  items,
  running,
  onEdit,
  onRemove,
  onMove,
}: {
  items: QueuedSend[];
  /** The conversation's reply is running in the background (it was switched away from). */
  running: boolean;
  onEdit: (item: QueuedSend) => void;
  onRemove: (id: string) => void;
  onMove: (dragId: string, targetId: string, edge: DropEdge) => void;
}) {
  const [drag, setDrag] = useState<{ id: string; overId: string | null; edge: DropEdge } | null>(null);

  if (items.length === 0 && !running) return null;

  const edgeOf = (event: DragEvent<HTMLElement>): DropEdge => {
    const box = event.currentTarget.getBoundingClientRect();
    return event.clientY < box.top + box.height / 2 ? "before" : "after";
  };

  return (
    <div className="flex flex-col gap-1.5" aria-label="대기 중인 메시지">
      {running ? (
        <div className="flex items-center gap-2 rounded-[9px] bg-(--info-soft) px-3 py-2 text-[12px] text-(--info)">
          <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-(--info)" aria-hidden />
          이 대화의 답변을 백그라운드에서 만들고 있습니다. 끝나면 여기에 표시됩니다.
        </div>
      ) : null}
      {items.length ? (
        <>
          <div className="flex items-center justify-between px-0.5 text-[11.5px] font-semibold text-(--ink-soft)">
            <span>대기 중 · {items.length}</span>
            <span className="font-normal text-(--ink-faint)">앞 답변이 끝나면 순서대로 보냅니다</span>
          </div>
          <ul className={`panel-scroll flex flex-col gap-1 overflow-y-auto ${MAX_STACK_HEIGHT}`}>
            {items.map((item, index) => {
              const over = drag && drag.overId === item.id && drag.id !== item.id ? drag.edge : null;
              return (
                <li
                  key={item.id}
                  data-testid="queued-send-row"
                  onDragOver={(event) => {
                    if (!drag) return;
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                    const edge = edgeOf(event);
                    if (drag.overId !== item.id || drag.edge !== edge) setDrag({ ...drag, overId: item.id, edge });
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    if (drag) onMove(drag.id, item.id, edgeOf(event));
                    setDrag(null);
                  }}
                  className={`group relative flex items-start gap-1.5 rounded-[10px] border bg-(--card) py-1.5 pl-1 pr-1.5 shadow-(--shadow-sm) ${
                    drag?.id === item.id ? "border-dashed border-(--line-strong) opacity-50" : "border-(--line)"
                  }`}
                >
                  {over ? (
                    <span
                      aria-hidden
                      className={`absolute inset-x-2 h-[2px] rounded-full bg-(--accent) ${over === "before" ? "-top-[3px]" : "-bottom-[3px]"}`}
                    />
                  ) : null}
                  <button
                    type="button"
                    draggable={items.length > 1}
                    disabled={items.length < 2}
                    onDragStart={(event) => {
                      event.dataTransfer.effectAllowed = "move";
                      event.dataTransfer.setData("text/plain", item.id);
                      setDrag({ id: item.id, overId: null, edge: "after" });
                    }}
                    onDragEnd={() => setDrag(null)}
                    aria-label="드래그하여 순서 변경"
                    title="드래그하여 순서 변경"
                    className="mt-0.5 grid h-6 w-5 shrink-0 cursor-grab place-items-center rounded text-(--ink-faint) hover:text-(--ink-soft) disabled:cursor-default disabled:opacity-40"
                  >
                    <Icon name="grip" size={14} />
                  </button>
                  <span className="mt-0.5 w-4 shrink-0 text-[11px] tabular-nums text-(--ink-faint)">{index + 1}</span>
                  <div className="min-w-0 flex-1 py-0.5">
                    <div className="line-clamp-2 whitespace-pre-wrap break-words text-[12.5px] leading-snug text-(--ink)">{item.text}</div>
                    {item.attachments.length ? (
                      <div className="mt-0.5 text-[11px] text-(--ink-soft)">첨부 {item.attachments.length}개</div>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 items-center gap-0.5">
                    <button
                      type="button"
                      onClick={() => onEdit(item)}
                      aria-label="편집"
                      title="편집 — 입력창으로 옮깁니다"
                      className="grid h-6 w-6 place-items-center rounded-md text-(--ink-soft) hover:bg-(--ground) hover:text-(--ink)"
                    >
                      <Icon name="edit" size={13} />
                    </button>
                    <button
                      type="button"
                      onClick={() => onRemove(item.id)}
                      aria-label="삭제"
                      title="삭제"
                      className="grid h-6 w-6 place-items-center rounded-md text-(--ink-soft) hover:bg-(--ground) hover:text-(--danger)"
                    >
                      <Icon name="trash" size={13} />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
    </div>
  );
}
