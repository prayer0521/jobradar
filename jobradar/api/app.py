"""FastAPI 应用装配。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..db import migrate
from ..scheduler import Scheduler
from ..tasks import TaskManager
from .routes import _submit_crawl, router

log = logging.getLogger(__name__)


def create_app(db_path=None, *, start_scheduler: bool = True) -> FastAPI:
    db = db_path or settings.db_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(db)
        n = app.state.tasks.reap_zombies()
        if n:
            log.info("清理了 %s 个上次进程残留的僵尸任务", n)
        if start_scheduler:
            app.state.scheduler = Scheduler(
                db, lambda **kw: _submit_crawl(app, trigger="schedule", **kw))
            app.state.scheduler.start()
        yield
        if getattr(app.state, "scheduler", None):
            app.state.scheduler.stop()

    app = FastAPI(title="jobradar", version="0.2.0", lifespan=lifespan)
    app.state.db_path = db
    app.state.tasks = TaskManager(db)
    app.state.scheduler = None

    # 本地自用，只放行同源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://{settings.host}:{settings.port}",
                       f"http://localhost:{settings.port}"],
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(router)

    web = settings.web_dir
    if web.exists():
        app.mount("/vendor", StaticFiles(directory=web / "vendor"),
                  name="vendor") if (web / "vendor").exists() else None
        app.mount("/static", StaticFiles(directory=web), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(web / "index.html")

        @app.get("/{page}.html", include_in_schema=False)
        def page(page: str):
            f = web / f"{page}.html"
            if not f.exists():
                return FileResponse(web / "index.html")
            return FileResponse(f)

    return app
