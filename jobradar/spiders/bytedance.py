"""字节跳动 jobs.bytedance.com。

用的就是飞书招聘那套 portal API（jobs.bytedance.com 和
bytedance.jobs.feishu.cn 返回的岗位 id 完全一致），所以直接复用
FeishuSpider，只覆盖 host 和详情页 URL 格式。

两个与飞书租户不同的地方：
- `data.count` 恒为 10000，是搜索层封顶值不是真实总数，不能用来算总页数
  （飞书租户的 count 是真实的）。翻页靠返回条数判断。
- 详情页路径是 /experienced/position/{id}/detail，不是租户的 /position/{id}/detail
"""
from __future__ import annotations

from .base import register
from .feishu import FeishuSpider


@register
class BytedanceSpider(FeishuSpider):
    slug = "bytedance"
    company = "字节跳动"
    host = "jobs.bytedance.com"
    referer = "https://jobs.bytedance.com/experienced/position"

    def detail_url(self, pid) -> str:
        return (f"https://{self.host}/experienced/position/{pid}/detail"
                if pid else "")
