"""리포트 = 템플릿 1회 + 증분 파일 몇 개. open-design Live Artifact 계약: template.html·data.json·
provenance.generator. 스케줄러가 회차마다 그 회차의 run만 넘기고 index.html을 재렌더한다. LLM 0회.

Task 9 (scale spec §9 "리포트") splits what used to be one growing ``data.json``:

``<job_id>/runs.jsonl``   회차마다 **append**되는 run 레코드(본문 없음). 한 job의 전체 실행 이력이
                          여기 있고, 이 파일만이 유일하게 자란다.
``<job_id>/parity.json``  값 동등성 블록 + **그 입력**(행마다 a_run_id/b_run_id/a_status/b_status).
                          ``rediff``는 이 파일만 읽고 본문을 다시 불러 재계산한다.
``<job_id>/data.json``    job/summary/matrix/parity/provenance/portal_origin/runs_total 요약.
                          **runs 배열은 없다** — 400건 × 회차가 이 파일에 쌓이던 자리를 없앤다.
``<job_id>/index.html``   data.json + 최신 200행만 임베드, 나머지는 포털 링크.

응답 본문은 이 모듈이 들고 있지 않다: parity가 필요한 그 순간에만 ``body_loader(run_id)``
(= ``backend.get_body``)로 불러온다. 본문이 없으면 판정을 지어내지 않고 상태 비교로 폴백하며,
그 이유를 노트로 적는다 — 그룹이 캡처 해제면 "본문 캡처 해제", 90일 창을 벗어났으면 "본문 만료".
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from atworks_agent import JobSpec, RunResult, cluster_diffs, compare_bodies

TEMPLATE = Path(__file__).with_name("report_template.html")

# job_id 모양만 통과시킨다: 세그먼트 구분자(``/``, ``\``)도, ``..``도 이 안엔 들어갈 수 없다.
# 이 라우트는 세션이 없다(R40) — job_id를 파일시스템 경로에 그대로 잇는 유일한 방어선이다.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

RUNS_FILE = "runs.jsonl"
PARITY_FILE = "parity.json"
DATA_FILE = "data.json"
INDEX_FILE = "index.html"

#: How many run rows index.html carries inline. The rest is one link to the portal — the report
#: page must stay a bounded artifact whatever a scheduled job's matrix × schedules add up to.
EMBEDDED_RUN_ROWS = 200

BodyLoader = Callable[[str], Awaitable[dict | None]]
CaptureDisabled = Callable[[str], bool]

#: The two reasons a parity row has no body to compare, verbatim for the report template.
NOTE_CAPTURE_OFF = "본문 캡처 해제"
NOTE_BODY_EXPIRED = "본문 만료"


def _run_row(run: RunResult) -> dict:
    """One runs.jsonl line. ``response_body`` never lands here: the report reads bodies through
    ``body_loader`` at the moment parity needs them, and a body on disk beside the summary is
    exactly the unbounded, undeletable copy retention (spec §6) exists to prevent."""
    return run.model_dump(mode="json", exclude_none=True, exclude={"response_body"})


def _row_order(row: dict) -> tuple[datetime, str]:
    """Newest-first key over runs.jsonl rows. The timestamp is PARSED, not compared as a string:
    two rows written under different UTC offsets would otherwise order by their text."""
    try:
        moment = datetime.fromisoformat(row["executed_at"]).astimezone(UTC)
    except (KeyError, ValueError):
        moment = datetime.min.replace(tzinfo=UTC)
    return moment, row.get("run_id", "")


def _latest_selection(
    runs: list[RunResult],
) -> tuple[dict[tuple[str, str | None, str], RunResult], list[tuple[str, str | None]]]:
    """The LATEST run per (api_id, test_data_label, env), plus the (api_id, test_data_label)
    pairs seen, sorted. Shared by `_matrix` and `_parity_pairs` so both agree on which run
    represents a cell — a schedule that runs the same job repeatedly must not have the matrix
    and the parity block disagree about which execution is "current"."""
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


def _parity_pairs(job: JobSpec, runs: list[RunResult]) -> list[dict] | None:
    """The parity block's INPUTS: one entry per row that has a run on BOTH target envs, naming
    the two run ids and their two statuses. This is what ``parity.json`` stores, and it is
    everything ``rediff`` needs besides the bodies themselves — so a re-diff never re-reads,
    re-orders or re-judges the run history, it only fetches two bodies per row again."""
    if len(job.target_envs) != 2:
        return None
    a_env, b_env = job.target_envs
    latest, order = _latest_selection(runs)
    pairs: list[dict] = []
    for api_id, label in order:
        a_run = latest.get((api_id, label, a_env))
        b_run = latest.get((api_id, label, b_env))
        if a_run is None or b_run is None:
            continue
        pairs.append({
            "api_id": api_id, "test_data_label": label,
            "a_run_id": a_run.run_id, "b_run_id": b_run.run_id,
            "a_status": a_run.status.value, "b_status": b_run.status.value,
        })
    return pairs


def _parity_block(
    targets: Sequence[str],
    pairs: Sequence[dict],
    bodies: dict[str, dict | None],
    ignore_paths: Sequence[str] = (),
    per_api_ignore: dict[str, list[str]] | None = None,
    capture_disabled: CaptureDisabled | None = None,
) -> dict:
    """The value-level comparison for a two-target job, over bodies ALREADY loaded. A row whose
    either side has no body falls back to a status-only verdict (never a fabricated body diff)
    and says why: the API's group opted out of capture, or the 90-day body window has passed.
    `clusters` groups only the `value_diff` rows — a `status_diff`/`equal` row carries no
    `diff_paths` to cluster on. `per_api_ignore` paths are scoped per row's own api_id — never
    merged across APIs, or a path meant for one API would silently suppress a real diff on every
    other API too."""
    per_api_ignore = per_api_ignore or {}
    rows: list[dict] = []
    for pair in pairs:
        api_id = pair["api_id"]
        a_body = bodies.get(pair["a_run_id"])
        b_body = bodies.get(pair["b_run_id"])
        same_status = pair["a_status"] == pair["b_status"]
        if a_body is None or b_body is None:
            # Task 6 (spec §7) + Task 9 (spec §6): a group opted out of body capture, or the
            # body aged out of the 90-day window. Either way the verdict falls back to status
            # only -- never a fabricated body diff -- and the note names which of the two it is.
            verdict = "equal" if same_status else "status_diff"
            diff_paths: list[str] = []
            basis = "status_only"
            note: str | None = (
                NOTE_CAPTURE_OFF if capture_disabled is not None and capture_disabled(api_id)
                else NOTE_BODY_EXPIRED
            )
        elif not same_status:
            verdict = "status_diff"
            diff_paths = []
            basis = "body"
            note = None
        else:
            row_ignore = [*ignore_paths, *per_api_ignore.get(api_id, [])]
            body_diff = compare_bodies(a_body, b_body, row_ignore)
            verdict = "equal" if body_diff.equal else "value_diff"
            diff_paths = body_diff.diff_paths
            basis = "body"
            note = None
        rows.append({**pair, "verdict": verdict, "diff_paths": diff_paths, "basis": basis, "note": note})
    clusters = cluster_diffs([
        {"diff_paths": r["diff_paths"], "row_key": f"{r['api_id']}|{r['test_data_label'] or ''}"}
        for r in rows if r["verdict"] == "value_diff"
    ])
    return {
        "targets": list(targets),
        "rows": rows,
        "value_diff_count": sum(1 for r in rows if r["verdict"] == "value_diff"),
        "status_diff_count": sum(1 for r in rows if r["verdict"] == "status_diff"),
        "clusters": [c.model_dump(mode="json") for c in clusters],
        "ignore_paths": list(ignore_paths),
        "per_api_ignore": {k: list(v) for k, v in per_api_ignore.items()},
    }


async def _load_bodies(pairs: Sequence[dict], body_loader: BodyLoader) -> dict[str, dict | None]:
    """Two bodies per parity row, and nothing else — the whole reason `write` takes a loader
    instead of a runs-with-bodies list. A loader that raises for one run leaves that run's body
    None (a status_only row), never the whole report."""
    bodies: dict[str, dict | None] = {}
    for pair in pairs:
        for run_id in (pair["a_run_id"], pair["b_run_id"]):
            if run_id in bodies:
                continue
            bodies[run_id] = await body_loader(run_id)
    return bodies


class Reports:
    def __init__(self, out_dir: Path, portal_origin: str = "http://localhost:3110",
                 capture_disabled: CaptureDisabled | None = None):
        self.out_dir = out_dir
        self.portal_origin = portal_origin
        # `api_id -> is this API's group opted out of body capture` — the ONLY thing that tells a
        # missing body "캡처 해제" apart from "만료". Left None a missing body reads as expired,
        # which is the safe default: it never claims a capture policy that isn't configured.
        self.capture_disabled = capture_disabled
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _folder(self, job_id: str) -> Path:
        if not SAFE_ID.fullmatch(job_id):
            raise ValueError(f"unsafe report id: {job_id!r}")
        base = self.out_dir.resolve()
        folder = (self.out_dir / job_id).resolve()
        if not folder.is_relative_to(base):
            raise ValueError(f"report id escapes the reports directory: {job_id!r}")
        return folder

    # -- runs.jsonl ---------------------------------------------------------------------------

    @staticmethod
    def _append_runs(folder: Path, runs: Sequence[RunResult]) -> None:
        if not runs:
            return
        # newline="\n": these lines are read back by split, and a CRLF file would still parse --
        # but the artifact stays one shape on every platform.
        with (folder / RUNS_FILE).open("a", encoding="utf-8", newline="\n") as handle:
            for run in runs:
                handle.write(json.dumps(_run_row(run), ensure_ascii=False) + "\n")

    @staticmethod
    def _read_run_dicts(folder: Path) -> list[dict]:
        """Every row appended so far, in file order, de-duplicated by run_id (last wins). The
        file is bounded by the job's own matrix × schedules, which every guardrail in
        `check_job_guardrails` already caps."""
        path = folder / RUNS_FILE
        if not path.exists():
            return []
        rows: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                row = json.loads(line)
                rows[row["run_id"]] = row
        return list(rows.values())

    def all_runs(self, job_id: str) -> list[dict]:
        return self._read_run_dicts(self._folder(job_id))

    # -- write / rediff -----------------------------------------------------------------------

    async def write(
        self,
        job: JobSpec,
        new_runs: Sequence[RunResult],
        *,
        body_loader: BodyLoader,
        generator: str = "refresh_runner",
        ignore_paths: Sequence[str] = (),
        per_api_ignore: dict[str, list[str]] | None = None,
    ) -> Path:
        """Append THIS execution's runs and re-render. ``new_runs`` is one occurrence's output,
        not the job's history — the history is runs.jsonl, which this reads back for the summary,
        the matrix and the parity cell selection. Bodies come from ``body_loader`` and only for
        the cells parity actually compares."""
        folder = self._folder(job.job_id)
        folder.mkdir(parents=True, exist_ok=True)
        self._append_runs(folder, new_runs)
        rows = self._read_run_dicts(folder)
        runs = [RunResult.model_validate(row) for row in rows]

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

        pairs = _parity_pairs(job, runs)
        parity = None
        if pairs is not None:
            bodies = await _load_bodies(pairs, body_loader)
            parity = _parity_block(job.target_envs, pairs, bodies, ignore_paths, per_api_ignore,
                                   self.capture_disabled)
        self._write_parity(folder, parity)

        data = {
            "job": job.model_dump(mode="json", exclude_none=True),
            "summary": counts,
            "matrix": _matrix(job, runs),
            "parity": parity,
            "runs_total": len(runs),
            "provenance": {"generator": generator, "generated_at": datetime.now(UTC).isoformat()},
            "portal_origin": self.portal_origin,
        }
        return self._write_report(folder, data, rows)

    async def rediff(
        self, job_id: str, ignore_paths: Sequence[str], per_api_ignore: dict[str, list[str]] | None = None,
        *, body_loader: BodyLoader,
    ) -> Path:
        """Recompute ONLY the parity block, over the pairs ``parity.json`` already recorded and
        their bodies re-fetched through ``body_loader`` — no new run, no re-selection, no
        re-judging of any past ``RunResult``. Lets an operator narrow noise (a volatile field)
        without re-hitting either server. ``summary``/``matrix``/runs.jsonl are untouched. A
        report with no parity block (a one-env job) is left exactly as it is."""
        folder = self._folder(job_id)
        data = json.loads((folder / DATA_FILE).read_text(encoding="utf-8"))
        stored = self._read_parity(folder) or data.get("parity")
        if stored is None:
            return folder / INDEX_FILE
        pairs = [
            {k: row[k] for k in ("api_id", "test_data_label", "a_run_id", "b_run_id", "a_status", "b_status")}
            for row in stored["rows"]
        ]
        bodies = await _load_bodies(pairs, body_loader)
        parity = _parity_block(stored["targets"], pairs, bodies, ignore_paths, per_api_ignore,
                               self.capture_disabled)
        self._write_parity(folder, parity)
        data["parity"] = parity
        data["provenance"] = {**data["provenance"], "generated_at": datetime.now(UTC).isoformat()}
        return self._write_report(folder, data, self._read_run_dicts(folder))

    # -- artifacts ----------------------------------------------------------------------------

    @staticmethod
    def _write_parity(folder: Path, parity: dict | None) -> None:
        (folder / PARITY_FILE).write_text(
            json.dumps(parity, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _read_parity(folder: Path) -> dict | None:
        path = folder / PARITY_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_report(self, folder: Path, data: dict, rows: Sequence[dict]) -> Path:
        (folder / DATA_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        latest = sorted(rows, key=_row_order, reverse=True)[:EMBEDDED_RUN_ROWS]
        embedded_data = {**data, "runs": latest, "runs_shown": len(latest)}
        # A model-authored summary or test-data value can carry `<!--<script` — inside the
        # data script tag that sequence walks the HTML tokenizer into script-data-double-escape,
        # where a lone `</script>` does not close the block, and everything to EOF is
        # swallowed (confirmed against the tokenizer spec; JSON.parse then never runs and the
        # report renders as an empty skeleton). Escaping every `<` to its JSON unicode escape
        # removes the character from the markup entirely — `</script>` handling comes free —
        # while leaving the JSON valid; JSON.parse decodes `<` back to `<` in the template.
        embedded = json.dumps(embedded_data, ensure_ascii=False).replace("<", "\\u003c")
        html = TEMPLATE.read_text(encoding="utf-8").replace("__REPORT_DATA__", embedded)
        (folder / INDEX_FILE).write_text(html, encoding="utf-8")
        return folder / INDEX_FILE

    def read_html(self, job_id: str) -> str | None:
        path = self._folder(job_id) / INDEX_FILE
        return path.read_text(encoding="utf-8") if path.exists() else None

    def parity(self, job_id: str) -> dict | None:
        folder = self._folder(job_id)
        stored = self._read_parity(folder)
        if stored is not None:
            return stored
        path = folder / DATA_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get("parity")
