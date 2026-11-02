// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useMemo } from "react";
import { AssistantPanel as PanelShell, type MerchantChat, type Prefill } from "web-shared";
import { actOnFormatBatch, actOnProfile, actOnRule } from "@/lib/api";
import { humanizeFormAnswers } from "@/lib/formAnswers";
import type { FormatBatchAction } from "@/lib/useFormatBatchActions";
import type { ProfileAction } from "@/lib/useProfileActions";
import type { RuleAction } from "@/lib/useRuleActions";
import type { AttachedItem, ComparisonProfile, FormatBatch, JobSpec, ValidationRule } from "@/lib/types";
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

export default function AssistantPanel({
  chat,
  prefill,
  onPrefill,
  onAttach,
  attachedRefs,
  attachments,
  onRemoveAttachment,
  ...shell
}: {
  chat: MerchantChat<JobSpec>;
  prefill: Prefill | null;
  onPrefill: (text: string) => void;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
  attachedRefs: ReadonlySet<string>;
  attachments: AttachedItem[];
  onRemoveAttachment: (order: number) => void;
  newMemoryCount: number;
  onOpenActivity: () => void;
  onClose: () => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
}) {
  // Display only: the model still receives `chat`'s exact `formatFormAnswers` text via chat.send;
  // this only reshapes the bubble the transcript shows for it.
  const shown = useMemo(
    () => ({
      ...chat,
      items: chat.items.map((item) =>
        item.kind === "user" ? { ...item, text: humanizeFormAnswers(item.text) ?? item.text } : item,
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
      aboveComposer={<Attachments items={attachments} onRemove={onRemoveAttachment} />}
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
          attachedRefs={attachedRefs}
        />
      )}
      {...shell}
    />
  );
}
