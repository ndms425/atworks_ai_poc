"""리포트 = 템플릿 1회 + data.json. open-design Live Artifact 계약: template.html·data.json·
provenance.generator. 스케줄러가 data.json만 갱신하고 index.html을 재렌더한다. LLM 0회."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from atworks_agent import JobSpec, RunResult

TEMPLATE = Path(__file__).with_name("report_template.html")

# job_id 모양만 통과시킨다: 세그먼트 구분자(``/``, ``\``)도, ``..``도 이 안엔 들어갈 수 없다.
# 이 라우트는 세션이 없다(R40) — job_id를 파일시스템 경로에 그대로 잇는 유일한 방어선이다.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _matrix(job: JobSpec, runs: list[RunResult]) -> dict:
    """The api × data grid, one column per environment, built from the LATEST run per
    (api_id, test_data_label, env). A row `differs` when its cells do not agree — that, and
    the count of such rows, is the whole comparison the operator asked for. No model output
    reaches this: every status here is a run record's own verdict."""
    envs = list(job.target_envs)
    latest: dict[tuple[str, str | None, str], RunResult] = {}
    order: list[tuple[str, str | None]] = []
    for run in runs:
        key = (run.api_id, run.test_data_label, run.target_env)
        current = latest.get(key)
        if current is None or run.executed_at >= current.executed_at:
            latest[key] = run
        if (run.api_id, run.test_data_label) not in order:
            order.append((run.api_id, run.test_data_label))
    rows: list[dict] = []
    for api_id, label in sorted(order, key=lambda pair: (pair[0], pair[1] or "")):
        cells = {
            env: {"status": latest[(api_id, label, env)].status.value,
                  "run_id": latest[(api_id, label, env)].run_id}
            for env in envs if (api_id, label, env) in latest
        }
        rows.append({
            "api_id": api_id,
            "test_data_label": label,
            "cells": cells,
            "differs": len({c["status"] for c in cells.values()}) > 1,
        })
    return {"envs": envs, "rows": rows, "differs_count": sum(1 for r in rows if r["differs"])}


class Reports:
    def __init__(self, out_dir: Path, portal_origin: str = "http://localhost:3110"):
        self.out_dir = out_dir
        self.portal_origin = portal_origin
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _folder(self, job_id: str) -> Path:
        if not SAFE_ID.fullmatch(job_id):
            raise ValueError(f"unsafe report id: {job_id!r}")
        base = self.out_dir.resolve()
        folder = (self.out_dir / job_id).resolve()
        if not folder.is_relative_to(base):
            raise ValueError(f"report id escapes the reports directory: {job_id!r}")
        return folder

    def write(self, job: JobSpec, runs: list[RunResult], *, generator: str = "refresh_runner") -> Path:
        folder = self._folder(job.job_id)
        folder.mkdir(parents=True, exist_ok=True)
        counts: dict = {"total": len(runs), "pass": 0, "fail": 0, "error": 0}
        by_env: dict[str, dict[str, int]] = {
            env: {"total": 0, "pass": 0, "fail": 0, "error": 0} for env in job.target_envs
        }
        for r in runs:
            counts[r.status.value] += 1
            bucket = by_env.setdefault(r.target_env, {"total": 0, "pass": 0, "fail": 0, "error": 0})
            bucket["total"] += 1
            bucket[r.status.value] += 1
        counts["by_env"] = by_env
        data = {
            "job": job.model_dump(mode="json", exclude_none=True),
            "summary": counts,
            "matrix": _matrix(job, runs),
            "runs": [r.model_dump(mode="json", exclude_none=True) for r in runs],
            "provenance": {"generator": generator, "generated_at": datetime.now(UTC).isoformat()},
            "portal_origin": self.portal_origin,
        }
        (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # A model-authored summary or test-data value can carry `<!--<script` — inside the
        # data script tag that sequence walks the HTML tokenizer into script-data-double-escape,
        # where a lone `</script>` does not close the block, and everything to EOF is
        # swallowed (confirmed against the tokenizer spec; JSON.parse then never runs and the
        # report renders as an empty skeleton). Escaping every `<` to its JSON unicode escape
        # removes the character from the markup entirely — `</script>` handling comes free —
        # while leaving the JSON valid; JSON.parse decodes `<` back to `<` in the template.
        embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        html = TEMPLATE.read_text(encoding="utf-8").replace("__REPORT_DATA__", embedded)
        (folder / "index.html").write_text(html, encoding="utf-8")
        return folder / "index.html"

    def read_html(self, job_id: str) -> str | None:
        path = self._folder(job_id) / "index.html"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def all_runs(self, job_id: str) -> list[dict]:
        path = self._folder(job_id) / "data.json"
        return json.loads(path.read_text(encoding="utf-8"))["runs"] if path.exists() else []
