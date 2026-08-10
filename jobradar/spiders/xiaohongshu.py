"""小红书 job.xiaohongshu.com —— 自建 ATS，无鉴权。

`recruitType` 必须是小写字符串 "social"，传 "SOCIAL" 或整数会返回
"招聘类型参数异常"。
"""
from __future__ import annotations

from ..models import RECRUIT_CAMPUS, RECRUIT_INTERN, RECRUIT_SOCIAL, Job
from .base import Spider, register

API = "https://job.xiaohongshu.com/websiterecruit/position/pageQueryPosition"

# recruitType 必须是小写：传 "SOCIAL" 或整数会返回"招聘类型参数异常"
_RECRUIT_PARAM = {
    RECRUIT_SOCIAL: "social",
    RECRUIT_CAMPUS: "campus",
    RECRUIT_INTERN: "intern",
}


@register
class XiaohongshuSpider(Spider):
    slug = "xiaohongshu"
    company = "小红书"
    page_size = 100          # 服务端上限，101 起报"单次分页查询最大限制100条"
    supports = (RECRUIT_SOCIAL, RECRUIT_CAMPUS, RECRUIT_INTERN)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # 一次请求只能取一种类型，把"类型×页码"摊平成一条序列
        self._queue = [t for t in self.recruit_types if t in _RECRUIT_PARAM]
        self._cur = 0
        self._page_in_type = 0

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        if self._cur >= len(self._queue):
            return [], False
        kind = self._queue[self._cur]
        self._page_in_type += 1

        data = self.http.json(
            "POST", API,
            headers={"Content-Type": "application/json",
                     "Referer": "https://job.xiaohongshu.com/"},
            json={"pageNum": self._page_in_type, "pageSize": self.page_size,
                  "recruitType": _RECRUIT_PARAM[kind]},
        )
        if not data:
            return self._next_type()

        block = data.get("data") or {}
        rows = block.get("list") or []

        jobs = [
            Job(
                company=self.company,
                job_id=str(p.get("positionId") or ""),
                title=p.get("positionName") or "",
                responsibility=p.get("duty") or "",
                requirement=p.get("qualification") or "",
                # workplace 是"北京市，上海市"这种全角逗号分隔
                cities=[c.strip() for c in
                        (p.get("workplace") or "").replace("，", ",").split(",")
                        if c.strip()],
                department=p.get("jobProjectName") or "",
                category=p.get("jobType") or "",
                publish_date=(p.get("publishTime") or "")[:10],
                url=f"https://job.xiaohongshu.com/detail/{p.get('positionId')}"
                    if p.get("positionId") else "",
                recruit_type=kind,
            )
            for p in rows
        ]

        try:
            total = int(block.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if not rows or self._page_in_type * self.page_size >= total:
            more_jobs, has_more = self._next_type()
            return jobs + more_jobs, has_more
        return jobs, True

    def _next_type(self) -> tuple[list[Job], bool]:
        self._cur += 1
        self._page_in_type = 0
        return [], self._cur < len(self._queue)
