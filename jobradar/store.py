"""SQLite 落地。按 fingerprint 去重，保留首次见到和最后见到的时间。

连接策略见 db.py：每次操作开一个连接，不共享。
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from .db import connect, migrate, write_tx
from .models import Job

# 分批提交的两道闸门。纯按行数不行：百度 page_size=20，攒够 500 行要 37 秒
# 才提交一次，进度条卡住、崩溃损失也大；腾讯 100/页只要 7.5 秒。
# 加一条时间闸门，快慢爬虫都平顺。
BATCH_ROWS = 500
BATCH_SECONDS = 5.0

_INSERT_SQL = """
INSERT INTO jobs (fingerprint, company, job_id, title,
    responsibility, requirement, cities, department, category,
    education, work_years, publish_date, url, track, recruit_type,
    first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(fingerprint) DO UPDATE SET
    last_seen = excluded.last_seen,
    responsibility = excluded.responsibility,
    requirement = excluded.requirement,
    publish_date = excluded.publish_date
RETURNING (first_seen = ?) AS is_new
"""


def _row(job: Job, now: str) -> tuple:
    return (
        job.fingerprint, job.company, job.job_id, job.title,
        job.responsibility, job.requirement,
        json.dumps(job.cities, ensure_ascii=False),
        job.department, job.category, job.education, job.work_years,
        job.publish_date, job.url, job.track, job.recruit_type, now, now,
        now,                      # RETURNING 里比较 first_seen 用
    )


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        from .config import settings
        self.path = Path(path) if path is not None else settings.db_path
        migrate(self.path)

    # 保留签名，调用方无需改动；连接已不再长驻，无需真的关
    def close(self) -> None:
        return None

    def upsert(self, jobs: Iterable[Job], now: str) -> tuple[int, int]:
        """流式写入，分批提交。返回 (处理条数, 新增条数)。

        接受生成器：边抓边写，不把整家公司的岗位物化进内存，
        且中途异常时已提交的批次保留。
        """
        fetched = inserted = 0
        with connect(self.path) as conn:
            batch: list[tuple] = []
            last_commit = time.monotonic()

            def flush() -> int:
                nonlocal batch, last_commit
                if not batch:
                    return 0
                new = 0
                with write_tx(conn):
                    for params in batch:
                        r = conn.execute(_INSERT_SQL, params).fetchone()
                        if r and r["is_new"]:
                            new += 1
                batch = []
                last_commit = time.monotonic()
                return new

            try:
                for job in jobs:
                    fetched += 1
                    batch.append(_row(job, now))
                    if (len(batch) >= BATCH_ROWS
                            or time.monotonic() - last_commit >= BATCH_SECONDS):
                        inserted += flush()
            finally:
                # 生成器中途抛异常时，已攒下的这批也要落库
                inserted += flush()
        return fetched, inserted

    def log_run(self, started_at: str, company: str, fetched: int,
                inserted: int, note: str = "", session_id: str | None = None,
                slug: str | None = None) -> None:
        with connect(self.path) as conn:
            conn.execute(
                "INSERT INTO runs (started_at, company, fetched, inserted,"
                " note, session_id, slug) VALUES (?,?,?,?,?,?,?)",
                (started_at, company, fetched, inserted, note,
                 session_id, slug),
            )

    def query(self, tracks: list[str] | None = None,
              companies: list[str] | None = None,
              recruit_types: list[str] | None = None,
              limit: int | None = None,
              offset: int = 0,
              balanced: bool = False) -> list[dict]:
        """balanced=True 时按公司轮转取样。

        默认按发布时间倒序，但岗位数在各公司间极不均衡（字节占 70%），
        直接 LIMIT 会让样本变成"字节招聘分析"，小公司一条都取不到。
        轮转取样让每家按名次交替入选：各家第 1 条、各家第 2 条……
        """
        where = "WHERE 1=1"
        params: list = []
        if tracks:
            where += f" AND track IN ({','.join('?' * len(tracks))})"
            params += tracks
        if companies:
            where += f" AND company IN ({','.join('?' * len(companies))})"
            params += companies
        if recruit_types:
            where += (f" AND recruit_type IN "
                      f"({','.join('?' * len(recruit_types))})")
            params += recruit_types

        if balanced:
            sql = f"""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY company
                        ORDER BY publish_date DESC, fingerprint
                    ) AS rn
                    FROM jobs {where}
                )
                ORDER BY rn, company
            """
        else:
            sql = f"SELECT * FROM jobs {where} ORDER BY publish_date DESC, company"

        if limit:
            sql += " LIMIT ?"
            params.append(limit)
            if offset:
                sql += " OFFSET ?"
                params.append(offset)
        with connect(self.path, readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("rn", None)
            d["cities"] = json.loads(d["cities"] or "[]")
            out.append(d)
        return out

    def iter_fingerprints(self, tracks: list[str] | None = None,
                          companies: list[str] | None = None,
                          recruit_types: list[str] | None = None,
                          limit: int | None = None,
                          balanced: bool = False) -> list[str]:
        """只取 fingerprint，用于 analyze 预览估算。

        复用 query 的取样逻辑：两处各写一遍的话，预览说的调用次数
        会和实际分析对不上。
        """
        return [r["fingerprint"] for r in self.query(
            tracks=tracks, companies=companies, recruit_types=recruit_types,
            limit=limit, balanced=balanced)]

    def cached_skills(self, fingerprints: list[str]) -> dict[str, dict]:
        """取已抽取过的结果，键为 fingerprint。"""
        if not fingerprints:
            return {}
        out: dict[str, dict] = {}
        with connect(self.path, readonly=True) as conn:
            # 避免超过 SQLite 变量上限，分块查
            for i in range(0, len(fingerprints), 500):
                chunk = fingerprints[i:i + 500]
                sql = (f"SELECT * FROM skills WHERE fingerprint IN "
                       f"({','.join('?' * len(chunk))})")
                for r in conn.execute(sql, chunk):
                    out[r["fingerprint"]] = {
                        "fingerprint": r["fingerprint"],
                        "level": r["level"] or "不明",
                        "years": r["years"] or "",
                        "hard_skills": json.loads(r["hard_skills"] or "[]"),
                        "domains": json.loads(r["domains"] or "[]"),
                        "signals": json.loads(r["signals"] or "[]"),
                    }
        return out

    def fingerprints_with_jd(self, company: str | None = None) -> set[str]:
        """已经有 JD 正文的岗位指纹。

        一次性取回来做成 set，让详情补全的判断变成内存查找 ——
        逐条查库会退化成每个岗位一次 SQL。
        """
        sql = ("SELECT fingerprint FROM jobs WHERE "
               "COALESCE(responsibility,'') || COALESCE(requirement,'') != ''")
        params: list = []
        if company:
            sql += " AND company = ?"
            params.append(company)
        with connect(self.path, readonly=True) as conn:
            return {r["fingerprint"] for r in conn.execute(sql, params)}

    def count_cached(self, fingerprints: list[str]) -> int:
        if not fingerprints:
            return 0
        total = 0
        with connect(self.path, readonly=True) as conn:
            for i in range(0, len(fingerprints), 500):
                chunk = fingerprints[i:i + 500]
                sql = (f"SELECT COUNT(*) c FROM skills WHERE fingerprint IN "
                       f"({','.join('?' * len(chunk))})")
                total += conn.execute(sql, chunk).fetchone()["c"]
        return total

    def save_skills(self, rows: list[dict], model: str, now: str) -> int:
        """每批抽完立刻落盘，配额中断时已抽的部分不丢。"""
        if not rows:
            return 0
        payload = [(
            r["fingerprint"], r.get("level") or "不明", r.get("years") or "",
            json.dumps(r.get("hard_skills") or [], ensure_ascii=False),
            json.dumps(r.get("domains") or [], ensure_ascii=False),
            json.dumps(r.get("signals") or [], ensure_ascii=False),
            model, now,
        ) for r in rows if r.get("fingerprint")]
        if not payload:
            return 0
        with connect(self.path) as conn, write_tx(conn):
            conn.executemany(
                """
                INSERT INTO skills (fingerprint, level, years, hard_skills,
                    domains, signals, model, extracted_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    level=excluded.level, years=excluded.years,
                    hard_skills=excluded.hard_skills,
                    domains=excluded.domains, signals=excluded.signals,
                    model=excluded.model, extracted_at=excluded.extracted_at
                """,
                payload,
            )
        return len(payload)

    def counts(self) -> dict:
        with connect(self.path, readonly=True) as conn:
            total = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
            cached = conn.execute(
                "SELECT COUNT(*) c FROM skills").fetchone()["c"]
            by_track = {
                r["track"]: r["c"]
                for r in conn.execute(
                    "SELECT track, COUNT(*) c FROM jobs GROUP BY track")
            }
            by_company = {
                r["company"]: r["c"]
                for r in conn.execute(
                    "SELECT company, COUNT(*) c FROM jobs GROUP BY company")
            }
            by_recruit = {
                r["recruit_type"]: r["c"]
                for r in conn.execute(
                    "SELECT recruit_type, COUNT(*) c FROM jobs"
                    " GROUP BY recruit_type")
            }
        return {"total": total, "cached": cached,
                "by_track": by_track, "by_company": by_company,
                "by_recruit": by_recruit}
