import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
)
from atworks_agent.serialization import run_record
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="mask", project_id="mes", operator="minseong")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"

# api-001 (group "contract") is the fixture API mock_backend.stub_response gives a PII-shaped
# sample body to (see host/atworks_host/mock_backend.py stub_response) -- neither field depends
# on env/seq so it doesn't disturb any pre-existing parity/stub_response assertion.
PII_EMAIL = "hong@example.com"
PII_SSN = "900101-1234567"


def _backend(**config_kwargs) -> MockAtworks:
    return MockAtworks(AtworksAgentConfig(model="m", **config_kwargs), FIXTURES)


async def _run_api_001(backend: MockAtworks) -> str:
    job = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT,
    )
    await backend.apply_job(SESSION, job.job_id)
    produced = await backend.execute_job_once(SESSION, job.job_id)
    assert len(produced) == 1
    return produced[0].run_id


# -- capture-enabled: body is stored, but masked -------------------------------------------------

async def test_get_body_returns_the_masked_body_with_no_raw_pii_substring():
    backend = _backend()   # masking_enabled=True (default), no disabled_groups
    run_id = await _run_api_001(backend)

    stored = backend.store.get_body(run_id)
    assert stored is not None
    assert stored["contact"] == "***"
    assert stored["ssn"] == "***"
    assert PII_EMAIL not in json.dumps(stored)
    assert PII_SSN not in json.dumps(stored)
    # a field with no PII-shaped value is untouched
    assert stored["path"] == backend.apis["api-001"].path


async def test_runs_by_ids_reports_the_body_exists_without_carrying_it():
    """Final review I3: the read paths no longer join `bodies` at all. A record says a body
    EXISTS; `get_body` is the only way to its content (and that content is masked)."""
    backend = _backend()
    run_id = await _run_api_001(backend)

    [run] = backend.store.runs_by_ids([run_id])
    assert run.response_body is None
    assert run.has_body is True
    assert run_record(run)["has_body"] is True
    assert backend.store.get_body(run_id)["contact"] == "***"


async def test_masking_enabled_false_stores_the_body_unmasked():
    backend = _backend(masking_enabled=False)
    run_id = await _run_api_001(backend)

    stored = backend.store.get_body(run_id)
    assert stored["contact"] == PII_EMAIL
    assert stored["ssn"] == PII_SSN


# -- capture disabled for the API's group: no bodies row at all ----------------------------------

async def test_disabled_group_never_writes_a_bodies_row_and_has_body_is_false():
    backend = _backend(masking_disabled_groups=("contract",))   # api-001's group
    run_id = await _run_api_001(backend)

    assert backend.store.get_body(run_id) is None

    run = await backend.get_run(SESSION, run_id)
    assert run.response_body is None
    assert run_record(run)["has_body"] is False
    assert "response_body" not in run_record(run)


async def test_a_different_group_still_captures_its_body():
    # api-004 is group "payment" -- unaffected by disabling "contract"
    backend = _backend(masking_disabled_groups=("contract",))
    job = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-004"], target_envs=["dev"]),
        ActorKind.AGENT,
    )
    await backend.apply_job(SESSION, job.job_id)
    produced = await backend.execute_job_once(SESSION, job.job_id)
    assert backend.store.get_body(produced[0].run_id) is not None


# -- parity report: disabled-capture row falls back to status_only, captured row stays body-based

async def test_parity_report_marks_a_disabled_capture_row_status_only_with_the_korean_note(tmp_path):
    backend = _backend(masking_disabled_groups=("contract",))   # api-001 opted out
    # capture_disabled is what lets the report say "캡처 해제" rather than "만료" (create_app
    # wires this from the backend for the real host).
    reports = Reports(tmp_path, capture_disabled=backend.capture_disabled)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(
        SESSION, JobDraft(
            kind=JobKind.RUN_NOW, summary="capture opt-out parity",
            api_ids=["api-001", "api-004"], target_envs=["legacy", "renewed"]),
        ActorKind.AGENT,
    )
    await backend.apply_job(SESSION, job.job_id)
    now = datetime(2026, 9, 5, 9, 0, tzinfo=KST)
    assert await sched.tick(now) == [job.job_id]

    data = json.loads((reports.out_dir / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in data["parity"]["rows"]}

    # api-001's group is disabled -> no body was ever captured -> status-only fallback
    row1 = rows["api-001"]
    assert row1["basis"] == "status_only"
    assert row1["note"] == "본문 캡처 해제"
    assert row1["diff_paths"] == []

    # api-004 still captures (and masks) its body -> judged on the body as before
    row4 = rows["api-004"]
    assert row4["basis"] == "body"
    assert row4.get("note") is None
    assert row4["verdict"] == "value_diff"
    assert "$.limit" in row4["diff_paths"]

    # no PII anywhere in the written report artifact
    raw = (reports.out_dir / job.job_id / "data.json").read_text(encoding="utf-8")
    assert PII_EMAIL not in raw
    assert PII_SSN not in raw
