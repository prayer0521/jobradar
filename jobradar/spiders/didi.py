"""滴滴 talent.didiglobal.com —— 自建 Spring 后端，GET only（POST 返回 405）。

两个限制：
- **每页固定 16 条，不可控**。pageSize/size/limit/offset/pageNum 全试过，
  服务端一律返回 16 条。1042 个岗位要翻 66 页。
- **列表不含 JD 正文**（jobDuty/jobQualification 恒为 null），必须逐条拉详情。
  所以声明 needs_detail，只对库里没有 JD 的新岗位拉——否则每轮几十分钟。
"""
from __future__ import annotations

from ..models import RECRUIT_CAMPUS, RECRUIT_SOCIAL, Job
from .base import Spider, register
from .moka import MokaSpider

BASE = "https://talent.didiglobal.com/recruit-portal-service/api/job/front"


@register
class DidiSpider(Spider):
    slug = "didi"
    company = "滴滴"
    page_size = 16           # 服务端固定值，传什么都是 16
    needs_detail = True      # 列表没有 JD 正文

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        data = self.http.json(
            "GET", f"{BASE}/list",
            params={"page": page},
            headers={"Referer": "https://talent.didiglobal.com/"},
        )
        if not data:
            return [], False

        block = data.get("data") or {}
        rows = block.get("items") or []

        jobs = [
            Job(
                company=self.company,
                job_id=str(p.get("jdId") or ""),
                title=p.get("jobName") or "",
                cities=[c for c in [p.get("workArea")] if c],
                department=p.get("deptName") or "",
                category=str(p.get("jobTypeName") or ""),
                publish_date=(p.get("refreshTime") or "")[:10],
                url=f"https://talent.didiglobal.com/social/job/{p.get('jdId')}"
                    if p.get("jdId") else "",
            )
            for p in rows
        ]

        try:
            total = int(block.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        has_more = bool(rows) and page * self.page_size < total
        return jobs, has_more

    def fetch_detail(self, job: Job) -> Job:
        data = self.http.json(
            "GET", f"{BASE}/view/{job.job_id}",
            headers={"Referer": "https://talent.didiglobal.com/"},
        )
        if not data:
            return job
        d = data.get("data") or {}
        # 详情页的标题往往比列表新，但不覆盖 title：
        # fingerprint 由 title 算出，改了会让去重失效、旧记录变孤儿
        job.responsibility = d.get("jobDesc") or ""
        job.requirement = d.get("qualification") or ""
        if d.get("publishTime"):
            job.publish_date = str(d["publishTime"])[:10]
        return job


@register
class DidiCampusSpider(MokaSpider):
    """滴滴校招走的是另一套系统：MokaHR 加密 ATS，和大疆同一家供应商。

    跟社招接口完全无关 —— 社招那个 jobType 参数是岗位职类不是招聘类型。
    orgId 是 didiglobal 不是 didi。
    """

    slug = "didi-campus"
    company = "滴滴"
    host = "campus.didiglobal.com"
    org_id = "didiglobal"
    site_id = 96064
    site_path = "campus_apply"
    kind = RECRUIT_CAMPUS
    supports = (RECRUIT_CAMPUS,)
