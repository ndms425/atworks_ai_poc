// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

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

  if (operators.length === 0) return null;

  return (
    <select
      aria-label="운영자 선택"
      value={value}
      onChange={(event) => {
        const operatorId = event.target.value;
        storeOperatorId(operatorId);
        onChange(operatorId);
      }}
      className="w-full min-w-0 truncate rounded-lg border border-(--line-strong) bg-(--card) px-2 py-1 text-[12px] font-medium text-(--ink) outline-none focus-visible:border-(--accent)"
    >
      {operators.map((operator) => (
        <option key={operator.operator_id} value={operator.operator_id}>
          {operator.name} ({ROLE_KO[operator.role]})
        </option>
      ))}
    </select>
  );
}
