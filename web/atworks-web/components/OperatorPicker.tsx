// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect } from "react";
import { useResource } from "web-shared";
import { fetchOperators } from "@/lib/api";
import { ROLE_KO } from "@/lib/types";

export const OPERATOR_STORAGE_KEY = "atworks-operator";

export function readStoredOperatorId(): string | null {
  try {
    return localStorage.getItem(OPERATOR_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeOperatorId(operatorId: string) {
  try {
    localStorage.setItem(OPERATOR_STORAGE_KEY, operatorId);
  } catch {
    // Storage may be unavailable (private mode, quota); the picker still works in-session.
  }
}

/** A compact demo "login": which operator's session and AI insights are shown. */
export default function OperatorPicker({ value, onChange }: { value: string; onChange: (operatorId: string) => void }) {
  const { data } = useResource(fetchOperators, []);
  const operators = data?.operators ?? [];
  // If the stored/current id isn't among the loaded options (a stale localStorage value, or a
  // race before page.tsx's own recovery kicks in), show the first option instead of a blank
  // <select> -- and tell the caller, so its state doesn't keep pointing at a nonexistent id.
  const known = operators.some((o) => o.operator_id === value);
  const selected = !known && operators.length > 0 ? operators[0].operator_id : value;

  useEffect(() => {
    if (selected !== value) onChange(selected);
  }, [selected, value, onChange]);

  if (operators.length === 0) return null;

  return (
    <select
      aria-label="운영자 선택"
      value={selected}
      onChange={(event) => {
        const operatorId = event.target.value;
        storeOperatorId(operatorId);
        onChange(operatorId);
      }}
      className="w-full max-w-full min-w-0 truncate rounded-lg border border-(--line-strong) bg-(--card) px-2 py-1 text-[12px] font-medium text-(--ink) outline-none focus-visible:border-(--accent)"
    >
      {operators.map((operator) => (
        <option key={operator.operator_id} value={operator.operator_id}>
          {operator.name} ({ROLE_KO[operator.role]})
        </option>
      ))}
    </select>
  );
}
