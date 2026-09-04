// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useMemo } from "react";
import { AssistantPanel as PanelShell, type MerchantChat, type Prefill } from "web-shared";
import { humanizeFormAnswers } from "@/lib/formAnswers";
import type { AttachedItem, JobSpec } from "@/lib/types";
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

export default function AssistantPanel({
  chat,
  prefill,
  onPrefill,
  onAttach,
  ...shell
}: {
  chat: MerchantChat<JobSpec>;
  prefill: Prefill | null;
  onPrefill: (text: string) => void;
  onAttach: (item: Omit<AttachedItem, "order">) => void;
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
  return (
    <PanelShell
      chat={shown}
      copy={COPY}
      prefill={prefill}
      renderBlock={(segment) => (
        <GenerativeBlock
          block={segment.block}
          status={segment.status}
          onChangeAction={chat.actOnChange}
          onPrefill={onPrefill}
          onSend={(text) => void chat.send(text)}
          onAttach={onAttach}
        />
      )}
      {...shell}
    />
  );
}
