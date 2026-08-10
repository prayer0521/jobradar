"""腾讯 careers.tencent.com —— 公开 GET 接口，实测可用。"""
from __future__ import annotations

import re
import time

from ..models import Job
from .base import Spider, register

API = "https://careers.tencent.com/tencentcareer/api/post/Query"

_CN_DATE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")


def _to_iso(value: str | None) -> str:
    """腾讯返回中文日期 '2026年08月07日'，统一成 ISO。

    不转的话字符串排序会出错：'年'(U+5E74) > '-'(U+002D)，
    腾讯的行会整体排在其他公司之前，跟真实日期无关。
    """
    if not value:
        return ""
    m = _CN_DATE.search(value)
    if not m:
        return value[:10]
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


@register
class TencentSpider(Spider):
    slug = "tencent"
    company = "腾讯"
    page_size = 100

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        data = self.http.json(
            "GET",
            API,
            params={
                "timestamp": int(time.time() * 1000),
                "pageIndex": page,
                "pageSize": self.page_size,
                "language": "zh-cn",
                "area": "cn",
            },
            headers={"Referer": "https://careers.tencent.com/search.html"},
        )
        if not data or data.get("Code") != 200:
            return [], False

        block = data.get("Data") or {}
        posts = block.get("Posts") or []
        total = block.get("Count") or 0

        jobs = [
            Job(
                company=self.company,
                job_id=str(p.get("PostId") or p.get("RecruitPostId") or ""),
                title=p.get("RecruitPostName") or "",
                # 腾讯把职责和要求都放在 Responsibility 里，Requirement 常为空
                responsibility=p.get("Responsibility") or "",
                requirement=p.get("Requirement") or "",
                cities=[c for c in [p.get("LocationName")] if c],
                department=p.get("BGName") or p.get("ProductName") or "",
                category=p.get("CategoryName") or "",
                publish_date=_to_iso(p.get("LastUpdateTime")),
                url=p.get("PostURL") or "",
            )
            for p in posts
        ]
        has_more = page * self.page_size < total
        return jobs, has_more
