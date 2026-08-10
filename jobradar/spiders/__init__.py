"""采集器注册入口。

已验证可用（公开接口，无需鉴权）：

  社招   tencent / baidu / netease / xiaohongshu / didi / ctrip
        bytedance / nio / xpeng / dewu / sensetime / dji
  校招   tencent-campus / baidu / xiaohongshu / bytedance / nio / xpeng
        dji-campus / didi-campus
  实习   tencent-campus / baidu / netease / xiaohongshu / bytedance
        nio / xpeng / dji-intern

平台分三类：
  飞书招聘  bytedance / nio / xpeng / dewu / sensetime —— 共用 FeishuSpider
  MokaHR   dji* / didi-campus —— 共用 MokaSpider（响应 AES 加密）
  自建站    其余各写各的

未接入及原因：
  alibaba    baxia 反爬 + 子公司站网关 403（XSRF 需绑登录会话）
  meituan    需登录 cookie，401；校招站共用同一鉴权
  jd         社招强制 SSO + 滑块（校招站 campus.jd.com 可用，未接）
  pinduoduo  WAF 在首页就拦，拿不到 HTML 或 JS bundle
  kuaishou   接口路径已知，必填参数未知
  bilibili   接口和头已知，CSRF token 需绑浏览器会话
  xiaomi     社招应用不发 bundle，未定位到接口
  iflytek    北森平台，服务端渲染
  huawei     portal 接口只有 8 个岗位，非完整目录
  lixiang    未找到招聘门户
  ctrip 校招 前端只换 pathname，接口无校招数据（bundle 里没有对应 endpoint）
  netease 校招 campus.163.com 纯前端渲染，未定位到接口
"""
from .base import Spider, all_slugs, get_spiders, register  # noqa: F401
from . import (  # noqa: F401  触发注册
    baidu, bytedance, ctrip, didi, dji, feishu, netease, tencent,
    tencent_campus, xiaohongshu,
)

__all__ = ["Spider", "all_slugs", "get_spiders", "register"]
