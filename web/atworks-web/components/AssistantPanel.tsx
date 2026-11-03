// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { type ReactNode, useCallback, useMemo } from "react";
import { type AssistantChatItem, AssistantPanel as PanelShell, type MerchantChat, type Prefill } from "web-shared";
import { actOnFormatBatch, actOnProfile, actOnRule } from "@/lib/api";
import { collapseApprovals } from "@/lib/approvals";
import { humanizeFormAnswers } from "@/lib/formAnswers";
import type { FormatBatchAction } from "@/lib/useFormatBatchActions";
import type { ProfileAction } from "@/lib/useProfileActions";
import type { RuleAction } from "@/lib/useRuleActions";
import type { AttachedItem, ComparisonProfile, FormatBatch, JobSpec, PortalViewId, ValidationRule } from "@/lib/types";
import GenerativeBlock from "./generative";

const COPY = {
  title: "aTworks 어시스턴트",
  intro: "API 실행, 실패 원인, 예약 실행에 대해 물어보세요.",
  starters: [
    "최근 실패한 api 중 risk 있는 것 가져와",
    "지난 1주일 업데이트된 api 오늘부터 3일간 매일 9시에 실행해줘",
    "지금 실행 가능한 api 알려줘",
    "승인 대기 중인 job 보여줘",
  ],
  label: "aTworks 어시스턴트에게 메시지 보내기",
  placeholder: "API, 실행, 승인에 대해 물어보세요…",
};

const ATTACH_KIND_LABEL: Record<AttachedItem["kind"], string> = { api: "API", run: "실행", job: "작업" };

/** What the next message will carry, directly above the field it will be typed in. Attaching used
 * to update state and `api.pendingAttachments` and show nothing anywhere, so on a wide screen —
 * where the rail is already open — the button looked dead; the records still rode the next turn,
 * which is worse than doing nothing: invisible and unremovable. */
function Attachments({ items, onRemove }: { items: AttachedItem[]; onRemove: (order: number) => void }) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1" aria-label={`첨부 ${items.length}건`}>
      <span className="text-[11.5px] font-medium text-(--ink-soft)">이 질문에 첨부:</span>
      {items.map((item) => (
        <span key={`${item.kind}:${item.ref_id}:${item.order}`} className="chip max-w-full gap-1 py-0.5 pr-1">
          <span className="text-(--ink-soft)">{ATTACH_KIND_LABEL[item.kind]}</span>
          <span className="truncate">{item.label}</span>
          <button
            type="button"
            onClick={() => onRemove(item.order)}
            aria-label={`${item.label} 첨부 해제`}
            className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-(--ink-faint) transition hover:bg-(--well) hover:text-(--ink)"
          >
            ×
          </button>
        </span>
      ))}
    </div>
  );
}

// A reply that ends by asking a yes/no question but came without chips gets "네" / "아니요" to tap,
// so the operator never has to type a one-word answer. The prompt asks the model for the
// question's own answers as chips; this only fills the gap when it did not. Open questions
// ("무엇이 보이나요?") get nothing: there is no answer to guess.
const YES_NO_ENDING = /(까요|습니까|ㄹ래요|할래요|볼래요|원하시나요|원하세요|필요하신가요|괜찮으신가요|맞나요|될까요)\s*\?\s*$/;

function withAnswerChips(item: AssistantChatItem): AssistantChatItem {
  if (item.pending || item.suggestions.length) return item;
  const lastText = [...item.segments].reverse().find((s) => s.type === "text");
  if (!lastText || lastText.type !== "text" || !YES_NO_ENDING.test(lastText.text.trim())) return item;
  return { ...item, suggestions: ["네", "아니요"] };
}

