// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** One entry per aTworks presentation tool. */

import { type ChangeAction, type GenerativeBlockProps, UnknownBlock } from "web-shared";
import type { RuleAction } from "@/lib/useRuleActions";
import type { AttachedItem, JobPreviewPayload, JobSpec, QuestionFormPayload, RulePreviewPayload, RunDigestPayload, RunGroupsPayload, ValidationRule } from "@/lib/types";
import JobPreviewCard from "./JobPreviewCard";
import QuestionFormCard from "./QuestionFormCard";
import RulePreviewCard from "./RulePreviewCard";
import RunDigestCard from "./RunDigestCard";
import RunGroupsCard from "./RunGroupsCard";

export default function GenerativeBlock({
  block,
  status,
  onChangeAction,
  onRuleAction,
  onPrefill,
  onSend,
  onAttach,
}: GenerativeBlockProps & {
  onChangeAction?: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  /** /changes/ 가 아니라 /rules/{id}/{action}으로 나간다 — onChangeAction과는 별개 경로. */
  onRuleAction?: (id: string, action: RuleAction) => Promise<ValidationRule | null>;
  onPrefill?: (text: string) => void;
  onSend?: (text: string) => void;
  onAttach?: (item: Omit<AttachedItem, "order">) => void;
}) {
  switch (block.component) {
    case "run_digest":
      return <RunDigestCard payload={block.payload as RunDigestPayload} onPrefill={onPrefill} onAttach={onAttach} />;
    case "run_groups":
      return <RunGroupsCard payload={block.payload as RunGroupsPayload} onPrefill={onPrefill} onAttach={onAttach} />;
    case "job_preview":
      return <JobPreviewCard payload={block.payload as JobPreviewPayload} onAct={onChangeAction} />;
    case "rule_preview":
      return <RulePreviewCard payload={block.payload as RulePreviewPayload} onRuleAct={onRuleAction} />;
    case "question_form":
      return <QuestionFormCard payload={block.payload as QuestionFormPayload} onSubmit={onSend} />;
    default:
      return status === "final" ? <UnknownBlock component={block.component} /> : null;
  }
}
