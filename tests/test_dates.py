"""日期归一化测试。

腾讯返回中文日期，其他公司返回 ISO。混在一起时字符串排序会出错：
'年'(U+5E74) > '-'(U+002D)，导致腾讯的行整体排在最前，limit 取样
会变成「只有腾讯」。这个 bug 实际发生过，测试锁死。
"""
from jobradar.spiders.tencent import _to_iso
from jobradar.spiders.netease import _ms_to_date


class Test腾讯中文日期:
    def test_标准中文日期(self):
        assert _to_iso("2026年08月07日") == "2026-08-07"

    def test_单位数月日补零(self):
        assert _to_iso("2026年8月7日") == "2026-08-07"

    def test_空值(self):
        assert _to_iso(None) == ""
        assert _to_iso("") == ""

    def test_已是ISO则原样保留(self):
        assert _to_iso("2026-08-07") == "2026-08-07"


class Test排序一致性:
    def test_归一后可与ISO正确比较(self):
        # 修复前 '2025年03月04' > '2026-08-06'，属于错误结果
        tencent_old = _to_iso("2025年03月04日")
        netease = "2026-08-06"
        assert tencent_old < netease

    def test_同日不同来源相等(self):
        assert _to_iso("2026年08月06日") == "2026-08-06"


class Test网易毫秒时间戳:
    def test_epoch毫秒转日期(self):
        assert _ms_to_date(1786013083000) == "2026-08-06"

    def test_非法值归空(self):
        assert _ms_to_date(None) == ""
        assert _ms_to_date("") == ""
        assert _ms_to_date(0) == ""
