// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** One entry per aTworks presentation tool. */

import { type ChangeAction, type GenerativeBlockProps, UnknownBlock } from "web-shared";
import type { AttachedItem, JobPreviewPayload, JobSpec, QuestionFormPayload, RunDigestPayload } from "@/lib/types";
import JobPreviewCard from "./JobPreviewCard";
import QuestionFormCard from "./QuestionFormCard";
import RunDigestCard from "./RunDigestCard";

export default function GenerativeBlock({
  block,
  status,
  onChangeAction,
  onPrefill,
  onSend,
  onAttach,
}: GenerativeBlockProps & {
  onChangeAction?: (id: string, action: ChangeAction) => Promise<JobSpec | null>;
  onPrefill?: (text: string) => void;
  onSend?: (text: string) => void;
  onAttach?: (item: Omit<AttachedItem, "order">) => void;
}) {
  switch (block.component) {
    case "run_digest":
      return <RunDigestCard payload={block.payload as RunDigestPayload} onPrefill={onPrefill} onAttach={onAttach} />;
    case "job_preview":
      return <JobPreviewCard payload={block.payload as JobPreviewPayload} onAct={onChangeAction} />;
    case "question_form":
      return <QuestionFormCard payload={block.payload as QuestionFormPayload} onSubmit={onSend} />;
    default:
      return status === "final" ? <UnknownBlock component={block.component} /> : null;
  }
}
