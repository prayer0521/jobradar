"""飞书招聘（Feishu Hire）通用适配器。

很多公司把招聘站托管在 `{租户}.jobs.feishu.cn`，接口契约完全一致，
只有子域不同。字节自有门户 jobs.bytedance.com 用的也是同一套后端。

**最关键的一个坑**：这套接口按 User-Agent 指纹拦截。
带 Windows 或 Linux 的 UA 一律返回 405（空 body），换 Mac UA 就正常 200，
与 Chrome 版本无关（120/124/126/131 都试过）。
项目默认 UA 是 Windows，所以这里按请求覆盖，不影响其它采集器。

排查这个坑花了不少时间：405 + 空 body 很像"方法不允许"或 CSRF 门禁，
实际是 UA 黑名单。如果哪天又冒出 405，先换 UA 再怀疑其它。
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..models import RECRUIT_CAMPUS, RECRUIT_INTERN, RECRUIT_SOCIAL, Job
from .base import Spider, register

# 站点只认 Mac UA，见模块 docstring
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0.0.0 Safari/537.36")

# offset 超过这个值服务端返回空数组（字节自有门户实测边界）
OFFSET_CAP = 10000

# 切换社招/校招的开关是 **请求头 website-path**，不是 body 里的字段。
# body 里的 portal_type 试遍 1-6 都没用，headers 才是关键。
# 带 website-path: campus 时返回校招 + 实习，两者靠 recruit_type.id 区分。
_CAMPUS_HEADER = "campus"

# recruit_type.id 是权威判据，比按标题猜关键词可靠：
#   101 = 社招正式、201 = 校招正式、202 = 实习
# 父节点 parent.name 是"社招"或"校招"。
_RECRUIT_BY_ID = {
    "101": RECRUIT_SOCIAL,
    "201": RECRUIT_CAMPUS,
    "202": RECRUIT_INTERN,
}


def ms_to_date(value) -> str:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class FeishuSpider(Spider):
    """子类只需给出 slug / company / host。

    recruit_types 决定抓什么：默认社招。要校招和实习就传
    (RECRUIT_CAMPUS, RECRUIT_INTERN) —— 它们来自同一个请求，
    服务端不分开返回，只能拿回来按 recruit_type.id 筛。
    """

    host: str = ""
    page_size = 100          # 实测 200 也收，100 更稳且日志好读
    referer: str = ""
    supports = (RECRUIT_SOCIAL, RECRUIT_CAMPUS, RECRUIT_INTERN)
    recruit_types: tuple[str, ...] = (RECRUIT_SOCIAL,)

    @property
    def api(self) -> str:
        return f"https://{self.host}/api/v1/search/job/posts"

    @property
    def _wants_campus(self) -> bool:
        return bool({RECRUIT_CAMPUS, RECRUIT_INTERN} & set(self.recruit_types))

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        offset = (page - 1) * self.page_size
        if offset >= OFFSET_CAP:
            return [], False

        headers = {
            "Content-Type": "application/json",
            "User-Agent": MAC_UA,           # 不能用项目默认的 Windows UA
            "portal-channel": "career",
            "portal-platform": "website",
            "Referer": self.referer or f"https://{self.host}/index",
        }
        if self._wants_campus:
            headers["website-path"] = _CAMPUS_HEADER

        data = self.http.json(
            "POST", self.api,
            headers=headers,
            json={
                "keyword": "",
                "limit": self.page_size,
                "offset": offset,
                "portal_type": 6 if self._wants_campus else 2,
                "portal_entrance": 1,
            },
        )
        if not data or data.get("code") != 0:
            return [], False

        block = data.get("data") or {}
        posts = block.get("job_post_list") or []
        jobs = [j for j in (self._parse(p) for p in posts) if j is not None]

        # 用返回条数判断翻页，不能用 count：
        # 筛掉不要的类型后 jobs 可能比 posts 少很多，但下一页仍有数据
        has_more = (len(posts) >= self.page_size
                    and offset + self.page_size < OFFSET_CAP)
        return jobs, has_more

    def _parse(self, p: dict) -> Job | None:
        rt = p.get("recruit_type") or {}
        kind = _RECRUIT_BY_ID.get(str(rt.get("id") or ""))
        if kind is None:
            # id 认不出来时退回按名字判断，别静默丢数据
            name = rt.get("name") or ""
            parent = (rt.get("parent") or {}).get("name") or ""
            if "实习" in name:
                kind = RECRUIT_INTERN
            elif "校招" in parent or "校园" in parent:
                kind = RECRUIT_CAMPUS
            else:
                kind = RECRUIT_SOCIAL
        if kind not in self.recruit_types:
            return None

        # 飞书租户只给 city_list，字节自有门户两个都有
        cities = [c.get("name") for c in (p.get("city_list") or [])
                  if c.get("name")]
        if not cities:
            one = (p.get("city_info") or {}).get("name")
            cities = [one] if one else []

        pid = p.get("id")
        return Job(
            company=self.company,
            job_id=str(pid or p.get("code") or ""),
            title=p.get("title") or "",
            responsibility=p.get("description") or "",
            requirement=p.get("requirement") or "",
            cities=cities,
            department=(p.get("department") or {}).get("name") or "",
            category=(p.get("job_category") or {}).get("name") or "",
            publish_date=ms_to_date(p.get("publish_time")),
            url=self.detail_url(pid),
            recruit_type=kind,
        )

    def detail_url(self, pid) -> str:
        return f"https://{self.host}/position/{pid}/detail" if pid else ""


@register
class NioSpider(FeishuSpider):
    slug = "nio"
    company = "蔚来"
    host = "nio.jobs.feishu.cn"


@register
class XpengSpider(FeishuSpider):
    slug = "xpeng"
    company = "小鹏汽车"
    host = "xiaopeng.jobs.feishu.cn"


@register
class DewuSpider(FeishuSpider):
    slug = "dewu"
    company = "得物"
    host = "poizon.jobs.feishu.cn"


@register
class SensetimeSpider(FeishuSpider):
    slug = "sensetime"
    company = "商汤科技"
    host = "sensetime.jobs.feishu.cn"
