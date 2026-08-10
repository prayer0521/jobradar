"""采集器基类与注册表。"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator

from ..http import Http
from ..models import RECRUIT_SOCIAL, Job

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type["Spider"]] = {}


def register(cls: type["Spider"]) -> type["Spider"]:
    _REGISTRY[cls.slug] = cls
    return cls


def get_spiders(slugs: list[str] | None = None) -> list[type["Spider"]]:
    if not slugs:
        return list(_REGISTRY.values())
    missing = [s for s in slugs if s not in _REGISTRY]
    if missing:
        raise KeyError(f"未知采集器: {missing}；可用: {sorted(_REGISTRY)}")
    return [_REGISTRY[s] for s in slugs]


def all_slugs() -> list[str]:
    return sorted(_REGISTRY)


class Spider(ABC):
    slug: str = ""
    company: str = ""
    page_size: int = 20

    # 列表接口不含 JD 正文时置 True（MokaHR、飞书招聘等平台常见）。
    # crawl() 会对需要补全的岗位逐条调 fetch_detail。
    needs_detail: bool = False

    # 这个采集器支持抓哪些招聘类型。抓取时按需求与之取交集，
    # 交集为空就跳过这家（比如只有社招接口的站点，抓校招时直接跳过）。
    supports: tuple[str, ...] = (RECRUIT_SOCIAL,)

    # 本次实际要抓的类型，由构造参数覆盖
    recruit_types: tuple[str, ...] = (RECRUIT_SOCIAL,)

    def __init__(self, http: Http, max_pages: int = 30,
                 recruit_types: tuple[str, ...] | None = None) -> None:
        self.http = http
        self.max_pages = max_pages
        if recruit_types:
            self.recruit_types = tuple(recruit_types)
        # 页级异常在 crawl() 内被吞掉（单页失败不该拖垮整家），
        # 但调用方需要知道这一轮是正常抓完还是中断了，否则 runs 表
        # 会把中断记成成功。
        self.last_error: str | None = None
        self.detail_fetched = 0
        self.detail_skipped = 0

    @abstractmethod
    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        """返回 (本页岗位, 是否还有下一页)。"""

    def fetch_detail(self, job: Job) -> Job:
        """补全单条岗位的 JD 正文。needs_detail 为真时必须覆写。

        返回补全后的 Job（可以原地改再返回）。抛异常会被 crawl() 吞掉，
        该岗位保留列表里已有的字段继续入库。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 声明了 needs_detail 但没实现 fetch_detail")

    def crawl(self, should_enrich: Callable[[Job], bool] | None = None
              ) -> Iterator[Job]:
        """逐页抓取。

        should_enrich: 判断某个岗位是否需要拉详情。默认全拉。
          调用方（service 层）会传一个查库的谓词，让已有 JD 的岗位直接跳过 ——
          逐条拉详情很慢，几千个岗位按 1.5 秒间隔要几十分钟，
          只对新岗位做是唯一可行的方式。
        """
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            try:
                jobs, has_more = self.fetch_page(page)
            except Exception as exc:                      # 单页失败不该拖垮整家
                log.warning("[%s] 第 %s 页失败: %s", self.slug, page, exc)
                self.last_error = str(exc)
                break
            if not jobs:
                break
            new = 0
            for job in jobs:
                if job.fingerprint in seen:
                    continue
                seen.add(job.fingerprint)
                new += 1
                if self.needs_detail:
                    job = self._maybe_detail(job, should_enrich)
                yield job
            log.info("[%s] 第 %s 页 +%s 条 (累计 %s)", self.slug, page, new, len(seen))
            if not has_more:
                break
        if self.needs_detail:
            log.info("[%s] 详情补全 %s 条，跳过 %s 条（已有 JD）",
                     self.slug, self.detail_fetched, self.detail_skipped)

    def _maybe_detail(self, job: Job,
                      should_enrich: Callable[[Job], bool] | None) -> Job:
        if should_enrich is not None and not should_enrich(job):
            self.detail_skipped += 1
            return job
        try:
            enriched = self.fetch_detail(job) or job
        except Exception as exc:                          # noqa: BLE001
            # 详情失败不丢岗位：标题、城市这些列表里已有的字段仍然有用
            log.warning("[%s] 详情失败 %s: %s", self.slug, job.title[:30], exc)
            return job
        self.detail_fetched += 1
        return enriched
