from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from atworks_agent import AtworksAgentConfig
from atworks_agent_runtime import AtworksAgent

from .app import create_app
from .briefing import Briefings
from .mock_backend import MockAtworks
from .reports import Reports
from .scheduler import Scheduler
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
    config = AtworksAgentConfig(model=os.environ.get("ATWORKS_MODEL", "claude-sonnet-4-5"))
    backend = MockAtworks(config, HERE / "fixtures")
    agent = AtworksAgent(backend=backend, skills_dir=ROOT / "atworks-agent" / "skills", config=config)
    portal_origin = os.environ.get("ATWORKS_PORTAL_ORIGIN", "http://localhost:3110")
    reports = Reports(HERE / "reports_out", portal_origin=portal_origin)
    briefings = Briefings(HERE / "briefings_out", config, portal_origin=portal_origin)
    scheduler = Scheduler(backend, reports, None, briefings=briefings)

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
                      on_startup=[start_loop])
    return app, backend, scheduler


app, _backend, _scheduler = build()

if __name__ == "__main__":
    uvicorn.run("atworks_host.main:app", host="127.0.0.1", port=int(os.environ.get("ATWORKS_PORT", "8010")), reload=False)
