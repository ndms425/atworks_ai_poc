// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { formatDate, Notice, Panel, Pill, Skeleton, useResource } from "web-shared";
import { fetchProfiles } from "@/lib/api";
import type { ComparisonProfile } from "@/lib/types";

const STATUS_TONE = {
  staged: "violet",
  applied: "ok",
  discarded: "muted",
} as const;

const STATUS_LABEL: Record<ComparisonProfile["status"], string> = {
  staged: "승인 대기",
  applied: "적용됨",
  discarded: "기각됨",
};

function ProfileRow({ profile }: { profile: ComparisonProfile }) {
  const perApiEntries = Object.entries(profile.per_api_ignore ?? {});
  return (
    <li className="px-[18px] py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium leading-snug text-(--ink)">{profile.summary}</div>
          <div className="mt-0.5 text-[12px] tabular-nums text-(--ink-soft)">
            job {profile.job_id} · {formatDate(profile.effective_from ?? profile.applied_at ?? profile.created_at)}
            {profile.applied_by ? ` · ${profile.applied_by}` : ""}
          </div>
        </div>
        <Pill tone={STATUS_TONE[profile.status]} dot>
          {STATUS_LABEL[profile.status]}
        </Pill>
      </div>
      {profile.ignore_paths.length > 0 || perApiEntries.length > 0 ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {profile.ignore_paths.map((path) => (
            <span key={path} className="rounded-full bg-(--well) px-2 py-0.5 font-mono text-[11.5px] text-(--ink)">
              {path}
            </span>
          ))}
          {perApiEntries.map(([apiId, paths]) =>
            paths.map((path) => (
              <span key={`${apiId}-${path}`} className="rounded-full bg-(--well) px-2 py-0.5 font-mono text-[11.5px] text-(--ink)">
                {apiId}: {path}
              </span>
            )),
          )}
        </div>
      ) : null}
    </li>
  );
}

/** FormatsView의 미러 — 읽기 전용 참고 목록. /profiles를 읽으며, 승인/기각 액션은 카드(ProfilePreviewCard)에서
 * useProfileActions를 통해서만 나간다 (여기서는 목록만 보여준다). */
export default function ProfilesView({ refreshKey }: { refreshKey: number }) {
  const { data, failed } = useResource(fetchProfiles, [refreshKey]);
  const profiles = data?.profiles ?? [];

  if (failed && !data) {
    return (
      <Panel title="비교 프로파일">
        <div className="px-[18px] py-3">
          <Notice>The aTworks AI host isn&apos;t reachable, so comparison profiles can&apos;t load.</Notice>
        </div>
      </Panel>
    );
  }
  if (!data) {
    return (
      <Panel title="비교 프로파일">
        <div className="p-[18px]">
          <Skeleton className="h-24" />
        </div>
      </Panel>
    );
  }
  if (profiles.length === 0) {
    return (
      <Panel title="비교 프로파일">
        <div className="px-[18px] py-3">
          <Notice>등록된 비교 프로파일이 없습니다.</Notice>
        </div>
      </Panel>
    );
  }
  return (
    <Panel title="비교 프로파일" subtitle={`${profiles.length}개`}>
      <ul className="divide-y divide-(--line)">
        {profiles.map((profile) => (
          <ProfileRow key={profile.profile_id} profile={profile} />
        ))}
      </ul>
    </Panel>
  );
}
