"""리포트 = 템플릿 1회 + data.json. open-design Live Artifact 계약: template.html·data.json·
provenance.generator. 스케줄러가 data.json만 갱신하고 index.html을 재렌더한다. LLM 0회."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from atworks_agent import JobSpec, RunResult, cluster_diffs, compare_bodies

TEMPLATE = Path(__file__).with_name("report_template.html")

# job_id 모양만 통과시킨다: 세그먼트 구분자(``/``, ``\``)도, ``..``도 이 안엔 들어갈 수 없다.
# 이 라우트는 세션이 없다(R40) — job_id를 파일시스템 경로에 그대로 잇는 유일한 방어선이다.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _latest_selection(
    runs: list[RunResult],
) -> tuple[dict[tuple[str, str | None, str], RunResult], list[tuple[str, str | None]]]:
    """The LATEST run per (api_id, test_data_label, env), plus the (api_id, test_data_label)
    pairs seen, sorted. Shared by `_matrix` and `_parity` so both agree on which run represents
    a cell — a schedule that runs the same job repeatedly must not have the matrix and the
    parity block disagree about which execution is "current"."""
    latest: dict[tuple[str, str | None, str], RunResult] = {}
    order: list[tuple[str, str | None]] = []
    for run in runs:
        key = (run.api_id, run.test_data_label, run.target_env)
        current = latest.get(key)
        if current is None or run.executed_at >= current.executed_at:
            latest[key] = run
        if (run.api_id, run.test_data_label) not in order:
            order.append((run.api_id, run.test_data_label))
    return latest, sorted(order, key=lambda pair: (pair[0], pair[1] or ""))


def _matrix(job: JobSpec, runs: list[RunResult]) -> dict:
    """The api × data grid, one column per environment, built from the LATEST run per
    (api_id, test_data_label, env). A row `differs` when its cells do not agree — that, and
    the count of such rows, is the whole comparison the operator asked for. No model output
    reaches this: every status here is a run record's own verdict."""
    envs = list(job.target_envs)
    latest, order = _latest_selection(runs)
    rows: list[dict] = []
    for api_id, label in order:
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


def _parity(job: JobSpec, runs: list[RunResult], ignore_paths: Sequence[str] = ()) -> dict | None:
    """The value-level comparison, present only for a two-target job (`a_env`/`b_env`).
    Reuses `_latest_selection` so a row's cells are the same runs `_matrix` would show. A row
    needs a run on BOTH target envs to be judged at all; missing `response_body` on either side
    falls back to a status-only verdict (no fabricated body diff). `clusters` groups only the
    `value_diff` rows — a `status_diff`/`equal` row carries no `diff_paths` to cluster on."""
    if len(job.target_envs) != 2:
        return None
    a_env, b_env = job.target_envs
    latest, order = _latest_selection(runs)
    rows: list[dict] = []
    for api_id, label in order:
        a_run = latest.get((api_id, label, a_env))
        b_run = latest.get((api_id, label, b_env))
        if a_run is None or b_run is None:
            continue
        if a_run.response_body is None or b_run.response_body is None:
            verdict = "equal" if a_run.status == b_run.status else "status_diff"
            diff_paths: list[str] = []
        elif a_run.status != b_run.status:
            verdict = "status_diff"
            diff_paths = []
        else:
            body_diff = compare_bodies(a_run.response_body, b_run.response_body, ignore_paths)
            verdict = "equal" if body_diff.equal else "value_diff"
            diff_paths = body_diff.diff_paths
        rows.append({
            "api_id": api_id,
            "test_data_label": label,
            "verdict": verdict,
            "diff_paths": diff_paths,
            "a_run_id": a_run.run_id,
            "b_run_id": b_run.run_id,
        })
    clusters = cluster_diffs([
        {"diff_paths": r["diff_paths"], "row_key": f"{r['api_id']}|{r['test_data_label'] or ''}"}
        for r in rows if r["verdict"] == "value_diff"
    ])
    return {
        "targets": [a_env, b_env],
        "rows": rows,
        "value_diff_count": sum(1 for r in rows if r["verdict"] == "value_diff"),
        "status_diff_count": sum(1 for r in rows if r["verdict"] == "status_diff"),
        "clusters": [c.model_dump(mode="json") for c in clusters],
        "ignore_paths": list(ignore_paths),
    }


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
            "parity": _parity(job, runs),
            "runs": [r.model_dump(mode="json", exclude_none=True) for r in runs],
            "provenance": {"generator": generator, "generated_at": datetime.now(UTC).isoformat()},
            "portal_origin": self.portal_origin,
        }
        return self._write_report(folder, data)

    def rediff(self, job_id: str, ignore_paths: Sequence[str]) -> Path:
        """Recompute ONLY the parity block, from bodies already stored in `data.json` — no
        backend call, no new run. Lets an operator narrow noise (a volatile field) without
        re-hitting either server. `matrix`/`summary`/`runs` are untouched; the stored job and
        run dicts are the same shapes `write` dumped, so `model_validate` round-trips them."""
        folder = self._folder(job_id)
        data = json.loads((folder / "data.json").read_text(encoding="utf-8"))
        job = JobSpec.model_validate(data["job"])
        runs = [RunResult.model_validate(r) for r in data["runs"]]
        data["parity"] = _parity(job, runs, ignore_paths)
        data["provenance"] = {**data["provenance"], "generated_at": datetime.now(UTC).isoformat()}
        return self._write_report(folder, data)

    def _write_report(self, folder: Path, data: dict) -> Path:
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
