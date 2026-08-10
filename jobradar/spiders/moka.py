"""MokaHR 加密 ATS 通用适配器。

大疆和滴滴校招用的是同一家供应商，接口路径、加密方案、字段结构完全一致，
只有 host / orgId / siteId 不同。

响应不是明文 JSON，而是 {"data": "<base64密文>", "necromancer": "<16位hex>"}：
AES-128-CBC，**key 就是 necromancer 字段本身**（16 个 ASCII 字符直接当密钥字节），
IV 是页面里的 aesIv 常量。

三个坑：
- `limit` 硬上限 50，传 51+ 返回**加密的**错误 {"code":102,"msg":"参数错误"}，
  不解密看不出来，很容易当成"抓完了"。
- `jobStats.total` 恒为 0，不能用来判断翻页，只能翻到返回不足一页为止。
- `showIsCampus` 在所有站点恒为 False，**不能用它区分校招**。
  区分靠 siteId 本身——每种招聘类型一个独立站点。
"""
from __future__ import annotations

import base64
import json
import logging

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..models import Job
from .base import Spider

log = logging.getLogger(__name__)

API_PATH = "/api/outer/ats-apply/website/jobs/v2"

# 页面里的 window.TurboApply.data.aesIv。两家站点用的是同一个值。
AES_IV = b"de7c21ed8d6f50fe"

# 服务端硬上限，超了返回加密的"参数错误"
MAX_LIMIT = 50


def decrypt(payload: dict) -> dict | None:
    blob, key = payload.get("data"), payload.get("necromancer")
    if not blob or not key:
        return None
    try:
        ct = base64.b64decode(blob)
        cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(AES_IV))
        dec = cipher.decryptor()
        raw = dec.update(ct) + dec.finalize()
        raw = raw[:-raw[-1]]                      # 去 PKCS7 填充
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:                      # noqa: BLE001
        log.warning("解密失败（IV 可能变了）: %s", exc)
        return None


class MokaSpider(Spider):
    """子类给出 host / org_id / site_id / site_path / kind。"""

    host: str = ""
    org_id: str = ""
    site_id: int = 0
    site_path: str = "social-recruitment"   # URL 里的路径段，要和 siteId 配对
    kind: str = ""                          # 这个站点对应的招聘类型
    page_size = MAX_LIMIT

    @property
    def site_url(self) -> str:
        return f"https://{self.host}/{self.site_path}/{self.org_id}/{self.site_id}"

    def fetch_page(self, page: int) -> tuple[list[Job], bool]:
        payload = self.http.json(
            "POST", f"https://{self.host}{API_PATH}",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "Referer": self.site_url},
            json={"orgId": self.org_id, "siteId": self.site_id,
                  "limit": min(self.page_size, MAX_LIMIT),
                  "offset": (page - 1) * min(self.page_size, MAX_LIMIT)},
        )
        if not payload:
            return [], False
        data = decrypt(payload)
        if not data:
            return [], False
        if data.get("code") not in (None, 0, 200):
            log.warning("[%s] 服务端拒绝: %s", self.slug, data.get("msg"))
            return [], False

        rows = (data.get("data") or {}).get("jobs") or []
        jobs = [
            Job(
                company=self.company,
                job_id=str(p.get("id") or ""),
                title=p.get("title") or "",
                # 职责和要求混在一个 HTML 字段里，没有单独的 requirement
                responsibility=p.get("jobDescription") or "",
                cities=[c.get("cityName") for c in (p.get("locations") or [])
                        if c.get("cityName")],
                department=(p.get("department") or {}).get("name") or "",
                category=(p.get("zhineng") or {}).get("name") or "",
                publish_date=(p.get("publishedAt") or "")[:10],
                url=f"{self.site_url}/job/{p.get('id')}" if p.get("id") else "",
                recruit_type=self.kind,
            )
            for p in rows
        ]
        # jobStats.total 恒为 0，只能靠返回条数判断
        return jobs, len(rows) >= min(self.page_size, MAX_LIMIT)
