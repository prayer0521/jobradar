"""并发安全测试。

锁死一个真实复现过的 bug：共享 sqlite 连接时事务会串扰 ——
线程 A INSERT 未提交，线程 B 的 COMMIT 会把 A 的行一起提交，
A 再 ROLLBACK 也回滚不掉。防止将来有人「优化」回共享连接。
"""
from __future__ import annotations

import sqlite3
import threading

from jobradar.db import connect, write_tx
from jobradar.store import Store
from tests.conftest import make_job


class TestWAL:
    def test_迁移后启用WAL(self, db_path):
        with connect(db_path, readonly=True) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

    def test_写事务不阻塞读(self, db_path):
        """抓取进行中，仪表盘必须还能刷新。非 WAL 下这里会 database is locked。"""
        store = Store(db_path)
        store.upsert([make_job(i) for i in range(10)], "2026-08-01 00:00:00")

        read_ok = []
        with connect(db_path) as wconn, write_tx(wconn):
            wconn.execute(
                "INSERT INTO jobs (fingerprint, company, title,"
                " first_seen, last_seen) VALUES ('zzz','x','y','t','t')")

            def reader():
                try:
                    with connect(db_path, readonly=True) as rconn:
                        rconn.execute("SELECT COUNT(*) FROM jobs").fetchone()
                    read_ok.append(True)
                except Exception as exc:      # noqa: BLE001
                    read_ok.append(exc)

            t = threading.Thread(target=reader)
            t.start()
            t.join(timeout=10)

        assert read_ok == [True], f"写事务期间读失败: {read_ok}"


class TestConcurrency:
    def test_多线程读写不报错也不丢数据(self, db_path):
        store = Store(db_path)
        errors: list[Exception] = []

        def writer(base: int):
            try:
                store.upsert([make_job(base + i) for i in range(50)],
                             "2026-08-01 00:00:00")
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(30):
                    store.counts()
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i * 100,))
                   for i in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"并发出错: {errors[:3]}"
        assert store.counts()["total"] == 200      # 4 写 × 50，无丢失

    def test_回滚不影响其他线程(self, db_path):
        """共享连接会让 B 的 commit 提交 A 的未提交行。独立连接不会。"""
        def worker(n: int, do_rollback: bool):
            with connect(db_path) as conn:
                try:
                    with write_tx(conn):
                        conn.execute(
                            "INSERT INTO jobs (fingerprint, company, title,"
                            " first_seen, last_seen) VALUES (?,?,?,?,?)",
                            (f"fp{n}", "c", f"t{n}", "x", "x"))
                        if do_rollback:
                            raise RuntimeError("故意回滚")
                except RuntimeError:
                    pass

        t1 = threading.Thread(target=worker, args=(1, True))
        t2 = threading.Thread(target=worker, args=(2, False))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        with connect(db_path, readonly=True) as conn:
            fps = {r["fingerprint"]
                   for r in conn.execute("SELECT fingerprint FROM jobs")}
        assert "fp1" not in fps, "回滚的行不该落库"
        assert "fp2" in fps, "已提交的行必须在"


class TestConnection:
    def test_只读连接禁止写(self, db_path):
        with connect(db_path, readonly=True) as conn:
            try:
                conn.execute(
                    "INSERT INTO jobs (fingerprint, company, title,"
                    " first_seen, last_seen) VALUES ('x','x','x','x','x')")
            except sqlite3.OperationalError:
                return
            raise AssertionError("只读连接不该允许写入")

    def test_autocommit关掉了隐式事务(self, db_path):
        with connect(db_path) as conn:
            assert conn.isolation_level is None
