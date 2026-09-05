// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { ApproveBar, ChangeStatusPill, formatDate, GenCard, GenCardHeader } from "web-shared";
import { type ProfileAction, useProfileActions } from "@/lib/useProfileActions";
import type { ComparisonProfile, ProfilePreviewPayload } from "@/lib/types";

function PathChips({ paths }: { paths: string[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {paths.map((path) => (
        <span key={path} className="rounded-full bg-(--well) px-2 py-0.5 font-mono text-[11.5px] text-(--ink)">
          {path}
        </span>
      ))}
    </div>
  );
}

export default function ProfilePreviewCard({
  payload,
  onProfileAct,
}: {
  payload: ProfilePreviewPayload;
  onProfileAct?: (id: string, action: ProfileAction) => Promise<ComparisonProfile | null>;
}) {
  // FormatBatchCard/RulePreviewCard와 같은 패턴 — 후속 change_update가 payload.change에 얹히면 그게 최신.
  const { profile, busy, error, act, canAct } = useProfileActions(payload.change ?? payload.profile, onProfileAct);
  const perApiEntries = Object.entries(profile.per_api_ignore ?? {});

  return (
    <GenCard>
      <GenCardHeader
        title={payload.headline ?? profile.summary}
        meta={
          <>
            <ChangeStatusPill status={profile.status} />
            <span aria-hidden>·</span>
            <span>{formatDate(profile.created_at)}</span>
          </>
        }
      />
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] text-(--ink-soft)">{payload.note}</p> : null}
      <p className="mx-3.5 mt-2 text-[12.5px] text-(--ink-soft)">job {profile.job_id}</p>
      {profile.ignore_paths.length > 0 ? (
        <div className="mx-3.5 mt-2">
          <p className="text-[11.5px] font-medium text-(--ink-soft)">전체 무시 경로</p>
          <div className="mt-1">
            <PathChips paths={profile.ignore_paths} />
          </div>
        </div>
      ) : null}
      {perApiEntries.length > 0 ? (
        <div className="mx-3.5 mt-2 flex flex-col gap-2">
          {perApiEntries.map(([apiId, paths]) => (
            <div key={apiId}>
              <p className="text-[11.5px] font-medium text-(--ink-soft)">{apiId}</p>
              <div className="mt-1">
                <PathChips paths={paths} />
              </div>
            </div>
          ))}
        </div>
      ) : null}
      <ApproveBar change={profile} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </GenCard>
  );
}
