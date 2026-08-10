"""编排层：CLI 和 API 共用的业务流程。

铁律：这个模块里不出现 print。所有对外表达走 on_event 回调，
由调用方决定是打到终端还是塞进任务进度。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from .http import Http
from .models import RECRUIT_SOCIAL
from .spiders import get_spiders
from .store import Store

log = logging.getLogger(__name__)

OnEvent = Callable[["Event"], None]
ShouldStop = Callable[[], bool]


@dataclass(frozen=True)
class Event:
    kind: str            # spider_start | page | spider_done | batch | warn | phase
    phase: str = ""
    current: int = 0
    total: int = 0
    message: str = ""


def _emit(on_event: OnEvent | None, ev: Event) -> None:
    if on_event is not None:
        on_event(ev)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 抓取


@dataclass
class CompanyResult:
    slug: str
    company: str
    fetched: int
    inserted: int
    error: str | None = None


@dataclass
class CrawlResult:
    started_at: str
    session_id: str
    duration_s: float
    companies: list[CompanyResult] = field(default_factory=list)
    total_fetched: int = 0
    total_inserted: int = 0
    counts: dict = field(default_factory=dict)
    cancelled: bool = False


def _guarded(spider, errbox: dict, on_event: OnEvent | None,
             should_stop: ShouldStop | None) -> Iterator:
    """包一层：spider 中途挂掉时不再向上抛，让已抓到的部分照常落库。

    这改变了失败语义——原来是全有或全无，现在是部分成功。
    代价是 runs 表会出现 fetched>0 且 note=failed 的行。
    """
    count = 0
    try:
        for job in spider.crawl(should_enrich=errbox.get("should_enrich")):
            if should_stop is not None and should_stop():
                errbox["cancelled"] = True
                return
            count += 1
            if count % 20 == 0:
                _emit(on_event, Event("page", phase=spider.company,
                                      current=count,
                                      message=f"{spider.company} 已抓 {count} 条"))
            yield job
    except Exception as exc:                        # noqa: BLE001
        errbox["error"] = str(exc)
        log.warning("[%s] 采集中断: %s", spider.slug, exc)
    finally:
        # crawl() 会吞掉页级异常，中断信息只留在 last_error 里
        if not errbox.get("error") and getattr(spider, "last_error", None):
            errbox["error"] = spider.last_error


def run_crawl(db: str | Path | None = None, *,
              companies: list[str] | None = None,
              max_pages: int = 30,
              interval: float = 1.5,
              recruit_types: tuple[str, ...] | list[str] | None = None,
              on_event: OnEvent | None = None,
              should_stop: ShouldStop | None = None,
              shared_limiter: bool = False) -> CrawlResult:
    t0 = time.monotonic()
    store = Store(db)
    now = _now()
    session_id = uuid.uuid4().hex[:12]
    result = CrawlResult(started_at=now, session_id=session_id, duration_s=0.0)
    wanted = tuple(recruit_types) if recruit_types else (RECRUIT_SOCIAL,)

    with Http(min_interval=interval, shared=shared_limiter) as http:
        for cls in get_spiders(companies):
            if should_stop is not None and should_stop():
                result.cancelled = True
                break

            # 这家支持的类型和本次要抓的类型没交集就跳过，
            # 免得白跑一趟又把社招数据当成校招入库
            usable = tuple(t for t in wanted if t in cls.supports)
            if not usable:
                log.info("[%s] 不支持 %s，跳过", cls.slug, "/".join(wanted))
                continue

            spider = cls(http, max_pages=max_pages, recruit_types=usable)
            _emit(on_event, Event("spider_start", phase=spider.company,
                                  message=spider.company))

            errbox: dict = {}
            if getattr(spider, "needs_detail", False):
                # 一次性取回该公司已有 JD 的指纹，让"要不要拉详情"变成
                # 内存判断。逐条拉详情很慢（几千个岗位要几十分钟），
                # 只对新岗位做。
                have_jd = store.fingerprints_with_jd(spider.company)
                errbox["should_enrich"] = lambda j: j.fingerprint not in have_jd
                _emit(on_event, Event(
                    "phase", phase=spider.company,
                    message=f"{spider.company} 需逐条取 JD，已有 {len(have_jd)} 条可跳过"))

            fetched, inserted = store.upsert(
                _guarded(spider, errbox, on_event, should_stop), now)

            err = errbox.get("error")
            note = f"failed: {err}" if err else ""
            store.log_run(now, spider.company, fetched, inserted, note,
                          session_id=session_id, slug=spider.slug)
            result.companies.append(CompanyResult(
                slug=spider.slug, company=spider.company,
                fetched=fetched, inserted=inserted, error=err))
            result.total_fetched += fetched
            result.total_inserted += inserted
            _emit(on_event, Event("spider_done", phase=spider.company,
                                  current=fetched, total=inserted,
                                  message=spider.company))
            if errbox.get("cancelled"):
                result.cancelled = True
                break

    result.counts = store.counts()
    result.duration_s = round(time.monotonic() - t0, 2)
    store.close()
    return result


# ---------------------------------------------------------------- 分析


class AnalyzeStatus(str, Enum):
    OK = "ok"
    NO_JOBS = "no_jobs"
    LLM_CONFIG = "llm_config"
    EXTRACT_FAILED = "extract_failed"
    REPORT_FAILED = "report_failed"
    PARTIAL = "partial"


@dataclass
class Coverage:
    jobs_total: int
    skills_total: int
    pct: float
    is_thin: bool
    hint: str = ""


@dataclass
class AnalyzeResult:
    status: AnalyzeStatus
    report_id: int | None = None
    markdown: str | None = None
    stats: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    jobs_selected: int = 0
    extracted_ok: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    coverage: Coverage | None = None
    error: str | None = None
    stats_path: str | None = None
    out_path: str | None = None
    duration_s: float = 0.0
    model: str | None = None
    cancelled: bool = False


def compute_coverage(counts: dict) -> Coverage:
    total = counts.get("total", 0) or 0
    cached = counts.get("cached", 0) or 0
    pct = round(cached / total * 100, 2) if total else 0.0
    thin = pct < 5.0
    hint = ""
    if thin and total:
        hint = (f"仅分析了 {cached}/{total} 个岗位（{pct}%），"
                f"结论不能代表完整行业情况")
    return Coverage(jobs_total=total, skills_total=cached, pct=pct,
                    is_thin=thin, hint=hint)


def preview_analyze(db: str | Path | None = None, *,
                    tracks: list[str] | None = None,
                    companies: list[str] | None = None,
                    recruit_types: list[str] | None = None,
                    limit: int = 150,
                    batch_size: int = 15,
                    balanced: bool = True) -> dict:
    """点按钮之前先告诉用户这一次要烧多少次 LLM 调用。"""
    store = Store(db)
    fps = store.iter_fingerprints(tracks=tracks, companies=companies,
                                  recruit_types=recruit_types,
                                  limit=limit, balanced=balanced)
    cached = store.count_cached(fps)
    to_extract = len(fps) - cached
    batches = -(-to_extract // batch_size) if to_extract else 0
    counts = store.counts()
    cov = compute_coverage(counts)
    after = counts["total"] and round(
        (counts["cached"] + to_extract) / counts["total"] * 100, 2) or 0.0

    warning = None
    if to_extract == 0 and fps:
        # store.query 是 ORDER BY publish_date DESC LIMIT N，重复点会选到同一批
        warning = ("这批岗位已全部分析过，本次不会有新增。"
                   "想提高覆盖率请调大 limit。")
    store.close()
    return {
        "selected": len(fps),
        "already_cached": cached,
        "to_extract": to_extract,
        "estimated_batches": batches,
        "plus_report_call": 1 if fps else 0,
        "estimated_llm_calls": batches + (1 if fps else 0),
        "coverage_now": cov.pct,
        "coverage_after": after,
        "warning": warning,
    }


def run_analyze(db: str | Path | None = None, *,
                tracks: list[str] | None = None,
                companies: list[str] | None = None,
                recruit_types: list[str] | None = None,
                limit: int = 150,
                batch_size: int = 15,
                balanced: bool = True,
                out: Path | None = None,
                llm_timeout: float | None = None,
                task_id: str | None = None,
                on_event: OnEvent | None = None,
                should_stop: ShouldStop | None = None) -> AnalyzeResult:
    from .analyze import aggregate, build_report, extract_skills
    from .llm import LLM, LLMError

    t0 = time.monotonic()
    tracks = tracks or ["backend", "ai"]
    store = Store(db)
    jobs = store.query(tracks=tracks, companies=companies,
                       recruit_types=recruit_types, limit=limit,
                       balanced=balanced)
    if not jobs:
        store.close()
        return AnalyzeResult(status=AnalyzeStatus.NO_JOBS,
                             duration_s=round(time.monotonic() - t0, 2))

    counts = store.counts()
    fps = [j["fingerprint"] for j in jobs]
    cache_hits = store.count_cached(fps)

    try:
        llm = LLM(timeout=llm_timeout) if llm_timeout else LLM()
    except LLMError as exc:
        store.close()
        return AnalyzeResult(status=AnalyzeStatus.LLM_CONFIG, error=str(exc),
                             duration_s=round(time.monotonic() - t0, 2))

    n_batches = -(-len(jobs) // batch_size)
    _emit(on_event, Event("phase", phase="extract", total=n_batches,
                          message=f"用 {llm.model} 分析 {len(jobs)} 个岗位"
                                  f"（分 {n_batches} 批）"))

    extracted = extract_skills(llm, jobs, batch_size=batch_size, store=store,
                               on_event=on_event, should_stop=should_stop)
    cancelled = bool(should_stop is not None and should_stop())

    if not extracted:
        llm.close(); store.close()
        return AnalyzeResult(status=AnalyzeStatus.EXTRACT_FAILED,
                             jobs_selected=len(jobs), cache_hits=cache_hits,
                             cancelled=cancelled,
                             duration_s=round(time.monotonic() - t0, 2))

    stats = aggregate(extracted)
    meta = {
        "抓取公司分布": counts["by_company"],
        "方向筛选": tracks or "全部",
        "库内总岗位数": counts["total"],
        "本次分析岗位数": len(jobs),
        "成功抽取条数": len(extracted),
        "分析模型": llm.model,
    }
    llm_calls = max(0, len(extracted) - cache_hits)
    llm_calls = -(-llm_calls // batch_size)

    stats_path = out_path = None
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        sp = out.with_suffix(".stats.json")
        # 先存频次数据：哪怕下一步生成报告失败，统计结果也不白跑
        sp.write_text(json.dumps({"meta": meta, "stats": stats},
                                 ensure_ascii=False, indent=2),
                      encoding="utf-8")
        stats_path = str(sp)

    _emit(on_event, Event("phase", phase="report", message="汇总生成报告"))
    try:
        markdown = build_report(llm, stats, meta)
    except Exception as exc:                        # noqa: BLE001
        # 同样用抽取后的计数：报告虽然没生成，抽取结果已经落库了
        after = store.counts()
        rid = _save_report(store, "failed", meta, stats, None, str(exc),
                           after, tracks, companies, limit, len(jobs),
                           len(extracted), cache_hits, llm_calls, llm.model,
                           task_id, round(time.monotonic() - t0, 2),
                           recruit_types=recruit_types)
        llm.close(); store.close()
        return AnalyzeResult(status=AnalyzeStatus.REPORT_FAILED, report_id=rid,
                             stats=stats, meta=meta, error=str(exc),
                             jobs_selected=len(jobs), extracted_ok=len(extracted),
                             cache_hits=cache_hits, llm_calls=llm_calls,
                             stats_path=stats_path, model=llm.model,
                             coverage=compute_coverage(after),
                             cancelled=cancelled,
                             duration_s=round(time.monotonic() - t0, 2))

    if out is not None:
        Path(out).write_text(markdown, encoding="utf-8")
        out_path = str(out)

    partial = len(extracted) < len(jobs)
    status = AnalyzeStatus.PARTIAL if partial else AnalyzeStatus.OK
    duration = round(time.monotonic() - t0, 2)
    # 快照要用抽取完成后的计数：报告反映的是它写入那一刻的库状态，
    # 用分析前的数字会把本次自己贡献的那些抽取结果漏掉。
    final_counts = store.counts()
    rid = _save_report(store, "partial" if partial else "ok", meta, stats,
                       markdown, None, final_counts, tracks, companies, limit,
                       len(jobs), len(extracted), cache_hits, llm_calls,
                       llm.model, task_id, duration,
                       recruit_types=recruit_types)

    llm.close(); store.close()
    return AnalyzeResult(status=status, report_id=rid, markdown=markdown,
                         stats=stats, meta=meta, jobs_selected=len(jobs),
                         extracted_ok=len(extracted), cache_hits=cache_hits,
                         llm_calls=llm_calls, model=llm.model,
                         coverage=compute_coverage(final_counts),
                         stats_path=stats_path, out_path=out_path,
                         cancelled=cancelled, duration_s=duration)


def _save_report(store: Store, status: str, meta: dict, stats: dict,
                 markdown: str | None, error: str | None, counts: dict,
                 tracks, companies, limit, jobs_selected, extracted_ok,
                 cache_hits, llm_calls, model, task_id, duration,
                 recruit_types=None) -> int:
    from .db import connect, write_tx
    with connect(store.path) as conn, write_tx(conn):
        cur = conn.execute(
            """
            INSERT INTO reports (created_at, task_id, status, model, tracks,
                recruit_types, companies, requested_limit, jobs_selected,
                extracted_ok, cache_hits, llm_calls, jobs_total, skills_total,
                stats_json, meta_json, markdown, error, duration_s)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (_now(), task_id, status, model,
             json.dumps(tracks, ensure_ascii=False),
             json.dumps(recruit_types, ensure_ascii=False)
             if recruit_types else None,
             json.dumps(companies, ensure_ascii=False) if companies else None,
             limit, jobs_selected, extracted_ok, cache_hits, llm_calls,
             counts.get("total", 0), counts.get("cached", 0),
             json.dumps(stats, ensure_ascii=False),
             json.dumps(meta, ensure_ascii=False),
             markdown, error, duration),
        )
        return cur.lastrowid
