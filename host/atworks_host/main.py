from __future__ import annotations

import asyncio
import functools
import logging
import os
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from atworks_agent import AtworksAgentConfig, AtworksSessionState
from atworks_agent_runtime import AtworksAgent
from atworks_agent_runtime.insight_narrator import narrate_insights

from .app import create_app
from .briefing import Briefings
from .insights import InsightPanels
from .memory_store import SqliteMemoryStore
from .mock_backend import MockAtworks
from .promoter import Promoter
from .reports import Reports
from .retention import Retention, TimestampedSessionStore
from .scheduler import Scheduler
from .store import Store
from .streaming import spawn_background

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

logger = logging.getLogger(__name__)


def build() -> tuple:
    # This deployment's .env takes precedence over inherited shell variables.
    load_dotenv(ROOT / ".env", override=True)
    # Empty credentials break auth fallback: the SDK treats "" as a present key and sends it.
    # Unset them so the SDK tries the next auth method.
    for cred in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(cred) == "":
            del os.environ[cred]
    if os.environ.get("ATWORKS_TRUST_OS_CA", "1") != "0":
        # The closed network's intercepting proxy presents a CA that the Windows/OS
        # certificate store trusts but Python's bundled certifi does not — every model
        # call would fail with CERTIFICATE_VERIFY_FAILED without this. truststore patches
        # ssl at the process level, so it must run before AsyncAnthropic opens any
        # connection. Set ATWORKS_TRUST_OS_CA=0 to fall back to certifi only.
        import truststore

        truststore.inject_into_ssl()
    config = AtworksAgentConfig(
        model=os.environ.get("ATWORKS_MODEL", "claude-sonnet-4-5"),
        enable_insight_narration=os.environ.get("ATWORKS_INSIGHT_NARRATION", "1") != "0",
        # 메모리는 켜되(어휘가 그 위에 산다) 참조 구현의 턴 후 자유 사실 추출은 켜지 않는다
        # (self-growth §7): 저장되는 것은 사람이 [예]를 누른 어휘뿐이다. save_memory/
        # recall_memories 도구는 애초에 레지스트리에 없어서 모델은 이 저장소에 닿지 못한다.
        enable_memory=True,
        memory_extract_facts=False,
    )
    # ATWORKS_STORE_PATH picks the SQLite file the Mock runs on; unset keeps the demo's
    # ephemeral ":memory:" store (fixtures reloaded every boot). Point it at a file and runs,
    # bodies, rollups and the audit log survive a restart -- which is also what makes the
    # retention job's cold partition mean anything.
    store = Store(os.environ.get("ATWORKS_STORE_PATH", ":memory:"))
    # ATWORKS_FIXTURES_DIR points the API-spec/operator catalogue at a dataset dir; pair it with
    # ATWORKS_STORE_PATH pointing at that dataset's scale.sqlite to boot the host on a generated
    # large dataset (scripts/scale/generate.py writes both in one out dir). Unset = demo fixtures.
    fixtures_dir = Path(os.environ.get("ATWORKS_FIXTURES_DIR", HERE / "fixtures"))
    backend = MockAtworks(config, fixtures_dir, store=store)
    # 같은 SQLite 파일 위의 MemoryStore. `MemoryRuntime.build`가 `check_memory_store`를 돌리므로
    # 계약이 하나라도 비면 첫 턴이 아니라 부팅에서 죽는다.
    agent = AtworksAgent(backend=backend, skills_dir=ROOT / "atworks-agent" / "skills", config=config,
                         memory_store=SqliteMemoryStore(store))
    portal_origin = os.environ.get("ATWORKS_PORTAL_ORIGIN", "http://localhost:3110")
    # `capture_disabled` is wired by create_app (one place, so the test client gets it too).
    reports = Reports(HERE / "reports_out", portal_origin=portal_origin)
    briefings = Briefings(HERE / "briefings_out", config, portal_origin=portal_origin)
    insights_dir = HERE / "insights_out"
    insights = InsightPanels(insights_dir, config, narrator=functools.partial(narrate_insights, agent.client, config))
    # One session store shared by the router and the retention sweep -- built here, not inside
    # create_app, so the idle TTL really reaches the sessions this process serves.
    sessions = TimestampedSessionStore(AtworksSessionState)
    retention = Retention(store, config, insights_dir, sessions)
    promoter = Promoter(store, config)
    scheduler = Scheduler(backend, reports, None, briefings=briefings, retention=retention,
                          promoter=promoter)

    async def loop() -> None:
        while True:
            try:
                await scheduler.tick(datetime.now().astimezone())
            except Exception:
                # A tick that raises must not kill the background loop; the next tick
                # 60s from now is what keeps due jobs moving.
                logger.exception("scheduler tick failed")
            await asyncio.sleep(60)

    async def start_loop() -> None:
        # spawn_background keeps a strong reference in a module-level set (the event
        # loop itself only holds a weak one), so the loop is not dropped mid-flight.
        spawn_background(loop())

    app = create_app(agent=agent, backend=backend, scheduler=scheduler, reports=reports, briefings=briefings,
                      insights=insights, sessions=sessions, on_startup=[start_loop])
    return app, backend, scheduler


app, _backend, _scheduler = build()

if __name__ == "__main__":
    uvicorn.run("atworks_host.main:app", host="127.0.0.1", port=int(os.environ.get("ATWORKS_PORT", "8010")), reload=False)
