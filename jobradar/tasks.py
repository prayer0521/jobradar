"""后台任务运行器。

全局单槽：同时只跑一个任务。理由——并发抓取对目标站双倍压力；
抓取与分析并行会让分析读到抓一半的数据，样本口径不可复现；
并发分析烧配额更快。

忙时直接拒绝（409）而不是排队：排队会让用户以为没点上然后重复点。
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .db import connect, write_tx

log = logging.getLogger(__name__)

MAX_LOGS = 200
MAX_RECORDS = 50


class TaskBusy(RuntimeError):
    def __init__(self, current_id: str | None) -> None:
        super().__init__("已有任务在运行")
        self.current_id = current_id


@dataclass
class TaskRecord:
    id: str
    kind: str                     # crawl | analyze
    state: str = "pending"        # pending|running|succeeded|failed|cancelled
    trigger: str = "manual"       # manual | schedule
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    params: dict = field(default_factory=dict)
    phase: str = ""
    current: int = 0
    total: int = 0
    message: str = ""
    logs: deque = field(default_factory=lambda: deque(maxlen=MAX_LOGS))
    result: dict | None = None
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        pct = None
        if self.total:
            pct = round(min(100.0, self.current / self.total * 100), 1)
        return {
            "id": self.id, "kind": self.kind, "state": self.state,
            "trigger": self.trigger, "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "params": self.params,
            "progress": {"phase": self.phase, "current": self.current,
                         "total": self.total, "message": self.message,
                         "pct": pct},
            "logs": list(self.logs),
            "result": self.result, "error": self.error,
        }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TaskManager:
    def __init__(self, db_path) -> None:
        self.db_path = db_path
        self._slot = threading.Lock()          # 单槽闸门
        self._guard = threading.RLock()        # 保护 _records
        self._records: OrderedDict[str, TaskRecord] = OrderedDict()
        self._current: str | None = None

    # ---------------------------------------------------------- 查询

    @property
    def current(self) -> TaskRecord | None:
        with self._guard:
            return self._records.get(self._current) if self._current else None

    def get(self, task_id: str) -> TaskRecord | None:
        with self._guard:
            return self._records.get(task_id)

    def recent(self, limit: int = 30) -> list[TaskRecord]:
        with self._guard:
            return list(self._records.values())[-limit:][::-1]

    def is_busy(self) -> bool:
        return self._current is not None

    # ---------------------------------------------------------- 提交

    def submit(self, kind: str, fn: Callable[[TaskRecord], Any],
               *, trigger: str = "manual", params: dict | None = None) -> TaskRecord:
        # 整个「取槽 + 登记」在 _guard 下完成，与 _run 的释放段对称，
        # 避免 _current 和 _slot 出现短暂的不一致窗口。
        with self._guard:
            if not self._slot.acquire(blocking=False):
                raise TaskBusy(self._current)
            rec = TaskRecord(id=uuid.uuid4().hex[:12], kind=kind,
                             trigger=trigger, created_at=_now(),
                             params=params or {})
            self._records[rec.id] = rec
            while len(self._records) > MAX_RECORDS:
                self._records.popitem(last=False)
            self._current = rec.id
        self._persist(rec)
        threading.Thread(target=self._run, args=(rec, fn),
                         daemon=True, name=f"task-{rec.id}").start()
        return rec

    def cancel(self, task_id: str) -> bool:
        rec = self.get(task_id)
        if rec is None or rec.state not in ("pending", "running"):
            return False
        rec._cancel.set()
        rec.log("收到取消请求，将在当前步骤结束后停止")
        return True

    # ---------------------------------------------------------- 内部

    def _run(self, rec: TaskRecord, fn) -> None:
        rec.state = "running"
        rec.started_at = _now()
        final_state = "succeeded"
        try:
            result = fn(rec)
            rec.result = result if isinstance(result, dict) else None
            final_state = "cancelled" if rec._cancel.is_set() else "succeeded"
        except Exception as exc:                      # noqa: BLE001
            final_state = "failed"
            rec.error = str(exc)
            rec.log(f"任务失败: {exc}")
            log.error("任务 %s 失败:\n%s", rec.id, traceback.format_exc())
        finally:
            rec.finished_at = _now()
            self._persist_state(rec, final_state)
            # 释放顺序很关键：state 是外部判断"结束了吗"的信号，
            # 必须在槽位真正放开之后才置位，否则调用方看到 succeeded
            # 立刻提交下一个任务会撞上 TaskBusy。
            with self._guard:
                self._current = None
                rec.state = final_state
                self._slot.release()

    def _persist_state(self, rec: TaskRecord, state: str) -> None:
        """落库时用显式 state：此刻 rec.state 还没更新到终态。"""
        self._persist(rec, state=state)

    def _persist(self, rec: TaskRecord, final: bool = False,
                 state: str | None = None) -> None:
        try:
            with connect(self.db_path) as conn, write_tx(conn):
                conn.execute(
                    """
                    INSERT INTO tasks (id, kind, state, trigger, created_at,
                        started_at, finished_at, params_json, result_json, error)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        state=excluded.state, started_at=excluded.started_at,
                        finished_at=excluded.finished_at,
                        result_json=excluded.result_json, error=excluded.error
                    """,
                    (rec.id, rec.kind, state or rec.state, rec.trigger,
                     rec.created_at, rec.started_at, rec.finished_at,
                     json.dumps(rec.params, ensure_ascii=False),
                     json.dumps(rec.result, ensure_ascii=False, default=str)
                     if rec.result else None,
                     rec.error),
                )
        except Exception as exc:                      # noqa: BLE001
            log.warning("任务状态落库失败（不影响任务本身）: %s", exc)

    def reap_zombies(self) -> int:
        """进程被 kill 时内存记录蒸发，DB 里会留下永远 running 的行。"""
        with connect(self.db_path) as conn, write_tx(conn):
            cur = conn.execute(
                "UPDATE tasks SET state='interrupted', "
                "finished_at=COALESCE(finished_at, ?) "
                "WHERE state IN ('running','pending')", (_now(),))
            return cur.rowcount


def _rec_log(self: TaskRecord, line: str) -> None:
    self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")


def _rec_should_stop(self: TaskRecord) -> bool:
    return self._cancel.is_set()


def _rec_on_event(self: TaskRecord, ev) -> None:
    if ev.phase:
        self.phase = ev.phase
    if ev.total:
        self.total = ev.total
    if ev.current:
        self.current = ev.current
    if ev.message:
        self.message = ev.message
        self.log(ev.message)


TaskRecord.log = _rec_log
TaskRecord.should_stop = _rec_should_stop
TaskRecord.on_event = _rec_on_event