export default function AssistantPanel({
  chat,
  prefill,
  composerFocus,
  headerActions,
  queueSlot,
  onQueue,
  busyPlaceholder,
  draftKey,
  onQuote,
  onPrefill,
  onAttach,
  attachedRefs,
  attachments,
  onRemoveAttachment,
  onOpenView,
  ...shell
}: {
  chat: MerchantChat<JobSpec>;
  prefill: Prefill | null;
  composerFocus: number;
  /** The conversation controls, shown in the panel header. */
  headerActions?: ReactNode;
  /** The visible conversation's queued sends, shown above the attachments. */
  queueSlot?: ReactNode;
  onQueue?: (text: string) => void;
  busyPlaceholder?: string;
  draftKey?: string | null;
  onQuote?: (text: string) => void;
  onPrefill: (text: string) => void;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  attachments: AttachedItem[];
  onRemoveAttachment: (order: number) => void;
  /** 접힌 승인 안내가 운영자를 보낼 화면(Jobs / Rules). */
  onOpenView: (view: PortalViewId) => void;
  newMemoryCount: number;
  onOpenActivity: () => void;
  onClose: () => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
}) {
  // Display only: the model still receives `chat`'s exact `formatFormAnswers` text via chat.send;
  // this only reshapes the bubble the transcript shows for it. `collapseApprovals` is the same
  // kind of transform: 승인 카드 묶음을 안내 하나로 바꾸되, 턴의 기록 자체는 그대로 둔다.
  const shown = useMemo(
    () => ({
      ...chat,
      items: chat.items.map((item) =>
        item.kind === "user"
          ? { ...item, text: humanizeFormAnswers(item.text) ?? item.text }
          : withAnswerChips(collapseApprovals(item)),
      ),
    }),
    [chat],
  );
  // /changes/ 가 아니라 /rules/{id}/{action}으로 나간다 (rule_action은 job_action의 미러지 같은
  // 경로가 아니다) — web-shared의 useMerchantChat.actOnChange가 {ok, change}에서 change를 꺼내는
  // 것과 같은 방식으로 여기서도 꺼낸다.
  const onRuleAction = useCallback(async (ruleId: string, action: RuleAction): Promise<ValidationRule | null> => {
    const data = await actOnRule(ruleId, action);
    return data?.change ?? null;
  }, []);
  // /changes/ 도 /rules/ 도 아니라 /format-batches/{id}/{action}으로 나간다 (format_batch_action은
  // rule_action의 미러지 같은 경로가 아니다) — onRuleAction과 같은 방식으로 {ok, change}에서
  // change를 꺼낸다.
  const onFormatBatchAction = useCallback(async (batchId: string, action: FormatBatchAction): Promise<FormatBatch | null> => {
    const data = await actOnFormatBatch(batchId, action);
    return data?.change ?? null;
  }, []);
  // /changes/ 도 /rules/ 도 /format-batches/ 도 아니라 /profiles/{id}/{action}으로 나간다
  // (profile_action은 rule_action/format_batch_action의 미러지 같은 경로가 아니다) — 같은 방식으로
  // {ok, change}에서 change를 꺼낸다.
  const onProfileAction = useCallback(async (profileId: string, action: ProfileAction): Promise<ComparisonProfile | null> => {
    const data = await actOnProfile(profileId, action);
    return data?.change ?? null;
  }, []);
  return (
    <PanelShell
      chat={shown}
      copy={COPY}
      prefill={prefill}
      focusKey={composerFocus}
      headerActions={headerActions}
      onQueue={onQueue}
      busyPlaceholder={busyPlaceholder}
      draftKey={draftKey}
      onQuote={onQuote}
      aboveComposer={
        <div className="flex flex-col gap-2">
          {queueSlot}
          <Attachments items={attachments} onRemove={onRemoveAttachment} />
        </div>
      }
      renderBlock={(segment) => (
        <GenerativeBlock
          block={segment.block}
          status={segment.status}
          onChangeAction={chat.actOnChange}
          onRuleAction={onRuleAction}
          onFormatBatchAction={onFormatBatchAction}
          onProfileAction={onProfileAction}
          onPrefill={onPrefill}
          onSend={(text) => void chat.send(text)}
          onAttach={onAttach}
          onOpenView={onOpenView}
          attachedRefs={attachedRefs}
        />
      )}
      {...shell}
    />
  );
}
