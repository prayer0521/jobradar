"""API 路由。

长任务（抓取 3-7 分钟、分析可能几十分钟）一律走 TaskManager 后台线程，
端点只负责提交和查状态。端点本身全部是同步 def，跑在 anyio threadpool，
不会阻塞事件循环。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from .. import service
from ..scheduler import (
    ScheduleConfig, load_config, next_run_at, save_config,
)
from ..spiders import all_slugs, get_spiders
from ..store import Store
from ..tasks import TaskBusy
from . import queries as q
from .schemas import (
    AnalyzePreview, AnalyzeRequest, Coverage, CrawlRequest, CrawlSession, Meta,
    Overview, ReportCompare, ReportDetail, ReportSummary, ScheduleConfigModel,
    ScheduleState, TaskView, from_mapping, pairs,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _db(request: Request):
    return request.app.state.db_path


def _tm(request: Request):
    return request.app.state.tasks


def _schedule_state(request: Request) -> ScheduleState:
    cfg = load_config(_db(request))
    sch = getattr(request.app.state, "scheduler", None)
    nxt = sch.next_run if sch else next_run_at(cfg, datetime.now())
    return ScheduleState(
        config=ScheduleConfigModel(**cfg.__dict__),
        next_run_at=nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
        last_run_at=sch.last_run_at if sch else None,
        last_result=sch.last_result if sch else None,
    )


def _task_view(rec) -> TaskView:
    return TaskView(**rec.to_dict())


# ---------------------------------------------------------------- 总览

@router.get("/overview", response_model=Overview)
def overview(request: Request) -> Overview:
    db = _db(request)
    counts = Store(db).counts()
    cov = q.coverage_detail(db)
    sessions = q.crawl_sessions(db, limit=1)
    latest = q.get_report(db)
    return Overview(
        jobs_total=counts["total"],
        by_recruit=from_mapping(counts.get("by_recruit") or {}),
        by_track=from_mapping(counts["by_track"]),
        by_company=from_mapping(counts["by_company"]),
        matrix=q.track_company_matrix(db),
        publish_trend=pairs(q.publish_trend(db)),
        coverage=Coverage(**cov),
        last_crawl=CrawlSession(**sessions[0]) if sessions else None,
        latest_report=ReportSummary(**{
            k: v for k, v in latest.items()
            if k in ReportSummary.model_fields}) if latest else None,
        scheduler=_schedule_state(request),
    )


@router.get("/coverage", response_model=Coverage)
def coverage(request: Request) -> Coverage:
    return Coverage(**q.coverage_detail(_db(request)))


@router.get("/crawl-history", response_model=list[CrawlSession])
def crawl_history(request: Request,
                  limit: int = Query(20, ge=1, le=100)) -> list[CrawlSession]:
    return [CrawlSession(**s) for s in q.crawl_sessions(_db(request), limit)]


@router.get("/meta", response_model=Meta)
def meta(request: Request) -> Meta:
    from .. import __version__
    from ..llm import LLM, LLMError
    model = None
    err = None
    try:
        model = LLM().model
    except LLMError as exc:
        err = str(exc)
    return Meta(
        spiders=[{"slug": c.slug, "company": c.company}
                 for c in get_spiders(None)],
        model=model, db_path=str(_db(request)),
        version=__version__, llm_ready=err is None, llm_error=err,
    )


# ---------------------------------------------------------------- 报告

@router.get("/reports", response_model=list[ReportSummary])
def list_reports(request: Request, limit: int = Query(20, ge=1, le=100),
                 offset: int = Query(0, ge=0)) -> list[ReportSummary]:
    return [ReportSummary(**r)
            for r in q.list_reports(_db(request), limit, offset)]


def _detail(row: dict) -> ReportDetail:
    stats = row.pop("stats", {}) or {}
    return ReportDetail(
        **{k: v for k, v in row.items() if k in ReportDetail.model_fields},
        top_skills=pairs(stats.get("top_skills")),
        top_domains=pairs(stats.get("top_domains")),
        top_signals=pairs(stats.get("top_signals")),
        levels=pairs(stats.get("levels")),
        skills_by_track={k: pairs(v) for k, v
                         in (stats.get("skills_by_track") or {}).items()},
    )


@router.get("/reports/latest", response_model=ReportDetail)
def latest_report(request: Request) -> ReportDetail:
    row = q.get_report(_db(request))
    if row is None:
        raise HTTPException(404, "还没有任何报告，去任务页点一次分析")
    return _detail(row)


@router.get("/reports/compare", response_model=ReportCompare)
def compare_reports(request: Request, a: int, b: int) -> ReportCompare:
    db = _db(request)
    ra, rb = q.get_report(db, a), q.get_report(db, b)
    if ra is None or rb is None:
        raise HTTPException(404, "报告不存在")
    sa, sb = ra.get("stats") or {}, rb.get("stats") or {}
    warning = None
    na, nb = ra["jobs_selected"] or 1, rb["jobs_selected"] or 1
    ratio = max(na, nb) / min(na, nb)
    if ratio >= 2:
        # 频次是绝对值，样本量差一倍以上时直接比大小会误导
        warning = (f"两份报告样本量差异较大（{na} vs {nb}），"
                   f"技能频次的绝对值不可直接比较")
    from .schemas import SkillDelta
    return ReportCompare(
        a=ReportSummary(**{k: v for k, v in ra.items()
                           if k in ReportSummary.model_fields}),
        b=ReportSummary(**{k: v for k, v in rb.items()
                           if k in ReportSummary.model_fields}),
        skills=[SkillDelta(**d) for d in q.compare_skills(sa, sb)],
        warning=warning,
    )


@router.get("/reports/{report_id}", response_model=ReportDetail)
def get_report(request: Request, report_id: int) -> ReportDetail:
    row = q.get_report(_db(request), report_id)
    if row is None:
        raise HTTPException(404, "报告不存在")
    return _detail(row)


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: int) -> dict:
    if not q.delete_report(_db(request), report_id):
        raise HTTPException(404, "报告不存在")
    return {"deleted": report_id}


# ---------------------------------------------------------------- 任务

@router.get("/tasks/current")
def current_task(request: Request) -> TaskView | None:
    rec = _tm(request).current
    return _task_view(rec) if rec else None


@router.get("/tasks", response_model=list[TaskView])
def list_tasks(request: Request,
               limit: int = Query(30, ge=1, le=100)) -> list[TaskView]:
    return [_task_view(r) for r in _tm(request).recent(limit)]


@router.get("/tasks/{task_id}", response_model=TaskView)
def get_task(request: Request, task_id: str) -> TaskView:
    rec = _tm(request).get(task_id)
    if rec is None:
        raise HTTPException(404, "任务不存在或已被清出内存")
    return _task_view(rec)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(request: Request, task_id: str) -> dict:
    ok = _tm(request).cancel(task_id)
    if not ok:
        raise HTTPException(409, "任务不存在或已结束")
    return {"cancelling": task_id,
            "note": "协作式取消：抓取最多等一个请求超时，分析最多等一次模型调用"}


def _submit_crawl(app, *, companies=None, max_pages=30, interval=1.5,
                  recruit_types=None, trigger="manual"):
    db = app.state.db_path

    def job(rec):
        from ..config import settings
        from ..http import CrawlBusy, crawl_lock
        try:
            with crawl_lock(settings.crawl_lock):
                res = service.run_crawl(
                    db, companies=companies, max_pages=max_pages,
                    interval=interval, recruit_types=recruit_types,
                    on_event=rec.on_event,
                    should_stop=rec.should_stop, shared_limiter=True)
        except CrawlBusy as exc:
            raise RuntimeError(str(exc)) from None
        return {
            "total_fetched": res.total_fetched,
            "total_inserted": res.total_inserted,
            "duration_s": res.duration_s,
            "counts": res.counts,
            "companies": [c.__dict__ for c in res.companies],
            "cancelled": res.cancelled,
        }

    return app.state.tasks.submit(
        "crawl", job, trigger=trigger,
        params={"companies": companies, "max_pages": max_pages,
                "interval": interval, "recruit_types": recruit_types})


@router.post("/tasks/crawl", response_model=TaskView)
def start_crawl(request: Request, req: CrawlRequest) -> TaskView:
    if req.companies:
        try:
            get_spiders(req.companies)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from None
    try:
        rec = _submit_crawl(request.app, companies=req.companies,
                            max_pages=req.max_pages, interval=req.interval,
                            recruit_types=list(req.recruit_types))
    except TaskBusy as exc:
        raise HTTPException(409, {"message": "已有任务在运行",
                                  "current_task": exc.current_id}) from None
    return _task_view(rec)


@router.get("/analyze/preview", response_model=AnalyzePreview)
def analyze_preview(request: Request,
                    tracks: list[str] = Query(default=["backend", "ai"]),
                    companies: list[str] | None = Query(default=None),
                    recruit_types: list[str] = Query(
                        default=["campus", "intern"]),
                    limit: int = Query(150, ge=1, le=1000),
                    batch_size: int = Query(15, ge=1, le=50),
                    balanced: bool = Query(True)) -> AnalyzePreview:
    return AnalyzePreview(**service.preview_analyze(
        _db(request), tracks=tracks, companies=companies,
        recruit_types=recruit_types, limit=limit, batch_size=batch_size,
        balanced=balanced))


@router.post("/tasks/analyze", response_model=TaskView)
def start_analyze(request: Request, req: AnalyzeRequest) -> TaskView:
    db = _db(request)
    if req.companies:
        try:
            get_spiders(req.companies)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from None

    preview = service.preview_analyze(
        db, tracks=list(req.tracks), companies=req.companies,
        recruit_types=list(req.recruit_types), limit=req.limit,
        batch_size=req.batch_size, balanced=req.balanced)
    if preview["selected"] == 0:
        raise HTTPException(422, "没有匹配的岗位，先去抓取")
    # 乐观锁：库在预览后变了导致实际调用数不同，就要求重新确认
    if (req.confirm_llm_calls is not None
            and req.confirm_llm_calls != preview["estimated_llm_calls"]):
        raise HTTPException(409, {
            "message": "预计的模型调用次数已变化，请重新确认",
            "confirmed": req.confirm_llm_calls,
            "actual": preview["estimated_llm_calls"],
        })

    def job(rec):
        rec.log(f"预计 {preview['estimated_llm_calls']} 次模型调用")
        res = service.run_analyze(
            db, tracks=list(req.tracks), companies=req.companies,
            recruit_types=list(req.recruit_types), balanced=req.balanced,
            limit=req.limit, batch_size=req.batch_size,
            llm_timeout=120.0,          # 比 CLI 的 300s 短，让取消更快生效
            task_id=rec.id,
            on_event=rec.on_event, should_stop=rec.should_stop)
        if res.status in (service.AnalyzeStatus.NO_JOBS,
                          service.AnalyzeStatus.LLM_CONFIG,
                          service.AnalyzeStatus.EXTRACT_FAILED):
            raise RuntimeError(res.error or f"分析失败: {res.status.value}")
        return {
            "status": res.status.value, "report_id": res.report_id,
            "jobs_selected": res.jobs_selected, "extracted_ok": res.extracted_ok,
            "cache_hits": res.cache_hits, "llm_calls": res.llm_calls,
            "duration_s": res.duration_s,
            "coverage": res.coverage.__dict__ if res.coverage else None,
        }

    try:
        rec = _tm(request).submit("analyze", job,
                                  params=req.model_dump())
    except TaskBusy as exc:
        raise HTTPException(409, {"message": "已有任务在运行",
                                  "current_task": exc.current_id}) from None
    return _task_view(rec)


# ---------------------------------------------------------------- 定时

@router.get("/schedule", response_model=ScheduleState)
def get_schedule(request: Request) -> ScheduleState:
    return _schedule_state(request)


@router.put("/schedule", response_model=ScheduleState)
def put_schedule(request: Request, cfg: ScheduleConfigModel) -> ScheduleState:
    if cfg.companies:
        try:
            get_spiders(cfg.companies)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from None
    try:
        save_config(_db(request), ScheduleConfig(**cfg.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    sch = getattr(request.app.state, "scheduler", None)
    if sch:
        sch.refresh()
    return _schedule_state(request)


@router.get("/spiders")
def spiders() -> list[str]:
    return all_slugs()
