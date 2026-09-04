// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/**
 * Display-only humanizer for the `[form answers — <id>]` user message that
 * `formatFormAnswers` (QuestionFormCard.tsx) builds — the model needs that exact text, so this
 * never touches what is sent; it only reshapes what the transcript bubble shows.
 */

export const FORM_ANSWERS_PREFIX = "[form answers — ";

/** Strips a trailing ` [value: x]` annotation `formatFormAnswers`'s `display()` adds. */
function stripValueSuffix(display: string): string {
  return display.replace(/ \[value: [^\]]*\]$/, "");
}

/**
 * Parses `formatFormAnswers` output back into a short human summary, e.g.
 * `"폼 응답 (job-slots)\n테스트할 API 선택: api-004: 결제 승인, api-001: 계약 생성"`.
 * Returns null when `text` is not shaped like a form-answers message.
 */
export function humanizeFormAnswers(text: string): string | null {
  if (!text.startsWith(FORM_ANSWERS_PREFIX)) return null;
  const lines = text.split("\n");
  const header = lines[0];
  const id = header.slice(FORM_ANSWERS_PREFIX.length, header.endsWith("]") ? -1 : undefined);
  const out = [`폼 응답 (${id})`];
  for (const line of lines.slice(1)) {
    const m = /^- (.+?): (.+)$/.exec(line);
    if (!m) continue;
    const [, label, rawValue] = m;
    const value = rawValue === "(skipped)" ? "(건너뜀)" : rawValue.split(", ").map(stripValueSuffix).join(", ");
    out.push(`${label}: ${value}`);
  }
  return out.join("\n");
}
