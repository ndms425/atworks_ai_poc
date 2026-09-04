"""리포트 = 템플릿 1회 + data.json. open-design Live Artifact 계약: template.html·data.json·
provenance.generator. 스케줄러가 data.json만 갱신하고 index.html을 재렌더한다. LLM 0회."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atworks_agent import JobSpec, RunResult

TEMPLATE = Path(__file__).with_name("report_template.html")


class Reports:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def write(self, job: JobSpec, runs: list[RunResult], *, generator: str = "refresh_runner") -> Path:
        folder = self.out_dir / job.job_id
        folder.mkdir(parents=True, exist_ok=True)
        counts = {"total": len(runs), "pass": 0, "fail": 0, "error": 0}
        for r in runs:
            counts[r.status.value] += 1
        data = {
            "job": job.model_dump(mode="json", exclude_none=True),
            "summary": counts,
            "runs": [r.model_dump(mode="json", exclude_none=True) for r in runs],
            "provenance": {"generator": generator, "generated_at": datetime.now(UTC).isoformat()},
        }
        (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # </script> inside a job summary or a failed rule must not close the data script
        # block early; escaping the slash keeps the JSON valid while breaking that tag.
        embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        html = TEMPLATE.read_text(encoding="utf-8").replace("__REPORT_DATA__", embedded)
        (folder / "index.html").write_text(html, encoding="utf-8")
        return folder / "index.html"

    def read_html(self, job_id: str) -> str | None:
        path = self.out_dir / job_id / "index.html"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def all_runs(self, job_id: str) -> list[dict]:
        path = self.out_dir / job_id / "data.json"
        return json.loads(path.read_text(encoding="utf-8"))["runs"] if path.exists() else []
