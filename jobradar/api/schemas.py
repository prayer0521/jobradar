"""API 响应模型。

一个约定：aggregate() 返回的是 list[tuple[str,int]]，JSON 化后是
[["LLM",15],...] 这种二元数组。它的输出会进 build_report 的 prompt，
改结构等于改模型输入，所以内部保持原样，只在这一层转成 NameCount。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class NameCount(BaseModel):
    name: str
    count: int


def pairs(rows) -> list[NameCount]:
    return [NameCount(name=str(n), count=int(c)) for n, c in (rows or [])]


def from_mapping(d: dict) -> list[NameCount]:
    return [NameCount(name=k, count=v)
            for k, v in sorted((d or {}).items(), key=lambda x: -x[1])]


# ---------------------------------------------------------------- 覆盖率

class TrackCoverage(BaseModel):
    track: str
    label: str
    total: int
    done: int
    pct: float


class Coverage(BaseModel):
    jobs_total: int
    skills_total: int
    pct: float
    is_thin: bool
    hint: str = ""
    by_track: list[TrackCoverage] = []


# ---------------------------------------------------------------- 总览

class CrawlSession(BaseModel):
    session_id: str | None = None
    started_at: str
    companies: list[str] = []
    fetched: int = 0
    inserted: int = 0
    failed: list[str] = []
    partial: bool = False


class ReportSummary(BaseModel):
    id: int
    created_at: str
    status: Literal["ok", "partial", "failed"]
    model: str | None = None
    tracks: list[str] = []
    recruit_types: list[str] = []
    jobs_selected: int = 0
    extracted_ok: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    coverage_at_time: float = 0.0
    duration_s: float | None = None
    error: str | None = None


class ScheduleConfigModel(BaseModel):
    enabled: bool = False
    hour: int = Field(3, ge=0, le=23)
    minute: int = Field(30, ge=0, le=59)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    companies: list[str] | None = None
    max_pages: int = Field(30, ge=1, le=200)


class ScheduleState(BaseModel):
    config: ScheduleConfigModel
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_result: str | None = None


class Overview(BaseModel):
    jobs_total: int
    by_recruit: list[NameCount] = []
    by_track: list[NameCount]
    by_company: list[NameCount]
    matrix: list[dict[str, Any]]
    publish_trend: list[NameCount]
    coverage: Coverage
    last_crawl: CrawlSession | None = None
    latest_report: ReportSummary | None = None
    scheduler: ScheduleState


# ---------------------------------------------------------------- 报告

class ReportDetail(ReportSummary):
    markdown: str | None = None
    top_skills: list[NameCount] = []
    top_domains: list[NameCount] = []
    top_signals: list[NameCount] = []
    levels: list[NameCount] = []
    skills_by_track: dict[str, list[NameCount]] = {}
    meta: dict = {}


class SkillDelta(BaseModel):
    name: str
    a: int
    b: int
    delta: int
    status: Literal["new", "gone", "up", "down", "flat"]


class ReportCompare(BaseModel):
    a: ReportSummary
    b: ReportSummary
    skills: list[SkillDelta]
    warning: str | None = None


# ---------------------------------------------------------------- 任务

class TaskProgress(BaseModel):
    phase: str = ""
    current: int = 0
    total: int = 0
    message: str = ""
    pct: float | None = None


class TaskView(BaseModel):
    id: str
    kind: Literal["crawl", "analyze"]
    state: Literal["pending", "running", "succeeded", "failed",
                   "cancelled", "interrupted"]
    trigger: Literal["manual", "schedule"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    params: dict = {}
    progress: TaskProgress | None = None
    logs: list[str] = []
    result: dict | None = None
    error: str | None = None


class CrawlRequest(BaseModel):
    companies: list[str] | None = None
    recruit_types: list[Literal["campus", "intern", "social"]] = [
        "campus", "intern"]
    max_pages: int = Field(30, ge=1, le=200)
    # 下限 0.5：再低就是对目标站施压了
    interval: float = Field(1.5, ge=0.5, le=10.0)


class AnalyzeRequest(BaseModel):
    tracks: list[Literal["backend", "ai", "other"]] = ["backend", "ai"]
    recruit_types: list[Literal["campus", "intern", "social"]] = [
        "campus", "intern"]
    # 按公司轮转取样。岗位数在各公司间极不均衡（字节占 70%），
    # 不均衡取样会让报告变成"字节招聘分析"
    balanced: bool = True
    companies: list[str] | None = None
    limit: int = Field(150, ge=1, le=1000)      # 硬上限护配额
    batch_size: int = Field(15, ge=1, le=50)
    # 前端回传 preview 的数字，不符则拒绝——防止「预览说 10 次、实际烧 40 次」
    confirm_llm_calls: int | None = None


class AnalyzePreview(BaseModel):
    selected: int
    already_cached: int
    to_extract: int
    estimated_batches: int
    plus_report_call: int
    estimated_llm_calls: int
    coverage_now: float
    coverage_after: float
    warning: str | None = None


class Meta(BaseModel):
    spiders: list[dict[str, str]]
    model: str | None = None
    db_path: str
    version: str
    llm_ready: bool
    llm_error: str | None = None
