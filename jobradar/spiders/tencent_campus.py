"""腾讯校招 join.qq.com。

和社招（careers.tencent.com）是两套完全不同的接口和字段，所以单独一个类。

两个坑：
- 列表接口是 POST（GET 返回 405），详情接口反过来是 **GET**（POST 返回空 data）。
- `projectId` 等筛选参数**传了会被服务端忽略**，返回恒为全量。
  只能拉全量再本地按 projectId 分校招/实习。
"""
from __future__ import annotations

from ..models import RECRUIT_CAMPUS, RECRUIT_INTERN, Job
from .base import Spider, register

LIST_API = "https://join.qq.com/api/v1/position/searchPosition"
DETAIL_API = "https://join.qq.com/api/v1/jobDetails/getJobDetailsByPostId"
REFERER = "https://join.qq.com/post.html"

# projectId 的含义来自 /api/v1/position/getAllProject 返回的枚举字典。
# 1=应届生 2=实习生 12=项目实习生 14=青云计划应届生 20=青云计划实习生
# 16=技术研发提前批
_CAMPUS_PROJECTS = {1, 14, 16}
_INTERN_PROJECTS = {2, 12, 20}


@register
class TencentCampusSpider(Spider):
    slug = "tencent-campus"
    company = "腾讯"
    page_size = 200
    supports = (RECRUIT_CAMPUS, RECRUIT_INTERN)
    needs_detail = True          # 列表只有标题，JD 要逐条拉

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        data = self.http.json(
            "POST", LIST_API,
            headers={"Content-Type": "application/json", "Referer": REFERER},
            json={"pageIndex": page, "pageSize": self.page_size},
        )
        if not data:
            return [], False

        block = data.get("data") or {}
        rows = block.get("positionList") or []

        jobs = []
        for p in rows:
            pid = p.get("projectId")
            if pid in _CAMPUS_PROJECTS:
                kind = RECRUIT_CAMPUS
            elif pid in _INTERN_PROJECTS:
                kind = RECRUIT_INTERN
            else:
                continue
            if kind not in self.recruit_types:
                continue
            # workCities 是空格分隔的一串："深圳总部 北京 上海 "
            cities = [c for c in (p.get("workCities") or "").split() if c]
            post_id = p.get("postId")
            jobs.append(Job(
                company=self.company,
                job_id=str(post_id or p.get("id") or ""),
                title=p.get("positionTitle") or "",
                cities=cities,
                department=(p.get("bgs") or "").split()[0]
                           if p.get("bgs") else "",
                category=p.get("projectName") or p.get("recruitLabelName") or "",
                url=f"https://join.qq.com/post_detail.html?pid={post_id}"
                    if post_id else "",
                recruit_type=kind,
            ))

        try:
            total = int(block.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        has_more = bool(rows) and page * self.page_size < total
        return jobs, has_more

    def fetch_detail(self, job: Job) -> Job:
        # 详情必须 GET：POST 会返回 200 但 data 是空对象
        data = self.http.json(
            "GET", DETAIL_API,
            params={"postId": job.job_id},
            headers={"Referer": REFERER},
        )
        if not data:
            return job
        d = data.get("data") or {}
        job.responsibility = d.get("desc") or ""
        # 任职要求 + 加分项：加分项往往写着最值得学的东西
        parts = [d.get("request") or ""]
        bonus = (d.get("internBonus") if job.recruit_type == RECRUIT_INTERN
                 else d.get("graduateBonus")) or ""
        if bonus:
            parts.append(f"【加分项】\n{bonus}")
        job.requirement = "\n\n".join(p for p in parts if p)
        if d.get("workCityList"):
            job.cities = list(d["workCityList"])
        return job
