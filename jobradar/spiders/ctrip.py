"""携程 job.ctrip.com。

两个与众不同的地方：
- **分页参数被完全忽略**：pageIndex/pageSize 传什么都返回全部 536 条，
  所以只请求一次，第二页直接返回空。
- JD 是 HTML 字符串（<p>、<ul><li>），不是纯文本。models.clean() 会剥标签。
- 成功码是 retCode "201" 而不是 200；cityName 是英文（Shanghai）。
"""
from __future__ import annotations

from ..models import Job
from .base import Spider, register

API = "https://job.ctrip.com/api/hrrecruit/getJobAd"

# 英文城市名转中文，跟其它公司的数据对齐，否则同一城市会被算成两个
_CITY_CN = {
    "shanghai": "上海", "beijing": "北京", "shenzhen": "深圳",
    "guangzhou": "广州", "hangzhou": "杭州", "chengdu": "成都",
    "nantong": "南通", "wuhan": "武汉", "xian": "西安", "nanjing": "南京",
    "suzhou": "苏州", "tianjin": "天津", "chongqing": "重庆",
    "hong kong": "香港", "singapore": "新加坡", "seoul": "首尔",
    "tokyo": "东京", "edinburgh": "爱丁堡", "london": "伦敦",
}


def _city(name: str | None) -> list[str]:
    if not name:
        return []
    key = name.strip().lower()
    return [_CITY_CN.get(key, name.strip())]


@register
class CtripSpider(Spider):
    slug = "ctrip"
    company = "携程"
    page_size = 1000

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        # 服务端忽略分页，一次就是全量，多请求纯属浪费
        if page > 1:
            return [], False

        data = self.http.json(
            "POST", API,
            headers={"Content-Type": "application/json",
                     "Referer": "https://job.ctrip.com/"},
            json={"condition": {"source": "ctrip"},
                  "pageIndex": 1, "pageSize": self.page_size},
        )
        if not data or str(data.get("retCode")) not in ("201", "200"):
            return [], False

        rows = (data.get("retValue") or {}).get("recruitJobAdList") or []
        jobs = [
            Job(
                company=self.company,
                job_id=str(p.get("id") or p.get("fromId") or ""),
                title=p.get("jobTitle") or "",
                responsibility=p.get("duty") or "",
                requirement=p.get("requirements") or "",
                cities=_city(p.get("cityName")),
                department=p.get("buName") or "",
                category=p.get("jobFamilyGroupName") or p.get("kindName") or "",
                publish_date=(p.get("publishDate") or "")[:10],
                url=f"https://job.ctrip.com/detail/{p.get('jobId')}"
                    if p.get("jobId") else "",
            )
            for p in rows
        ]
        return jobs, False
