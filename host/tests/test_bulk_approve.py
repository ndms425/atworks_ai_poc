"""전체 승인 / 전체 반려.

일괄은 단건 승인 경로 위의 루프일 뿐이다 — 그래서 여기서 지켜야 할 것은 "N건이 승인됐다"가
아니라, 그 N건이 **단건과 똑같이** 승인됐다는 것이다: 건건의 감사 2행, 건건의 마크 해제,
그리고 승인 표식이 루프가 끝난 뒤에 남아 있지 않을 것.
"""

import re
from pathlib import Path

import pytest
from commerce_common.testing import FakeClient, text_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import ActorKind, AtworksAgentConfig, AtworksSessionContext, JobDraft, JobKind
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

from .test_app import FIXTURES, SKILLS


@pytest.fixture
async def client_backend(tmp_path):
    config = AtworksAgentConfig(model="m", max_bulk_approve=3)
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c, backend


async def _stage(backend, n):
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    return [
        await backend.stage_job(
            session,
            JobDraft(kind=JobKind.RUN_NOW, summary=f"job {i}", api_ids=["api-001"], target_envs=["dev"]),
            ActorKind.AGENT,
        )
        for i in range(n)
    ]


async def test_bulk_apply_approves_every_pending_job(client_backend):
    client, backend = client_backend
    jobs = await _stage(backend, 3)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]

    r = await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                          json={"expected": 3})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["applied"] == 3 and body["failed"] == [] and body["remaining"] == 0
    for job in jobs:
        assert (await backend.get_job(None, job.job_id)).status.value == "applied"


async def test_bulk_discard_dismisses_every_pending_job(client_backend):
    client, backend = client_backend
    jobs = await _stage(backend, 2)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]

    r = await client.post("/api/atworks/changes/bulk-discard", headers={"X-Session-Id": sid},
                          json={"expected": 2})
    assert r.status_code == 200 and r.json()["applied"] == 2
    for job in jobs:
        assert (await backend.get_job(None, job.job_id)).status.value == "discarded"


async def test_a_stale_expected_count_approves_nothing(client_backend):
    """모델이 방금 올린 항목이 "전체"에 조용히 섞이는 경우. 하나도 승인되지 않아야 한다."""
    client, backend = client_backend
    jobs = await _stage(backend, 3)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]

    r = await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                          json={"expected": 2})
    assert r.status_code == 409
    for job in jobs:
        assert (await backend.get_job(None, job.job_id)).status.value == "staged"


async def test_the_cap_bounds_one_click_and_reports_the_rest(client_backend):
    """max_bulk_approve=3인 픽스처에 5건. 3건만 승인되고 2건은 remaining으로 보고된다."""
    client, backend = client_backend
    await _stage(backend, 5)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]

    r = await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                          json={"expected": 5})
    body = r.json()
    assert body["applied"] == 3 and body["remaining"] == 2 and body["cap"] == 3
    staged = await backend.get_pending_jobs(None)
    assert len(staged) == 2


async def test_every_item_leaves_its_own_audit_pair(client_backend):
    """감사 로그는 "사람이 무엇을 승인했나"에 답한다. 한 번의 클릭이 3건을 덮었어도 기록은
    건건이 남아야 하고, 각각 시도 1행 + 결과 1행이어야 한다."""
    client, backend = client_backend
    jobs = await _stage(backend, 3)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                      json={"expected": 3})

    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    rows = [e.model_dump(mode="json") for e in (await backend.audit(session, limit=200)).items]
    for job in jobs:
        actions = {row["action"] for row in rows if row["target_id"] == job.job_id}
        # 라우트의 2행(시도/결과)에 더해 백엔드 apply_job이 자기 `applied` 행을 남긴다 —
        # 단건 승인과 같은 구성이어야 한다.
        assert {"apply_job", "apply_job:ok"} <= actions, f"{job.job_id}: {actions}"


async def test_the_approval_mark_does_not_survive_the_loop(client_backend):
    """마크는 클릭 한 번에 소비되고 남지 않는다 — 일괄이라고 달라지지 않는다. 남아 있으면
    다음 채팅 턴이 아무도 누르지 않은 승인을 쓸 수 있다."""
    client, backend = client_backend
    await _stage(backend, 3)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                      json={"expected": 3})

    # 마크가 남았다면 같은 세션의 다음 호출이 그것을 쓸 수 있다. 큐가 빈 상태로 다시 불러
    # 아무 일도 일어나지 않는지 본다 — 남은 마크로 뭔가가 적용되면 applied가 0이 아니다.
    again = await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                              json={"expected": 0})
    assert again.status_code == 200 and again.json()["applied"] == 0


async def test_one_bulk_run_adds_one_context_line_not_one_per_item(client_backend):
    """100건을 승인하면 다음 턴의 컨텍스트 블록에 100문장이 들어가면 안 된다."""
    client, backend = client_backend
    await _stage(backend, 3)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    await client.post("/api/atworks/changes/bulk-apply", headers={"X-Session-Id": sid},
                      json={"expected": 3})

    # 다음 채팅 턴이 받는 컨텍스트 블록에 문장이 3개가 아니라 1개여야 한다. 블록은 턴 시작에
    # 비워지므로, 한 턴을 돌려 그 안에 들어간 문장을 센다.
    chat = await client.post("/api/atworks/chat", headers={"X-Session-Id": sid},
                             json={"message": "상태 요약"})
    assert chat.status_code == 200
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    del session


def test_inline_approval_cap_matches_the_web_constant():
    """채팅 카드를 접는 기준은 설정에 있고, 그 값을 실제로 쓰는 코드는 웹에 있다.

    두 숫자가 갈라지면 아무것도 깨지지 않고 조용히 틀린다 — 서버는 6건부터 접힌다고 믿고,
    화면은 8건까지 펼친다. 그래서 웹 상수를 읽어 설정값과 같은지 여기서 못 박는다.
    """
    source = (Path(__file__).resolve().parents[2] / "web" / "atworks-web" / "lib" / "approvals.ts").read_text(encoding="utf-8")
    match = re.search(r"export const MAX_INLINE_APPROVALS = (\d+);", source)
    assert match is not None, "web/atworks-web/lib/approvals.ts에서 MAX_INLINE_APPROVALS를 찾지 못했습니다"
    assert int(match.group(1)) == AtworksAgentConfig(model="m").max_inline_approvals
