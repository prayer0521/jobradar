"""定时抓取。只抓取，不分析——分析烧配额，必须手动触发。

不用 APScheduler 的决定性理由：它自带执行器和 max_instances，
与 TaskManager 的单槽语义重复，两套并发控制叠加是 bug 温床。
而我们需要的只是「到点调一次 submit」，它擅长的 cron 表达能力用不上。

自研调度器最大的坑是夏令时，但中国全境 UTC+8 无 DST，
「每天 03:30」永远唯一且存在。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .db import connect, write_tx

log = logging.getLogger(__name__)

SETTINGS_KEY = "schedule"
TICK_SECONDS = 20


@dataclass
class ScheduleConfig:
    enabled: bool = False
    hour: int = 3
    minute: int = 30
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    companies: list[str] | None = None
    max_pages: int = 30

    def validate(self) -> "ScheduleConfig":
        if not 0 <= self.hour <= 23:
            raise ValueError("hour 必须在 0-23")
        if not 0 <= self.minute <= 59:
            raise ValueError("minute 必须在 0-59")
        bad = [d for d in self.weekdays if not 0 <= d <= 6]
        if bad:
            raise ValueError(f"weekdays 必须在 0-6（0=周一），非法值: {bad}")
        if not 1 <= self.max_pages <= 200:
            raise ValueError("max_pages 必须在 1-200")
        return self


def next_run_at(cfg: ScheduleConfig, now: datetime) -> datetime | None:
    if not cfg.enabled or not cfg.weekdays:
        return None
    for delta in range(0, 8):
        cand = (now + timedelta(days=delta)).replace(
            hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
        if cand > now and cand.weekday() in cfg.weekdays:
            return cand
    return None


def load_config(db_path) -> ScheduleConfig:
    with connect(db_path, readonly=True) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (SETTINGS_KEY,)).fetchone()
    if not row:
        return ScheduleConfig()
    try:
        return ScheduleConfig(**json.loads(row["value"]))
    except (ValueError, TypeError) as exc:
        log.warning("定时配置解析失败，回落到默认值: %s", exc)
        return ScheduleConfig()


def save_config(db_path, cfg: ScheduleConfig) -> None:
    cfg.validate()
    with connect(db_path) as conn, write_tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SETTINGS_KEY, json.dumps(asdict(cfg), ensure_ascii=False)))


class Scheduler:
    """每 20 秒醒一次比较时间。

    用轮询而不是 sleep(距下次还剩多久)：后者遇到系统休眠或时钟跳变
    会睡过头。20 秒精度对每天一次的抓取绰绰有余。
    """

    def __init__(self, db_path, submit_crawl) -> None:
        self.db_path = db_path
        self._submit = submit_crawl
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next: datetime | None = None
        self.last_run_at: str | None = None
        self.last_result: str | None = None

    def start(self) -> None:
        self.refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def refresh(self) -> None:
        cfg = load_config(self.db_path)
        self._next = next_run_at(cfg, datetime.now())

    @property
    def next_run(self) -> datetime | None:
        return self._next

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._tick()
            except Exception as exc:                  # noqa: BLE001
                log.warning("调度器 tick 出错（继续运行）: %s", exc)

    def _tick(self) -> None:
        cfg = load_config(self.db_path)
        if not cfg.enabled:
            self._next = None
            return
        now = datetime.now()
        if self._next is None:
            self._next = next_run_at(cfg, now)
            return
        if now < self._next:
            return

        self.last_run_at = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._submit(companies=cfg.companies, max_pages=cfg.max_pages)
            self.last_result = "started"
            log.info("定时抓取已触发")
        except Exception as exc:                      # noqa: BLE001
            # 上一轮还没跑完说明抓取比周期还慢，排队只会雪崩，跳过本次
            self.last_result = f"skipped: {exc}"
            log.warning("定时抓取跳过: %s", exc)
        self._next = next_run_at(cfg, datetime.now())
