"""集中配置。所有路径基于包所在位置绝对化。

为什么需要这个模块：原来 `load_env(".env")` 和 `DEFAULT_DB="data/jobs.db"`
都是相对 CWD 的。CLI 靠 `jr` 脚本里的 `cd` 兜住了，但 uvicorn / systemd
下 CWD 可能是任意目录，会静默读不到 .env、或在别处建一个空库。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

# 锚点：jobradar/config.py -> jobradar/ -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    db_path: Path = PROJECT_ROOT / "data" / "jobs.db"
    env_file: Path = PROJECT_ROOT / ".env"
    reports_dir: Path = PROJECT_ROOT / "reports"
    web_dir: Path = PROJECT_ROOT / "web"
    crawl_lock: Path = PROJECT_ROOT / "data" / ".crawl.lock"
    host: str = "127.0.0.1"      # 无鉴权，不要轻易改成 0.0.0.0
    port: int = 8787

    @classmethod
    def load(cls) -> "Settings":
        base = cls()
        return replace(
            base,
            db_path=Path(os.getenv("JOBRADAR_DB") or base.db_path).resolve(),
            host=os.getenv("JOBRADAR_HOST", base.host),
            port=int(os.getenv("JOBRADAR_PORT") or base.port),
        )


settings = Settings.load()
