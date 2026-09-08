# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""👍 → 회귀 eval 케이스 한 장(자가발전 spec §9).

한 사람이 "이 답 좋다"를 누른 턴은, 그 턴이 실제로 실행한 ``QuerySpec``과 함께, 다음 변경이
깨뜨리면 안 되는 **행동**이 된다. 이 모듈이 그 턴을 commerce-evals 케이스 스키마의 JSON 한 장으로
옮긴다 -- 모델은 이 경로에 없다: 저장된 스펙에서 차원·측정값·필터 **종류**만 뽑고, 질문은 이미
마스킹된 요약을 그대로 쓴다.

케이스가 고정하는 것은 값이 아니라 모양이다. ``spec_equals``에 필터의 **값**(30일이었는지 7일이
었는지, 어떤 API id였는지)은 들어가지 않는다: 오늘 실패가 많은 API가 다음 달에도 그럴 이유가 없고,
그런 값을 고정한 케이스는 제품이 아니라 데이터가 바뀔 때마다 붉어진다. 남는 것은 "이 질문에는
경로 2조각별로, 실패·에러 수를, 상태와 기간 필터로 묶어 답한다"는 **판단**이다.

파일은 turn_id 하나당 한 장이다(같은 턴에 두 번 👍를 눌러도 같은 파일이 다시 쓰인다) -- 재투표가
케이스를 불리면 스위트가 한 사람의 클릭 수만큼 커진다.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from atworks_agent import AskEntry

#: 이 리포지토리의 기본 케이스 디렉터리. `ATWORKS_EVALS_DIR`가 있으면 그것이 이긴다 (배포에서는
#: 리포지토리 밖의 쓰기 가능한 경로를 가리킨다 -- 생성된 케이스는 코드가 아니라 데이터다).
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 케이스가 절대 부르면 안 되는 도구. 질문에 답하는 턴이 실행 계획을 스테이징하거나 승인하면
#: 그건 다른 흐름이다 -- 👍가 그 경계를 넘겨 학습되지 않도록 모든 자동 케이스가 이 줄을 갖는다.
NEVER_CALLS: tuple[str, ...] = ("stage_job", "apply_job")

#: 한 턴이 답 하나에 쓰는 도구의 상한(스킬 로드 + query_runs + 카드 + 여유 1). 넘으면 모델이
#: 헤맸다는 뜻이고, 그건 👍를 받았더라도 고정할 행동이 아니다.
MAX_TOOL_CALLS = 4

_SEQ_RE = re.compile(r"^(\d{8})-(\d+)\.json$")


def default_cases_dir() -> Path:
    """``ATWORKS_EVALS_DIR`` 또는 ``<repo>/evals/cases``."""
    override = os.environ.get("ATWORKS_EVALS_DIR")
    return Path(override) if override else _REPO_ROOT / "evals" / "cases"


def filter_kinds(entry_spec: Any) -> list[str]:
    """스펙이 **실제로 설정한** 필터 필드 이름들, 정렬해서. 값은 버린다(위 모듈 설명)."""
    return sorted(entry_spec.filters.model_dump(exclude_none=True))


def build_case(entry: AskEntry, *, case_id: str) -> dict[str, Any]:
    """``AskEntry`` → commerce-evals 케이스 딕트(spec §9의 모양 그대로)."""
    if entry.spec is None:   # pragma: no cover - 호출자가 이미 거른다
        raise ValueError("an eval case needs the turn's QuerySpec")
    return {
        "id": case_id,
        "priority": "P2",
        # 태그는 "무엇을 고정하는가"의 색인이다: 도구 하나 + 이 답이 선 차원들.
        "tags": ["query_runs", *entry.spec.dimensions],
        "skip": False,
        # 상태는 사람의 신원이 아니라 **역할 맥락**이다: 같은 질문이 developer와 pm에게 다르게
        # 읽힐 수 있고, 케이스는 그 맥락에서 재생돼야 한다.
        "state": {"operator": entry.operator, "role": entry.role},
        # 이미 마스킹된 요약 그대로. 여기서 한 번 더 마스킹하면 규칙이 바뀐 뒤 같은 케이스가
        # 날마다 다르게 읽힌다(ask_record와 같은 이유).
        "turns": [entry.question],
        "expected": {
            "calls_tool": "query_runs",
            "spec_equals": {
                "dimensions": list(entry.spec.dimensions),
                "measures": list(entry.spec.measures),
                "filters_kinds": filter_kinds(entry.spec),
            },
            "ui_components": ["query_table"],
            "never_calls": list(NEVER_CALLS),
            "max_tool_calls": MAX_TOOL_CALLS,
        },
        "notes": f"auto-generated from 👍 on turn {entry.turn_id}",
    }


def _existing_path(cases_dir: Path, day: str, turn_id: str) -> Path | None:
    """이 turn_id로 이미 쓰인 파일. ``notes``가 turn_id를 담고 있으므로 별도 색인이 필요 없다 --
    하루치 파일 몇 장을 읽는 값이고, 색인 파일을 따로 두면 그 둘이 어긋나는 상태가 생긴다."""
    for path in sorted(cases_dir.glob(f"{day}-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("notes", "")).endswith(turn_id):
            return path
    return None


def _next_seq(cases_dir: Path, day: str) -> int:
    used = [int(m.group(2)) for p in cases_dir.glob(f"{day}-*.json")
            if (m := _SEQ_RE.match(p.name)) is not None]
    return max(used, default=0) + 1


def write_case(entry: AskEntry, *, cases_dir: Path | None = None) -> Path:
    """``evals/cases/<YYYYMMDD>-<seq>.json`` 한 장을 쓰고 그 경로를 돌려준다.

    turn_id에 대해 멱등하다: 같은 턴을 다시 👍하면 같은 파일이 다시 쓰인다(새 seq를 쓰지 않는다).
    LF로 쓴다 -- 리포지토리 전체 규칙이고, 시드 케이스는 커밋된다."""
    if entry.spec is None:
        raise ValueError("an eval case needs the turn's QuerySpec")
    directory = cases_dir if cases_dir is not None else default_cases_dir()
    directory.mkdir(parents=True, exist_ok=True)
    day = entry.at.strftime("%Y%m%d")
    path = _existing_path(directory, day, entry.turn_id)
    if path is None:
        path = directory / f"{day}-{_next_seq(directory, day):03d}.json"
    case = build_case(entry, case_id=f"query_runs-{path.stem}")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(case, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path
