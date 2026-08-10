"""百度 talent.baidu.com —— 表单编码 POST，Referer 是硬性要求（缺则 illegal-visit）。"""
from __future__ import annotations

import logging

from ..models import RECRUIT_CAMPUS, RECRUIT_INTERN, RECRUIT_SOCIAL, Job
from .base import Spider, register

log = logging.getLogger(__name__)

API = "https://talent.baidu.com/httservice/getPostListNew"

# recruitType 的枚举值从落地页 HTML 里挖出来的，猜不出来：
# 校招是 GRADUATE 不是 CAMPUS，试 CAMPUS/SCHOOL/FRESH 都返回 fail
_RECRUIT_PARAM = {
    RECRUIT_SOCIAL: ("SOCIAL", "social-list"),
    RECRUIT_CAMPUS: ("GRADUATE", "campus-list"),
    RECRUIT_INTERN: ("INTERN", "intern-list"),
}


@register
class BaiduSpider(Spider):
    slug = "baidu"
    company = "百度"
    page_size = 20      # 实测上限：>20 服务端直接返回 status="fail"
    supports = (RECRUIT_SOCIAL, RECRUIT_CAMPUS, RECRUIT_INTERN)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # 一次请求只能取一种类型，所以把"类型×页码"摊平成一条序列：
        # 当前类型翻完了就切下一个，对 crawl() 来说仍是线性翻页。
        self._queue = [t for t in self.recruit_types if t in _RECRUIT_PARAM]
        self._cur = 0
        self._page_in_type = 0

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        if self._cur >= len(self._queue):
            return [], False
        kind = self._queue[self._cur]
        rtype, referer = _RECRUIT_PARAM[kind]
        self._page_in_type += 1

        data = self.http.json(
            "POST",
            API,
            data={                      # 表单编码，不是 JSON
                "recruitType": rtype,
                "pageSize": self.page_size,
                "curPage": self._page_in_type,   # 每种类型各自从 1 开始
                "keyWord": "",
                "projectType": "",
                "postType": "",
                "workPlace": "",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"https://talent.baidu.com/jobs/{referer}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        # 成功时 status 为 "ok"；pageSize 超限或缺 Referer 时为 "fail"/"no-auth"
        status = str((data or {}).get("status", "")).lower()
        if status not in ("ok", "success", "200"):
            log.warning("[%s] %s 第 %s 页被拒，status=%r",
                        self.slug, kind, self._page_in_type, status)
            return self._next_type()

        block = data.get("data") or {}
        rows = block.get("list") or []

        jobs = []
        for p in rows:
            places = p.get("workPlace") or ""
            jobs.append(
                Job(
                    company=self.company,
                    job_id=str(p.get("postId") or p.get("jobId") or ""),
                    title=p.get("name") or "",
                    responsibility=p.get("workContent") or "",
                    requirement=p.get("serviceCondition") or "",
                    cities=[c.strip() for c in places.split(",") if c.strip()],
                    department=p.get("orgName") or p.get("bgShortName") or "",
                    category=p.get("postType") or p.get("projectType") or "",
                    education=p.get("reqEducationName") or p.get("education") or "",
                    work_years=str(p.get("workYears") or ""),
                    publish_date=(p.get("publishDate") or "")[:10],
                    url=f"https://talent.baidu.com/jobs/detail?postId={p.get('postId')}"
                    if p.get("postId") else "",
                    recruit_type=kind,
                )
            )

        # total 是字符串，稳妥转换
        try:
            total = int(block.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        done = (block.get("isLastPage", False)
                or self._page_in_type * self.page_size >= total
                or not rows)
        if done:
            more_jobs, has_more = self._next_type()
            return jobs + more_jobs, has_more
        return jobs, True

    def _next_type(self) -> tuple[list[Job], bool]:
        """当前类型抓完，切到下一个。还有类型就继续翻页。"""
        self._cur += 1
        self._page_in_type = 0
        return [], self._cur < len(self._queue)
