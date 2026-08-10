"""采集器解析测试。用录制的响应片段，不出网。

锁住的都是实际踩过的坑：飞书的 UA 陷阱、携程的英文城市、
小红书的全角逗号、大疆的 AES 解密、滴滴的详情补全。
"""
from __future__ import annotations

import base64
import json

import pytest

from jobradar.spiders.ctrip import CtripSpider, _city
from jobradar.spiders.dji import DjiSpider
from jobradar.spiders.feishu import MAC_UA, FeishuSpider, ms_to_date
from jobradar.spiders.moka import AES_IV, decrypt as _decrypt


class FakeHttp:
    """记录请求、返回预设响应。"""

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = []

    def json(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.payload


class TestFeishu:
    def _spider(self, payload):
        class S(FeishuSpider):
            slug, company, host = "t", "测试", "t.jobs.feishu.cn"
        return S(FakeHttp(payload), max_pages=1)

    def test_必须用Mac_UA(self):
        """带 Windows/Linux UA 会被返回 405，这是排查最久的一个坑。"""
        sp = self._spider({"code": 0, "data": {"job_post_list": []}})
        sp.fetch_page(1)
        ua = sp.http.calls[0]["headers"]["User-Agent"]
        assert "Macintosh" in ua, "飞书接口拒绝非 Mac UA"
        assert ua == MAC_UA

    def test_解析岗位(self):
        sp = self._spider({"code": 0, "data": {"job_post_list": [{
            "id": 123, "title": "算法工程师",
            "description": "职责内容", "requirement": "要求内容",
            "city_list": [{"name": "上海"}, {"name": "北京"}],
            "job_category": {"name": "研发"},
            "recruit_type": {"name": "全职"},
            "publish_time": 1784871116225,
        }]}})
        jobs, more = sp.fetch_page(1)
        j = jobs[0]
        assert j.title == "算法工程师"
        assert j.cities == ["上海", "北京"]
        assert j.responsibility == "职责内容"
        assert j.publish_date == "2026-07-24"
        assert more is False

    def test_跳过实习岗(self):
        sp = self._spider({"code": 0, "data": {"job_post_list": [
            {"id": 1, "title": "实习生", "recruit_type": {"name": "实习"}},
            {"id": 2, "title": "正式岗", "recruit_type": {"name": "全职"}},
        ]}})
        jobs, _ = sp.fetch_page(1)
        assert [j.title for j in jobs] == ["正式岗"]

    def test_city_list为空时回落city_info(self):
        sp = self._spider({"code": 0, "data": {"job_post_list": [{
            "id": 1, "title": "岗", "city_info": {"name": "深圳"},
            "recruit_type": {"name": "全职"}}]}})
        jobs, _ = sp.fetch_page(1)
        assert jobs[0].cities == ["深圳"]

    def test_offset超上限直接停(self):
        sp = self._spider({"code": 0, "data": {"job_post_list": []}})
        sp.page_size = 100
        jobs, more = sp.fetch_page(101)      # offset=10000
        assert jobs == [] and more is False
        assert not sp.http.calls, "超过 offset 上限不该再发请求"

    def test_错误码返回空(self):
        sp = self._spider({"code": 1, "message": "boom"})
        assert sp.fetch_page(1) == ([], False)


class TestBytedance:
    def test_继承飞书但详情URL不同(self):
        from jobradar.spiders.bytedance import BytedanceSpider
        sp = BytedanceSpider(FakeHttp({"code": 0, "data": {"job_post_list": [{
            "id": 999, "title": "岗", "recruit_type": {"name": "正式"}}]}}))
        jobs, _ = sp.fetch_page(1)
        assert "/experienced/position/999/detail" in jobs[0].url


class TestFeishuCampus:
    def _spider(self, payload, types):
        class S(FeishuSpider):
            slug, company, host = "t", "测试", "t.jobs.feishu.cn"
        return S(FakeHttp(payload), max_pages=1, recruit_types=types)

    def _payload(self):
        # recruit_type.id 是权威判据：101=社招 201=校招 202=实习
        return {"code": 0, "data": {"job_post_list": [
            {"id": 1, "title": "社招岗", "recruit_type": {
                "id": "101", "name": "正式",
                "parent": {"name": "社招"}}},
            {"id": 2, "title": "校招岗", "recruit_type": {
                "id": "201", "name": "正式",
                "parent": {"name": "校招"}}},
            {"id": 3, "title": "实习岗", "recruit_type": {
                "id": "202", "name": "实习",
                "parent": {"name": "校招"}}},
        ]}}

    def test_按id分流招聘类型(self):
        from jobradar.models import (
            RECRUIT_CAMPUS, RECRUIT_INTERN, RECRUIT_SOCIAL)
        sp = self._spider(self._payload(),
                          (RECRUIT_SOCIAL, RECRUIT_CAMPUS, RECRUIT_INTERN))
        jobs, _ = sp.fetch_page(1)
        got = {j.title: j.recruit_type for j in jobs}
        assert got == {"社招岗": RECRUIT_SOCIAL, "校招岗": RECRUIT_CAMPUS,
                       "实习岗": RECRUIT_INTERN}

    def test_只要校招时过滤掉其它(self):
        from jobradar.models import RECRUIT_CAMPUS
        sp = self._spider(self._payload(), (RECRUIT_CAMPUS,))
        jobs, _ = sp.fetch_page(1)
        assert [j.title for j in jobs] == ["校招岗"]

    def test_校招要带website_path请求头(self):
        """这个头是唯一的开关，body 里的 portal_type 试遍都没用。"""
        from jobradar.models import RECRUIT_CAMPUS, RECRUIT_SOCIAL
        sp = self._spider({"code": 0, "data": {"job_post_list": []}},
                          (RECRUIT_CAMPUS,))
        sp.fetch_page(1)
        assert sp.http.calls[0]["headers"].get("website-path") == "campus"

        sp2 = self._spider({"code": 0, "data": {"job_post_list": []}},
                           (RECRUIT_SOCIAL,))
        sp2.fetch_page(1)
        assert "website-path" not in sp2.http.calls[0]["headers"]

    def test_id认不出时按名字兜底(self):
        from jobradar.models import RECRUIT_INTERN
        sp = self._spider({"code": 0, "data": {"job_post_list": [
            {"id": 9, "title": "怪岗", "recruit_type": {
                "id": "999", "name": "实习", "parent": {"name": "校招"}}},
        ]}}, (RECRUIT_INTERN,))
        jobs, _ = sp.fetch_page(1)
        assert jobs[0].recruit_type == RECRUIT_INTERN


class TestMultiTypeQueue:
    """百度/小红书/网易一次请求只能取一种类型，靠队列轮换。"""

    def test_百度校招用GRADUATE(self):
        from jobradar.models import RECRUIT_CAMPUS
        from jobradar.spiders.baidu import BaiduSpider
        sp = BaiduSpider(FakeHttp({"status": "ok", "data": {"list": []}}),
                         max_pages=1, recruit_types=(RECRUIT_CAMPUS,))
        sp.fetch_page(1)
        assert sp.http.calls[0]["data"]["recruitType"] == "GRADUATE"

    def test_小红书类型值必须小写(self):
        from jobradar.models import RECRUIT_INTERN
        from jobradar.spiders.xiaohongshu import XiaohongshuSpider
        sp = XiaohongshuSpider(FakeHttp({"data": {"list": []}}),
                               max_pages=1, recruit_types=(RECRUIT_INTERN,))
        sp.fetch_page(1)
        assert sp.http.calls[0]["json"]["recruitType"] == "intern"

    def test_网易实习是workType_1(self):
        from jobradar.models import RECRUIT_INTERN
        from jobradar.spiders.netease import NeteaseSpider
        sp = NeteaseSpider(FakeHttp({"code": 200, "data": {"list": []}}),
                           max_pages=1, recruit_types=(RECRUIT_INTERN,))
        sp.fetch_page(1)
        assert sp.http.calls[0]["json"]["workType"] == "1"

    def test_每种类型页码各自从1开始(self):
        from jobradar.models import RECRUIT_CAMPUS, RECRUIT_INTERN
        from jobradar.spiders.xiaohongshu import XiaohongshuSpider
        # total=0 让第一种类型一页就抓完，触发切换
        sp = XiaohongshuSpider(FakeHttp({"data": {"total": 0, "list": []}}),
                               max_pages=3,
                               recruit_types=(RECRUIT_CAMPUS, RECRUIT_INTERN))
        sp.fetch_page(1)
        sp.fetch_page(2)
        pages = [c["json"]["pageNum"] for c in sp.http.calls]
        types = [c["json"]["recruitType"] for c in sp.http.calls]
        assert types == ["campus", "intern"]
        assert pages == [1, 1], "切换类型后页码要重置"


class TestMoka:
    def test_大疆三个站点siteId不同(self):
        from jobradar.spiders.dji import (
            DjiCampusSpider, DjiInternSpider, DjiSpider)
        assert DjiSpider.site_id == 170070
        assert DjiCampusSpider.site_id == 143359
        assert DjiInternSpider.site_id == 168240
        # 实习站的路径段是 social 不是 campus，写错会 404
        assert DjiInternSpider.site_path == "social-recruitment"
        assert DjiCampusSpider.site_path == "campus-recruitment"

    def test_滴滴校招是另一套系统(self):
        from jobradar.models import RECRUIT_CAMPUS
        from jobradar.spiders.didi import DidiCampusSpider
        assert DidiCampusSpider.org_id == "didiglobal"   # 不是 didi
        assert DidiCampusSpider.supports == (RECRUIT_CAMPUS,)

    def test_limit不超过服务端上限(self):
        """传 51+ 会返回加密的"参数错误"，不解密看不出来。"""
        from jobradar.spiders.dji import DjiCampusSpider
        sp = DjiCampusSpider(FakeHttp(None), max_pages=1)
        sp.page_size = 200
        sp.fetch_page(1)
        assert sp.http.calls[0]["json"]["limit"] == 50


class TestTencentCampus:
    def test_按projectId分校招实习(self):
        from jobradar.models import RECRUIT_CAMPUS, RECRUIT_INTERN
        from jobradar.spiders.tencent_campus import TencentCampusSpider
        sp = TencentCampusSpider(FakeHttp({"data": {"count": 3, "positionList": [
            {"postId": "1", "positionTitle": "应届岗", "projectId": 1,
             "workCities": "深圳 北京"},
            {"postId": "2", "positionTitle": "实习岗", "projectId": 2,
             "workCities": "上海"},
            {"postId": "3", "positionTitle": "青云应届", "projectId": 14,
             "workCities": "深圳"},
        ]}}), max_pages=1, recruit_types=(RECRUIT_CAMPUS, RECRUIT_INTERN))
        jobs, _ = sp.fetch_page(1)
        got = {j.title: j.recruit_type for j in jobs}
        assert got["应届岗"] == RECRUIT_CAMPUS
        assert got["青云应届"] == RECRUIT_CAMPUS
        assert got["实习岗"] == RECRUIT_INTERN
        assert jobs[0].cities == ["深圳", "北京"]   # 空格分隔

    def test_详情必须用GET(self):
        """POST 会返回 200 但 data 是空对象。"""
        from jobradar.models import Job
        from jobradar.spiders.tencent_campus import TencentCampusSpider
        sp = TencentCampusSpider(FakeHttp({"data": {
            "desc": "职责正文", "request": "任职要求",
            "graduateBonus": "加分项内容"}}), max_pages=1)
        job = Job(company="腾讯", job_id="123", title="岗")
        out = sp.fetch_detail(job)
        assert sp.http.calls[0]["method"] == "GET"
        assert out.responsibility == "职责正文"
        assert "加分项" in out.requirement


class TestCtrip:
    def test_英文城市转中文(self):
        """不转的话同一城市会被算成两个，污染统计。"""
        assert _city("Shanghai") == ["上海"]
        assert _city("Beijing") == ["北京"]
        assert _city("Unknown City") == ["Unknown City"]
        assert _city(None) == []

    def test_只请求一次(self):
        """服务端忽略分页，一次返回全量，翻页纯属浪费。"""
        sp = CtripSpider(FakeHttp({"retCode": "201", "retValue": {
            "recruitJobAdList": [{"id": 1, "jobTitle": "岗位",
                                  "cityName": "Shanghai"}]}}))
        jobs, more = sp.fetch_page(1)
        assert len(jobs) == 1 and more is False
        assert sp.fetch_page(2) == ([], False)
        assert len(sp.http.calls) == 1, "第二页不该发请求"

    def test_retCode201才算成功(self):
        sp = CtripSpider(FakeHttp({"retCode": "500"}))
        assert sp.fetch_page(1) == ([], False)


class TestXiaohongshu:
    def test_全角逗号分隔城市(self):
        from jobradar.spiders.xiaohongshu import XiaohongshuSpider
        sp = XiaohongshuSpider(FakeHttp({"data": {"total": 1, "list": [{
            "positionId": 1, "positionName": "岗",
            "workplace": "北京市，上海市，杭州市"}]}}))
        jobs, _ = sp.fetch_page(1)
        assert jobs[0].cities == ["北京市", "上海市", "杭州市"]

    def test_recruitType必须是小写social(self):
        from jobradar.spiders.xiaohongshu import XiaohongshuSpider
        sp = XiaohongshuSpider(FakeHttp({"data": {"list": []}}))
        sp.fetch_page(1)
        assert sp.http.calls[0]["json"]["recruitType"] == "social"


class TestDji:
    def _encrypt(self, obj, key: bytes) -> str:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        raw = json.dumps(obj, ensure_ascii=False).encode()
        pad = 16 - len(raw) % 16
        raw += bytes([pad]) * pad
        enc = Cipher(algorithms.AES(key), modes.CBC(AES_IV)).encryptor()
        return base64.b64encode(enc.update(raw) + enc.finalize()).decode()

    def test_AES解密(self):
        key = b"007625bb3c1f7de2"
        payload = {"data": self._encrypt({"data": {"jobs": [{"title": "岗"}]}}, key),
                   "necromancer": key.decode()}
        got = _decrypt(payload)
        assert got["data"]["jobs"][0]["title"] == "岗"

    def test_解密失败返回None不抛(self):
        assert _decrypt({"data": "bm90LWVuY3J5cHRlZA==",
                         "necromancer": "0123456789abcdef"}) is None
        assert _decrypt({}) is None

    def test_jobStats总数为0时靠条数判断翻页(self):
        """jobStats.total 恒为 0，不能用来判断有没有下一页。"""
        key = b"0123456789abcdef"
        jobs = [{"id": i, "title": f"岗{i}"} for i in range(50)]
        sp = DjiSpider(FakeHttp({
            "data": self._encrypt(
                {"data": {"jobs": jobs, "jobStats": {"total": 0}}}, key),
            "necromancer": key.decode()}))
        parsed, more = sp.fetch_page(1)
        assert len(parsed) == 50
        assert more is True, "满页就该继续翻，不能信 total=0"

    def test_siteId必须是数字(self):
        sp = DjiSpider(FakeHttp(None))
        sp.fetch_page(1)
        assert sp.http.calls[0]["json"]["siteId"] == 170070
        assert isinstance(sp.http.calls[0]["json"]["siteId"], int)


class TestDidi:
    def test_声明需要详情补全(self):
        from jobradar.spiders.didi import DidiSpider
        assert DidiSpider.needs_detail is True

    def test_详情补全JD但不改标题(self):
        """标题参与 fingerprint，改了会让去重失效、旧记录变孤儿。"""
        from jobradar.models import Job
        from jobradar.spiders.didi import DidiSpider
        sp = DidiSpider(FakeHttp({"data": {
            "jobDesc": "详情职责", "qualification": "详情要求",
            "jobName": "详情里的新标题"}}))
        job = Job(company="滴滴", job_id="1", title="列表里的标题")
        before = job.fingerprint
        out = sp.fetch_detail(job)
        assert out.responsibility == "详情职责"
        assert out.requirement == "详情要求"
        assert out.title == "列表里的标题"
        assert out.fingerprint == before


class TestMsToDate:
    def test_毫秒转日期(self):
        assert ms_to_date(1784871116225) == "2026-07-24"

    def test_非法值归空(self):
        assert ms_to_date(None) == ""
        assert ms_to_date(0) == ""
        assert ms_to_date("abc") == ""
