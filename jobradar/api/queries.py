"""数据聚合查询。给 API 端点用，不进 service（那里只放编排）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..db import connect

TRACK_LABELS = {"backend": "后端/服务端", "ai": "算法/AI", "other": "其他"}


def coverage_detail(db_path) -> dict:
    with connect(db_path, readonly=True) as conn:
        total = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
        rows = conn.execute("""
            SELECT j.track,
                   COUNT(*) total,
                   SUM(CASE WHEN s.fingerprint IS NOT NULL THEN 1 ELSE 0 END) done
            FROM jobs j LEFT JOIN skills s ON s.fingerprint = j.fingerprint
            GROUP BY j.track
        """).fetchall()

    by_track = []
    for r in rows:
        t = r["track"] or "other"
        d = r["done"] or 0
        by_track.append({
            "track": t, "label": TRACK_LABELS.get(t, t),
            "total": r["total"], "done": d,
            "pct": round(d / r["total"] * 100, 2) if r["total"] else 0.0,
        })
    by_track.sort(key=lambda x: -x["total"])

    pct = round(done / total * 100, 2) if total else 0.0
    thin = pct < 5.0
    hint = (f"仅分析了 {done}/{total} 个岗位（{pct}%），结论不能代表完整行业情况"
            if thin and total else "")
    return {"jobs_total": total, "skills_total": done, "pct": pct,
            "is_thin": thin, "hint": hint, "by_track": by_track}


def track_company_matrix(db_path) -> list[dict]:
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT company, track, COUNT(*) c FROM jobs "
            "GROUP BY company, track").fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        cell = out.setdefault(r["company"], {"company": r["company"]})
        cell[r["track"] or "other"] = r["c"]
    for cell in out.values():
        for t in TRACK_LABELS:
            cell.setdefault(t, 0)
        cell["total"] = sum(cell[t] for t in TRACK_LABELS)
    return sorted(out.values(), key=lambda x: -x["total"])


def publish_trend(db_path, days: int = 60) -> list[tuple[str, int]]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT publish_date d, COUNT(*) c FROM jobs "
            "WHERE publish_date >= ? AND publish_date <= ? "
            "GROUP BY publish_date ORDER BY publish_date", (since, today)
        ).fetchall()
    return [(r["d"], r["c"]) for r in rows]


def crawl_sessions(db_path, limit: int = 20) -> list[dict]:
    """按 session 聚合。老数据没有 session_id，降级用 started_at 分组。"""
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, id DESC "
            "LIMIT 400").fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        key = r["session_id"] or r["started_at"]
        g = groups.setdefault(key, {
            "session_id": r["session_id"], "started_at": r["started_at"],
            "companies": [], "fetched": 0, "inserted": 0, "failed": [],
            "partial": False,
        })
        g["companies"].append(r["company"])
        g["fetched"] += r["fetched"] or 0
        g["inserted"] += r["inserted"] or 0
        if (r["note"] or "").startswith("failed:"):
            g["failed"].append(r["company"])
            # 流式 upsert 后会出现「抓到一半才失败」，这是新语义
            if (r["fetched"] or 0) > 0:
                g["partial"] = True
    out = sorted(groups.values(), key=lambda x: x["started_at"], reverse=True)
    return out[:limit]


def _report_row_to_summary(r) -> dict:
    total = r["jobs_total"] or 0
    return {
        "id": r["id"], "created_at": r["created_at"], "status": r["status"],
        "model": r["model"],
        "tracks": json.loads(r["tracks"] or "[]"),
        "recruit_types": json.loads(
            (r["recruit_types"] if "recruit_types" in r.keys() else None)
            or "[]"),
        "jobs_selected": r["jobs_selected"], "extracted_ok": r["extracted_ok"],
        "cache_hits": r["cache_hits"], "llm_calls": r["llm_calls"],
        "coverage_at_time": round((r["skills_total"] or 0) / total * 100, 2)
        if total else 0.0,
        "duration_s": r["duration_s"], "error": r["error"],
    }


def list_reports(db_path, limit: int = 20, offset: int = 0) -> list[dict]:
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, model, tracks, recruit_types,"
            " jobs_selected,"
            " extracted_ok, cache_hits, llm_calls, jobs_total, skills_total,"
            " duration_s, error FROM reports"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
    return [_report_row_to_summary(r) for r in rows]


def get_report(db_path, report_id: int | None = None) -> dict | None:
    sql = "SELECT * FROM reports"
    params: tuple = ()
    if report_id is None:
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
    else:
        sql += " WHERE id=?"
        params = (report_id,)
    with connect(db_path, readonly=True) as conn:
        r = conn.execute(sql, params).fetchone()
    if r is None:
        return None
    d = _report_row_to_summary(r)
    stats = json.loads(r["stats_json"] or "{}")
    d.update({
        "markdown": r["markdown"],
        "meta": json.loads(r["meta_json"] or "{}"),
        "stats": stats,
    })
    return d


def delete_report(db_path, report_id: int) -> bool:
    from ..db import write_tx
    with connect(db_path) as conn, write_tx(conn):
        cur = conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
        return cur.rowcount > 0


def compare_skills(stats_a: dict, stats_b: dict) -> list[dict]:
    a = {n: c for n, c in (stats_a.get("top_skills") or [])}
    b = {n: c for n, c in (stats_b.get("top_skills") or [])}
    out = []
    for name in sorted(set(a) | set(b)):
        ca, cb = a.get(name, 0), b.get(name, 0)
        if ca and not cb:
            status = "gone"
        elif cb and not ca:
            status = "new"
        elif cb > ca:
            status = "up"
        elif cb < ca:
            status = "down"
        else:
            status = "flat"
        out.append({"name": name, "a": ca, "b": cb, "delta": cb - ca,
                    "status": status})
    out.sort(key=lambda x: -abs(x["delta"]))
    return out
