// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** One entry per aTworks presentation tool. */

import { type ChangeAction, type GenerativeBlockProps, UnknownBlock } from "web-shared";
import { APPROVAL_OVERFLOW, type ApprovalOverflowPayload } from "@/lib/approvals";
import type { FormatBatchAction } from "@/lib/useFormatBatchActions";
import type { ProfileAction } from "@/lib/useProfileActions";
import type { RuleAction } from "@/lib/useRuleActions";
import type {
  AttachedItem,
  ComparisonProfile,
  FormatBatch,
  FormatBatchPayload,
  JobPreviewPayload,
  JobSpec,
  ParitySummaryPayload,
  PortalViewId,
  ProfilePreviewPayload,
  QueryTablePayload,
  QuestionFormPayload,
  RulePreviewPayload,
  RunDigestPayload,
  RunGroupsPayload,
  ValidationRule,
} from "@/lib/types";
import ApprovalOverflowCard from "./ApprovalOverflowCard";
import FormatBatchCard from "./FormatBatchCard";
import JobPreviewCard from "./JobPreviewCard";
import ParitySummaryCard from "./ParitySummaryCard";
import ProfilePreviewCard from "./ProfilePreviewCard";
import QueryTableCard from "./QueryTableCard";
import QuestionFormCard from "./QuestionFormCard";
import RulePreviewCard from "./RulePreviewCard";
import RunDigestCard from "./RunDigestCard";
import RunGroupsCard from "./RunGroupsCard";

export default function GenerativeBlock({
  block,
  status,
  onChangeAction,
  onRuleAction,
  onFormatBatchAction,
  onProfileAction,
  onPrefill,
  onSend,
  onAttach,
  onOpenView,
  attachedRefs,
}: GenerativeBlockProps & {
  onChangeAction?: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  /** /changes/ 가 아니라 /rules/{id}/{action}으로 나간다 — onChangeAction과는 별개 경로. */
  onRuleAction?: (id: string, action: RuleAction) => Promise<ValidationRule | null>;
  /** /changes/ 도 /rules/ 도 아니라 /format-batches/{id}/{action}으로 나간다. */
  onFormatBatchAction?: (id: string, action: FormatBatchAction) => Promise<FormatBatch | null>;
  /** /changes/ 도 /rules/ 도 /format-batches/ 도 아니라 /profiles/{id}/{action}으로 나간다. */
  onProfileAction?: (id: string, action: ProfileAction) => Promise<ComparisonProfile | null>;
  onPrefill?: (text: string) => void;
  onSend?: (text: string) => void;
  onAttach?: (item: Omit<AttachedItem, "order">) => void;
  /** 승인 카드가 상한을 넘어 접혔을 때, 안내 블록이 운영자를 보낼 화면. */
  onOpenView?: (view: PortalViewId) => void;
  attachedRefs: ReadonlySet<string>;
}) {
  switch (block.component) {
    case "run_digest":
      return <RunDigestCard payload={block.payload as RunDigestPayload} onPrefill={onPrefill} onAttach={onAttach} attachedRefs={attachedRefs} />;
    case "run_groups":
      return <RunGroupsCard payload={block.payload as RunGroupsPayload} onPrefill={onPrefill} onAttach={onAttach} attachedRefs={attachedRefs} />;
    case "query_table":
      return <QueryTableCard payload={block.payload as QueryTablePayload} onPrefill={onPrefill} onAttach={onAttach} attachedRefs={attachedRefs} />;
    case "job_preview":
      return <JobPreviewCard payload={block.payload as JobPreviewPayload} onAct={onChangeAction} />;
    case "rule_preview":
      return <RulePreviewCard payload={block.payload as RulePreviewPayload} onRuleAct={onRuleAction} />;
    case "format_batch":
      return <FormatBatchCard payload={block.payload as FormatBatchPayload} onFormatBatchAct={onFormatBatchAction} />;
    case "parity_summary":
      return <ParitySummaryCard payload={block.payload as ParitySummaryPayload} />;
    case "profile_preview":
      return <ProfilePreviewCard payload={block.payload as ProfilePreviewPayload} onProfileAct={onProfileAction} />;
    // 카드가 아니라 카드 묶음을 대신하는 안내 — collapseApprovals가 넣는다(모델이 내지 않는다).
    case APPROVAL_OVERFLOW:
      return <ApprovalOverflowCard payload={block.payload as ApprovalOverflowPayload} onOpenView={onOpenView} />;
    case "question_form":
      return <QuestionFormCard payload={block.payload as QuestionFormPayload} onSubmit={onSend} />;
    // Screen directives are intercepted in useMerchantChat and executed by the portal, never rendered as a card.
    case "screen_navigate":
    case "screen_highlight":
      return null;
    default:
      return status === "final" ? <UnknownBlock component={block.component} /> : null;
  }
}
