"""`jr 服务` 入口。"""
from __future__ import annotations

import logging

import uvicorn

from .api.app import create_app
from .config import settings


def serve(host: str | None = None, port: int | None = None,
          reload: bool = False) -> int:
    h = host or settings.host
    p = port or settings.port
    logging.getLogger("jobradar").setLevel(logging.INFO)
    # flush=True：uvicorn.run 会一直阻塞，不刷的话重定向到文件时这几行永远出不来
    print(f"\njobradar 服务已启动 -> http://{h}:{p}", flush=True)
    print(f"  仪表盘   http://{h}:{p}/", flush=True)
    print(f"  报告     http://{h}:{p}/report.html", flush=True)
    print(f"  任务     http://{h}:{p}/tasks.html", flush=True)
    print(f"  API 文档 http://{h}:{p}/docs", flush=True)
    print("\n按 Ctrl+C 停止\n", flush=True)
    uvicorn.run(create_app(), host=h, port=p, log_level="warning")
    return 0
