"""大疆 careers.dji.com —— MokaHR 加密 ATS，见 moka.py。

三个站点各自独立，靠 siteId 区分招聘类型：
  170070  social-recruitment  社招
  143359  campus-recruitment  校招
  168240  social-recruitment  实习（注意路径段是 social 不是 campus）

`showIsCampus` 恒为 False，即使在校招站点下也是，别拿它做判断。
"""
from __future__ import annotations

from ..models import RECRUIT_CAMPUS, RECRUIT_INTERN, RECRUIT_SOCIAL
from .base import register
from .moka import MokaSpider


class _DjiBase(MokaSpider):
    company = "大疆"
    host = "apply.careers.dji.com"
    org_id = "dji"


@register
class DjiSpider(_DjiBase):
    slug = "dji"
    site_id = 170070
    site_path = "social-recruitment"
    kind = RECRUIT_SOCIAL
    supports = (RECRUIT_SOCIAL,)


@register
class DjiCampusSpider(_DjiBase):
    slug = "dji-campus"
    site_id = 143359
    site_path = "campus-recruitment"
    kind = RECRUIT_CAMPUS
    supports = (RECRUIT_CAMPUS,)


@register
class DjiInternSpider(_DjiBase):
    slug = "dji-intern"
    site_id = 168240
    # 实习站的路径段是 social-recruitment，写成 campus 会 404
    site_path = "social-recruitment"
    kind = RECRUIT_INTERN
    supports = (RECRUIT_INTERN,)
