"""数据库连接与迁移。

连接策略：每次操作开一个新连接，不共享、不做池。
实测 connect+close 约 17µs，而最慢的聚合查询 5.2ms —— 开销是查询的 0.3%。

为什么不共享连接：Python 3.10 默认 isolation_level="" 走隐式事务，
共享连接等于共享事务上下文。实测线程 A INSERT 未提交时，线程 B 的 COMMIT
会把 A 的行一起提交，A 再 ROLLBACK 也回滚不掉。这不是加
check_same_thread=False 能解决的问题。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

# 现有三张表：与线上库保持一致，一列不动
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint   TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    job_id        TEXT,
    title         TEXT NOT NULL,
    responsibility TEXT,
    requirement   TEXT,
    cities        TEXT,
    department    TEXT,
    category      TEXT,
    education     TEXT,
    work_years    TEXT,
    publish_date  TEXT,
    url           TEXT,
    track         TEXT,
    recruit_type  TEXT NOT NULL DEFAULT 'social',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_track   ON jobs(track);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    company    TEXT NOT NULL,
    fetched    INTEGER NOT NULL,
    inserted   INTEGER NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS skills (
    fingerprint  TEXT PRIMARY KEY,
    level        TEXT,
    years        TEXT,
    hard_skills  TEXT,
    domains      TEXT,
    signals      TEXT,
    model        TEXT,
    extracted_at TEXT NOT NULL
);

-- 报告存库而非存文件：历史列表要排序分页、对比要同时取两份、
-- 覆盖率快照要跟报告绑定。存文件就得 glob 文件名再解析。
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    task_id         TEXT,
    status          TEXT NOT NULL,      -- ok | partial | failed
    model           TEXT,
    tracks          TEXT NOT NULL,      -- JSON array
    recruit_types   TEXT,               -- JSON array，报告的招聘类型口径
    companies       TEXT,               -- JSON array | NULL
    requested_limit INTEGER,
    jobs_selected   INTEGER NOT NULL,
    extracted_ok    INTEGER NOT NULL,
    cache_hits      INTEGER NOT NULL,
    llm_calls       INTEGER NOT NULL,
    -- 快照而非实时 join：报告是某一时刻的判断，两周后覆盖率涨了，
    -- 旧报告仍应显示它当时的样本口径
    jobs_total      INTEGER NOT NULL,
    skills_total    INTEGER NOT NULL,
    stats_json      TEXT NOT NULL,
    meta_json       TEXT NOT NULL,
    markdown        TEXT,
    error           TEXT,
    duration_s      REAL
);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    state        TEXT NOT NULL,
    trigger      TEXT NOT NULL,          -- manual | schedule
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    params_json  TEXT,
    result_json  TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                  -- JSON
);
"""

# runs 表补列：原来靠 started_at 字符串把三家公司归为一次抓取，
# 两次抓取同秒启动就会串；company 存中文名也跟 API 的 slug 参数对不上。
_RUNS_NEW_COLUMNS = [
    ("session_id", "TEXT"),
    ("slug", "TEXT"),
]

# jobs 表补列：加入招聘类型（社招/校招/实习）。
# 已有数据全是社招，DEFAULT 'social' 正好把它们标对。
_JOBS_NEW_COLUMNS = [
    ("recruit_type", "TEXT NOT NULL DEFAULT 'social'"),
]

# reports 表补列：记录这份报告是基于哪种招聘类型出的，
# 不记的话历史报告看不出口径，校招报告和社招报告混在一起没法比。
_REPORTS_NEW_COLUMNS = [
    ("recruit_types", "TEXT"),
]


@contextmanager
def connect(path: str | Path, *, readonly: bool = False,
            timeout: float = 15.0):
    """开一个连接，用完即关。"""
    p = Path(path)
    uri = f"file:{p.as_posix()}" + ("?mode=ro" if readonly else "")
    conn = sqlite3.connect(uri, uri=True, timeout=timeout,
                           isolation_level=None)   # autocommit，边界自己控
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    if not readonly:
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下足够安全
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """写事务。

    必须 BEGIN IMMEDIATE 而不是默认的 DEFERRED：WAL 下 DEFERRED 事务
    先读后写时要做锁升级，若期间别人已写入会立刻拿到 SQLITE_BUSY，
    而且 busy_timeout 对这种情况无效（快照已过期，退避也没用）。
    IMMEDIATE 一上来就取写锁，busy_timeout 才真正生效。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(path: str | Path) -> dict:
    """建表 + 切 WAL + 补列。幂等，可反复调用。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    info = {"journal_mode": None, "added_columns": []}
    with connect(p) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            # 持久属性，设一次即可；写事务期间不能切，所以放在最前
            conn.execute("PRAGMA journal_mode=WAL")
        info["journal_mode"] = conn.execute(
            "PRAGMA journal_mode").fetchone()[0]
        conn.execute("PRAGMA wal_autocheckpoint=1000")   # 1000 页 ≈ 4MB
        conn.executescript(SCHEMA)

        existing = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        for col, coltype in _RUNS_NEW_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {coltype}")
                info["added_columns"].append(f"runs.{col}")

        job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        needs_refingerprint = "recruit_type" not in job_cols
        for col, coltype in _JOBS_NEW_COLUMNS:
            if col not in job_cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
                info["added_columns"].append(f"jobs.{col}")
        # 索引必须在加列之后建，不能写进 SCHEMA —— 老库里还没有这一列
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_recruit"
                     " ON jobs(recruit_type)")

        rep_cols = {r["name"]
                    for r in conn.execute("PRAGMA table_info(reports)")}
        for col, coltype in _REPORTS_NEW_COLUMNS:
            if col not in rep_cols:
                conn.execute(f"ALTER TABLE reports ADD COLUMN {col} {coltype}")
                info["added_columns"].append(f"reports.{col}")

    # 加了 recruit_type 之后 fingerprint 的算法变了，已有行必须重算，
    # 否则下次抓取会把同一个岗位当成新岗位插一遍。
    if needs_refingerprint:
        info["refingerprinted"] = _refingerprint(p)
    return info


def _refingerprint(path) -> int:
    """重算全部 fingerprint，并把 skills 缓存迁移到新指纹上。

    缓存是花钱抽出来的，绝不能丢：先建 旧→新 的映射，再整表改写。
    """
    import json as _json

    from .models import make_fingerprint

    with connect(path) as conn:
        rows = conn.execute(
            "SELECT fingerprint, company, title, cities, recruit_type"
            " FROM jobs").fetchall()
        mapping: dict[str, str] = {}
        for r in rows:
            try:
                cities = _json.loads(r["cities"] or "[]")
            except ValueError:
                cities = []
            new = make_fingerprint(r["company"], r["title"], cities,
                                   r["recruit_type"] or "social")
            if new != r["fingerprint"]:
                mapping[r["fingerprint"]] = new
        if not mapping:
            return 0

        with write_tx(conn):
            # 先搬缓存，再改 jobs：反过来的话映射就找不到源了
            for old, new in mapping.items():
                conn.execute("UPDATE OR IGNORE skills SET fingerprint=?"
                             " WHERE fingerprint=?", (new, old))
                conn.execute("UPDATE OR IGNORE jobs SET fingerprint=?"
                             " WHERE fingerprint=?", (new, old))
        return len(mapping)
