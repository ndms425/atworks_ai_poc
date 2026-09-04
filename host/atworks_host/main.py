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
from .mock_backend import MockAtworks
from .reports import Reports
from .scheduler import Scheduler
from .streaming import spawn_background

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

logger = logging.getLogger(__name__)


def build() -> tuple:
    load_dotenv(ROOT / ".env")
    config = AtworksAgentConfig(model=os.environ.get("ATWORKS_MODEL", "claude-sonnet-4-5"))
    backend = MockAtworks(config, HERE / "fixtures")
    agent = AtworksAgent(backend=backend, skills_dir=ROOT / "atworks-agent" / "skills", config=config)
    reports = Reports(HERE / "reports_out")
    scheduler = Scheduler(backend, reports, None)

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

    app = create_app(agent=agent, backend=backend, scheduler=scheduler, reports=reports, on_startup=[start_loop])
    return app, backend, scheduler


app, _backend, _scheduler = build()

if __name__ == "__main__":
    uvicorn.run("atworks_host.main:app", host="127.0.0.1", port=int(os.environ.get("ATWORKS_PORT", "8010")), reload=False)
