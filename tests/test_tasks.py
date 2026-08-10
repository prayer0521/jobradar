"""任务运行器与调度器测试。"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from jobradar.db import connect
from jobradar.scheduler import (
    ScheduleConfig, Scheduler, load_config, next_run_at, save_config,
)
from jobradar.tasks import TaskBusy, TaskManager


@pytest.fixture
def tm(db_path):
    return TaskManager(db_path)


class TestSingleSlot:
    def test_忙时第二次提交被拒(self, tm):
        gate = threading.Event()
        tm.submit("crawl", lambda rec: gate.wait(timeout=5))
        with pytest.raises(TaskBusy):
            tm.submit("analyze", lambda rec: None)
        gate.set()

    def test_完成后槽位释放(self, tm):
        r1 = tm.submit("crawl", lambda rec: {"ok": 1})
        for _ in range(100):
            if r1.state == "succeeded":
                break
            time.sleep(0.02)
        assert r1.state == "succeeded"
        r2 = tm.submit("analyze", lambda rec: {"ok": 2})   # 不该抛
        assert r2.id != r1.id

    def test_失败也释放槽位(self, tm):
        def boom(rec):
            raise RuntimeError("炸了")
        r1 = tm.submit("crawl", boom)
        for _ in range(100):
            if r1.state == "failed":
                break
            time.sleep(0.02)
        assert r1.state == "failed"
        assert "炸了" in r1.error
        tm.submit("crawl", lambda rec: None)               # 不该抛

    def test_看到终态时槽位必已释放(self, tm):
        """state 是外部判断"结束了吗"的唯一信号，必须晚于 release。

        否则调用方看到 succeeded 后立刻提交下一个任务会撞上 TaskBusy。
        """
        for _ in range(20):
            rec = tm.submit("crawl", lambda r: {"ok": 1})
            while rec.state not in ("succeeded", "failed", "cancelled"):
                time.sleep(0.001)
            # 看到终态的下一刻就提交，不留缓冲
            tm.submit("crawl", lambda r: None).id
            while tm.is_busy():
                time.sleep(0.001)


class TestCancel:
    def test_取消置位后任务在检查点退出(self, tm):
        started = threading.Event()

        def long_job(rec):
            started.set()
            for _ in range(200):
                if rec.should_stop():
                    return {"stopped": True}
                time.sleep(0.01)
            return {"stopped": False}

        rec = tm.submit("crawl", long_job)
        started.wait(timeout=5)
        assert tm.cancel(rec.id) is True
        for _ in range(200):
            if rec.state == "cancelled":
                break
            time.sleep(0.02)
        assert rec.state == "cancelled"
        assert rec.result == {"stopped": True}

    def test_取消已结束的任务返回False(self, tm):
        rec = tm.submit("crawl", lambda r: None)
        for _ in range(100):
            if rec.state == "succeeded":
                break
            time.sleep(0.02)
        assert tm.cancel(rec.id) is False

    def test_取消不存在的任务返回False(self, tm):
        assert tm.cancel("nope") is False


class TestProgress:
    def test_on_event更新进度并记日志(self, tm):
        from jobradar.service import Event

        def job(rec):
            rec.on_event(Event("batch", phase="extract", current=3, total=10,
                               message="抽取进度 3/10"))
            return {}

        rec = tm.submit("analyze", job)
        for _ in range(100):
            if rec.state == "succeeded":
                break
            time.sleep(0.02)
        d = rec.to_dict()
        assert d["progress"]["current"] == 3
        assert d["progress"]["total"] == 10
        assert d["progress"]["pct"] == 30.0
        assert any("抽取进度 3/10" in ln for ln in d["logs"])

    def test_日志环有上限(self, tm):
        def job(rec):
            for i in range(500):
                rec.log(f"line {i}")
            return {}
        rec = tm.submit("crawl", job)
        for _ in range(200):
            if rec.state == "succeeded":
                break
            time.sleep(0.02)
        assert len(rec.logs) == 200


class TestPersistence:
    def test_任务落库(self, tm, db_path):
        rec = tm.submit("crawl", lambda r: {"total": 5})
        for _ in range(100):
            if rec.state == "succeeded":
                break
            time.sleep(0.02)
        with connect(db_path, readonly=True) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?",
                               (rec.id,)).fetchone()
        assert row["state"] == "succeeded"
        assert row["kind"] == "crawl"

    def test_僵尸任务被回收(self, tm, db_path):
        from jobradar.db import write_tx
        with connect(db_path) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO tasks (id, kind, state, trigger, created_at)"
                " VALUES ('zombie','crawl','running','manual','t')")
        assert tm.reap_zombies() == 1
        with connect(db_path, readonly=True) as conn:
            state = conn.execute(
                "SELECT state FROM tasks WHERE id='zombie'").fetchone()["state"]
        assert state == "interrupted"


class TestNextRunAt:
    def test_当天稍后触发(self):
        cfg = ScheduleConfig(enabled=True, hour=15, minute=30)
        now = datetime(2026, 8, 7, 10, 0)          # 周五
        assert next_run_at(cfg, now) == datetime(2026, 8, 7, 15, 30)

    def test_当天已过则顺延到明天(self):
        cfg = ScheduleConfig(enabled=True, hour=3, minute=30)
        now = datetime(2026, 8, 7, 10, 0)
        assert next_run_at(cfg, now) == datetime(2026, 8, 8, 3, 30)

    def test_跳到下一个勾选的星期(self):
        # 只勾周一(0)，现在是周五
        cfg = ScheduleConfig(enabled=True, hour=3, minute=30, weekdays=[0])
        now = datetime(2026, 8, 7, 10, 0)
        nxt = next_run_at(cfg, now)
        assert nxt.weekday() == 0
        assert nxt == datetime(2026, 8, 10, 3, 30)

    def test_未启用返回None(self):
        assert next_run_at(ScheduleConfig(enabled=False), datetime.now()) is None

    def test_一个星期都没勾返回None(self):
        cfg = ScheduleConfig(enabled=True, weekdays=[])
        assert next_run_at(cfg, datetime.now()) is None

    def test_边界不返回当前时刻(self):
        """正好卡在触发时刻时应给出下一次，避免同一分钟反复触发。"""
        cfg = ScheduleConfig(enabled=True, hour=3, minute=30)
        now = datetime(2026, 8, 7, 3, 30, 0)
        assert next_run_at(cfg, now) == datetime(2026, 8, 8, 3, 30)


class TestScheduleConfig:
    def test_存取往返(self, db_path):
        cfg = ScheduleConfig(enabled=True, hour=5, minute=15,
                             weekdays=[0, 2, 4], companies=["tencent"],
                             max_pages=50)
        save_config(db_path, cfg)
        got = load_config(db_path)
        assert got == cfg

    def test_未配置时给默认值(self, db_path):
        assert load_config(db_path) == ScheduleConfig()

    def test_非法值被拒(self, db_path):
        for bad in (ScheduleConfig(hour=24), ScheduleConfig(minute=60),
                    ScheduleConfig(weekdays=[9]), ScheduleConfig(max_pages=0)):
            with pytest.raises(ValueError):
                save_config(db_path, bad)


class TestSchedulerSkip:
    def test_忙时跳过不排队(self, db_path):
        save_config(db_path, ScheduleConfig(enabled=True, hour=0, minute=0))
        calls = []

        def busy_submit(**kw):
            calls.append(kw)
            raise TaskBusy("running-id")

        sch = Scheduler(db_path, busy_submit)
        sch._next = datetime(2020, 1, 1)      # 强制已到期
        sch._tick()
        assert len(calls) == 1
        assert sch.last_result.startswith("skipped:")

    def test_成功触发记录状态(self, db_path):
        save_config(db_path, ScheduleConfig(enabled=True, hour=0, minute=0))
        sch = Scheduler(db_path, lambda **kw: None)
        sch._next = datetime(2020, 1, 1)
        sch._tick()
        assert sch.last_result == "started"
        assert sch.next_run is not None       # 触发后重算下次

    def test_未启用时不触发(self, db_path):
        save_config(db_path, ScheduleConfig(enabled=False))
        calls = []
        sch = Scheduler(db_path, lambda **kw: calls.append(kw))
        sch._next = datetime(2020, 1, 1)
        sch._tick()
        assert not calls
