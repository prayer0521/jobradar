"""网易 hr.163.com —— JSON POST，无需鉴权，字段最干净。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..models import RECRUIT_INTERN, RECRUIT_SOCIAL, Job
from .base import Spider, register

API = "https://hr.163.com/api/hr163/position/queryPage"

# workType 实测：0=社招(1980)、1=实习(405)、2=派遣(53)、3=空。
# 没有校招值 —— 网易校招在 campus.163.com，纯前端渲染，接口未定位到。
_WORK_TYPE = {
    RECRUIT_SOCIAL: "0",
    RECRUIT_INTERN: "1",
}


def _ms_to_date(value) -> str:
    """updateTime 是 epoch 毫秒。"""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@register
class NeteaseSpider(Spider):
    slug = "netease"
    company = "网易"
    page_size = 50
    supports = (RECRUIT_SOCIAL, RECRUIT_INTERN)   # 没有校招接口

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # 一次请求只能取一种 workType，把"类型×页码"摊平成一条序列
        self._queue = [t for t in self.recruit_types if t in _WORK_TYPE]
        self._cur = 0
        self._page_in_type = 0

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        if self._cur >= len(self._queue):
            return [], False
        kind = self._queue[self._cur]
        self._page_in_type += 1

        data = self.http.json(
            "POST",
            API,
            json={
                "currentPage": self._page_in_type,   # 每种类型各自从 1 开始
                "pageSize": self.page_size,
                "workType": _WORK_TYPE[kind],
                "firstLevelDeptNo": "",
                "postTypeNo": "",
            },
            headers={
                "Content-Type": "application/json",
                "Referer": "https://hr.163.com/job-list.html",
            },
        )
        if not data or data.get("code") != 200:
            return self._next_type()

        block = data.get("data") or {}
        rows = block.get("list") or []

        jobs = [
            Job(
                company=self.company,
                job_id=str(p.get("id") or ""),
                title=p.get("name") or "",
                responsibility=p.get("description") or "",
                requirement=p.get("requirement") or "",
                cities=list(p.get("workPlaceNameList") or []),
                department=p.get("firstDepName") or p.get("productName") or "",
                category=p.get("firstPostTypeName") or "",
                education=p.get("reqEducationName") or "",
                work_years=p.get("reqWorkYearsName") or "",
                publish_date=_ms_to_date(p.get("updateTime")),
                url=f"https://hr.163.com/position-detail/{p.get('id')}"
                if p.get("id") else "",
                recruit_type=kind,
            )
            for p in rows
        ]
        if block.get("lastPage", False) or not rows:
            more_jobs, has_more = self._next_type()
            return jobs + more_jobs, has_more
        return jobs, True

    def _next_type(self) -> tuple[list[Job], bool]:
        """当前类型抓完，切到下一个。"""
        self._cur += 1
        self._page_in_type = 0
        return [], self._cur < len(self._queue)
