// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useState } from "react";
import { Button, GenCard, GenCardHeader } from "web-shared";
import type { FormQuestion, QuestionFormPayload } from "@/lib/types";

/** `.chip-on`/`.btn-primary` are storefront-only classes (see web-shared/base.css); this merchant
 * portal marks a picked chip and the submit button with tokens `.chip`/`Button` already carry. */
const CHIP_ON = "border-(--accent) bg-(--accent-soft) text-(--accent-ink)";

function initial(qs: FormQuestion[]): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const q of qs) out[q.id] = q.default ?? (q.type === "checkbox" ? [] : "");
  return out;
}

function display(q: FormQuestion, v: string): string {
  const m = q.options?.find((o) => o.value === v || o.label === v);
  if (!m) return v;
  return m.label === m.value ? m.label : `${m.label} [value: ${m.value}]`;
}

/** open-design formatFormAnswers 미러. atworks_agent.question_form.format_form_answers와 같은 줄 모양. */
export function formatFormAnswers(form: QuestionFormPayload, answers: Record<string, string | string[]>): string {
  const lines = [`[form answers — ${form.id}]`];
  for (const q of form.questions) {
    const v = answers[q.id];
    const shown = Array.isArray(v) ? (v.length ? v.map((x) => display(q, x)).join(", ") : "(skipped)") : v && v.trim() ? display(q, v.trim()) : "(skipped)";
    lines.push(`- ${q.label}: ${shown}`);
  }
  return lines.join("\n");
}

export default function QuestionFormCard({ payload, onSubmit }: { payload: QuestionFormPayload; onSubmit?: (text: string) => void }) {
  const [answers, setAnswers] = useState(() => initial(payload.questions));
  const [sent, setSent] = useState(false);
  const set = (id: string, v: string | string[]) => setAnswers((a) => ({ ...a, [id]: v }));
  return (
    <GenCard>
      <GenCardHeader title={payload.title} meta={payload.description ? <span>{payload.description}</span> : null} />
      <div className="px-3.5 pb-3">
        {payload.questions.map((q) => (
          <fieldset key={q.id} className={`my-2 rounded-lg border p-2 ${q.highlight ? "border-(--warn) bg-(--warn-soft)" : "border-(--line)"}`} disabled={sent}>
            <legend className="text-[13px] font-medium">{q.label}</legend>
            <p className="mb-1 text-[11.5px] text-(--ink-soft)">왜 묻나: {q.why}</p>
            {q.type === "radio" || q.type === "select" ? (
              <div className="flex flex-wrap gap-1">
                {(q.options ?? []).map((o) => (
                  <label key={o.value} className={`chip ${answers[q.id] === o.value ? CHIP_ON : ""}`}>
                    <input type="radio" name={q.id} className="sr-only" checked={answers[q.id] === o.value} onChange={() => set(q.id, o.value)} />
                    {o.label}
                  </label>
                ))}
              </div>
            ) : q.type === "checkbox" ? (
              <div className="flex flex-wrap gap-1">
                {(q.options ?? []).map((o) => {
                  const cur = (answers[q.id] as string[]) ?? [];
                  const on = cur.includes(o.value);
                  return (
                    <label key={o.value} className={`chip ${on ? CHIP_ON : ""}`}>
                      <input type="checkbox" className="sr-only" checked={on} onChange={() => set(q.id, on ? cur.filter((x) => x !== o.value) : [...cur, o.value])} />
                      {o.label}
                    </label>
                  );
                })}
              </div>
            ) : q.type === "switch" ? (
              <label className="chip">
                <input type="checkbox" checked={answers[q.id] === "true"} onChange={(e) => set(q.id, e.target.checked ? "true" : "false")} /> {answers[q.id] === "true" ? "예" : "아니오"}
              </label>
            ) : (
              <input
                className="w-full rounded border border-(--line) px-2 py-1 text-[13px]"
                type={q.type === "number" ? "number" : q.type === "date" ? "date" : q.type === "time" ? "time" : "text"}
                value={answers[q.id] as string}
                placeholder={q.placeholder}
                onChange={(e) => set(q.id, e.target.value)}
              />
            )}
          </fieldset>
        ))}
        <Button
          variant="primary"
          className="mt-1"
          disabled={sent || !onSubmit}
          onClick={() => {
            setSent(true);
            onSubmit?.(formatFormAnswers(payload, answers));
          }}
        >
          {sent ? "보냈습니다" : "이대로 보내기"}
        </Button>
      </div>
    </GenCard>
  );
}
