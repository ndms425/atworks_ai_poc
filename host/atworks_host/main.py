from __future__ import annotations

import asyncio
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def build() -> tuple:
    load_dotenv(ROOT / ".env")
    config = AtworksAgentConfig(model=os.environ.get("ATWORKS_MODEL", "claude-sonnet-4-5"))
    backend = MockAtworks(config, HERE / "fixtures")
    agent = AtworksAgent(backend=backend, skills_dir=ROOT / "atworks-agent" / "skills", config=config)
    reports = Reports(HERE / "reports_out")
    scheduler = Scheduler(backend, reports, None)

    async def loop() -> None:
        while True:
            await scheduler.tick(datetime.now().astimezone())
            await asyncio.sleep(60)

    async def start_loop() -> None:
        asyncio.create_task(loop())

    app = create_app(agent=agent, backend=backend, scheduler=scheduler, reports=reports, on_startup=[start_loop])
    return app, backend, scheduler


app, _backend, _scheduler = build()

if __name__ == "__main__":
    uvicorn.run("atworks_host.main:app", host="127.0.0.1", port=int(os.environ.get("ATWORKS_PORT", "8010")), reload=False)
