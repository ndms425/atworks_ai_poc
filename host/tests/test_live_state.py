"""state_now: each turn re-reads what waits on a person and the current status of the items the
conversation already knows, so the model never asks for approval of something already approved."""

from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    AtworksSessionState,
    JobDraft,
    JobKind,
)
from atworks_agent.live_state import live_state
from atworks_agent.prompt import build_dynamic_context
from atworks_agent.screen import render_screen_state_hint
from atworks_agent.types import ScreenState, ScreenTarget
from atworks_host.mock_backend import MockAtworks

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SESSION = AtworksSessionContext(session_id="s", project_id="mes-demo", operator="minseong")


async def _staged(backend):
    return await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="결제 API 즉시 실행", api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT,
    )


async def test_a_staged_job_is_listed_as_pending():
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    job = await _staged(backend)
    now = await live_state(backend, SESSION, AtworksSessionState())
    assert now["pending_approvals"]["total"] == 1
    assert now["pending_approvals"]["jobs"] == [{"id": job.job_id, "status": "staged", "summary": "결제 API 즉시 실행"}]


async def test_an_approved_job_is_no_longer_pending_and_the_stale_copy_is_replaced():
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    job = await _staged(backend)
    state = AtworksSessionState()
    state.seen_jobs[job.job_id] = job.model_copy()       # what the conversation remembers: staged
    await backend.apply_job(SESSION, job.job_id)        # approved on the Jobs page meanwhile

    now = await live_state(backend, SESSION, state)
    assert now["pending_approvals"]["total"] == 0
    [line] = now["this_conversation_items"]["jobs"]
    assert line["id"] == job.job_id and line["status"] == "applied"
    assert state.seen_jobs[job.job_id].status.value == "applied"


def test_state_now_and_row_status_reach_the_context():
    block = build_dynamic_context(atworks_context={"project": "p"}, attached_items=[],
                                  state_now={"pending_approvals": {"total": 0}})
    assert '"state_now"' in block and '"pending_approvals"' in block

    hint = render_screen_state_hint(ScreenState(view="jobs", visible=[
        ScreenTarget(kind="job", ref_id="job-0002", label="order 그룹 매일 9시", status="applied"),
    ]))
    assert "- job:job-0002 [applied] — order 그룹 매일 9시" in hint


# -- The bug the operator hit: approved on the card, then the model's own apply_job was told the job
# -- was "waiting for approval", and the model invented a state mismatch.


async def test_model_apply_of_an_already_approved_job_says_it_is_applied():
    from commerce_common.skills import Skill, SkillRegistry

    from atworks_agent import AtworksToolExecutor

    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    job = await _staged(backend)
    state = AtworksSessionState()
    state.seen_jobs[job.job_id] = job.model_copy()
    await backend.apply_job(SESSION, job.job_id)            # the operator's click on the card

    skills = SkillRegistry([Skill(name="job-approval", description="d", body="b")])
    executor = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=SESSION, state=state)
    out = await executor.execute("apply_job", {"job_id": job.job_id})

    assert not out.is_error and out.blocked is None
    assert "ALREADY APPLIED" in out.result_text and "minseong" in out.result_text
    assert "waiting for their approval" not in out.result_text
    assert state.seen_jobs[job.job_id].status.value == "applied"


async def test_model_apply_of_a_staged_job_still_says_it_waits_for_approval():
    from commerce_common.skills import Skill, SkillRegistry

    from atworks_agent import AtworksToolExecutor

    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    job = await _staged(backend)
    state = AtworksSessionState()
    state.seen_jobs[job.job_id] = job.model_copy()
    skills = SkillRegistry([Skill(name="job-approval", description="d", body="b")])
    executor = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=SESSION, state=state)
    out = await executor.execute("apply_job", {"job_id": job.job_id})
    assert out.blocked == "approval" and "waiting for their approval" in out.result_text
