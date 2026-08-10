"""upsert 分批提交 + RETURNING + 流式语义测试。"""
from __future__ import annotations

import pytest

from jobradar import store as store_mod
from jobradar.db import connect
from jobradar.store import Store
from tests.conftest import make_job

NOW1 = "2026-08-01 00:00:00"
NOW2 = "2026-08-02 00:00:00"


class TestReturning:
    def test_首次插入计为新增(self, store):
        fetched, inserted = store.upsert([make_job(i) for i in range(10)], NOW1)
        assert (fetched, inserted) == (10, 10)

    def test_重复写入不计新增(self, store):
        jobs = [make_job(i) for i in range(10)]
        store.upsert(jobs, NOW1)
        fetched, inserted = store.upsert([make_job(i) for i in range(10)], NOW2)
        assert fetched == 10
        assert inserted == 0, "同一批岗位第二次写入不该算新增"

    def test_混合新旧只计新的(self, store):
        store.upsert([make_job(i) for i in range(5)], NOW1)
        fetched, inserted = store.upsert(
            [make_job(i) for i in range(8)], NOW2)     # 5 旧 + 3 新
        assert (fetched, inserted) == (8, 3)

    def test_first_seen不被覆盖(self, store):
        store.upsert([make_job(1)], NOW1)
        store.upsert([make_job(1)], NOW2)
        with connect(store.path, readonly=True) as conn:
            row = conn.execute(
                "SELECT first_seen, last_seen FROM jobs").fetchone()
        assert row["first_seen"] == NOW1, "first_seen 必须保留首次时间"
        assert row["last_seen"] == NOW2, "last_seen 应更新"


class TestBatching:
    def test_跨批边界不丢行(self, store, monkeypatch):
        # 调小批阈值，强制多次 flush
        monkeypatch.setattr(store_mod, "BATCH_ROWS", 7)
        fetched, inserted = store.upsert(
            [make_job(i) for i in range(50)], NOW1)
        assert (fetched, inserted) == (50, 50)
        assert store.counts()["total"] == 50

    def test_接受生成器不物化(self, store):
        """upsert 必须能吃生成器——流式写入是抓取进度上报的前提。"""
        def gen():
            for i in range(20):
                yield make_job(i)
        fetched, inserted = store.upsert(gen(), NOW1)
        assert (fetched, inserted) == (20, 20)

    def test_生成器中途抛异常已抓部分保留(self, store, monkeypatch):
        """这是流式改造带来的语义变化：不再是全有或全无。"""
        monkeypatch.setattr(store_mod, "BATCH_ROWS", 5)

        def bad_gen():
            for i in range(12):
                yield make_job(i)
            raise RuntimeError("模拟 spider 中途挂掉")

        with pytest.raises(RuntimeError):
            store.upsert(bad_gen(), NOW1)

        assert store.counts()["total"] == 12, "抛异常前已 yield 的必须落库"


class TestRecruitType:
    def test_招聘类型被写入(self, store):
        """INSERT 漏写这一列的话，校招数据会静默落成 social。"""
        from jobradar.models import RECRUIT_CAMPUS, RECRUIT_INTERN
        jobs = [make_job(1), make_job(2), make_job(3)]
        jobs[0].recruit_type = RECRUIT_CAMPUS
        jobs[1].recruit_type = RECRUIT_INTERN
        store.upsert(jobs, NOW1)
        got = {r["title"]: r["recruit_type"] for r in store.query()}
        assert got["岗位1"] == RECRUIT_CAMPUS
        assert got["岗位2"] == RECRUIT_INTERN
        assert got["岗位3"] == "social"      # 默认值

    def test_同岗位不同招聘类型不互相覆盖(self, store):
        """同一家的"算法工程师·北京"校招岗和社招岗是两个岗位。"""
        from jobradar.models import RECRUIT_CAMPUS
        a, b = make_job(1), make_job(1)
        b.recruit_type = RECRUIT_CAMPUS
        assert a.fingerprint != b.fingerprint, "招聘类型必须参与指纹"
        store.upsert([a, b], NOW1)
        assert store.counts()["total"] == 2

    def test_按招聘类型过滤(self, store):
        from jobradar.models import RECRUIT_CAMPUS
        a, b = make_job(1), make_job(2)
        b.recruit_type = RECRUIT_CAMPUS
        store.upsert([a, b], NOW1)
        assert len(store.query(recruit_types=["campus"])) == 1
        assert len(store.query(recruit_types=["social"])) == 1
        assert len(store.query(recruit_types=["campus", "social"])) == 2

    def test_counts含招聘类型维度(self, store):
        from jobradar.models import RECRUIT_CAMPUS
        b = make_job(2)
        b.recruit_type = RECRUIT_CAMPUS
        store.upsert([make_job(1), b], NOW1)
        assert store.counts()["by_recruit"] == {"social": 1, "campus": 1}


class TestBalancedSampling:
    """岗位数在各公司间极不均衡，直接 LIMIT 会让样本被大厂主导。"""

    def _seed(self, store):
        jobs = []
        for i in range(100):
            jobs.append(make_job(i, company="大厂"))
        for i in range(5):
            jobs.append(make_job(1000 + i, company="小厂"))
        store.upsert(jobs, NOW1)

    def test_默认取样会被大厂主导(self, store):
        self._seed(store)
        rows = store.query(limit=20)
        assert {r["company"] for r in rows} == {"大厂"}

    def test_均衡取样让每家都有代表(self, store):
        self._seed(store)
        rows = store.query(limit=20, balanced=True)
        got = {r["company"] for r in rows}
        assert got == {"大厂", "小厂"}, "小厂必须能进样本"

    def test_均衡取样不重复不丢失(self, store):
        self._seed(store)
        rows = store.query(balanced=True)
        fps = [r["fingerprint"] for r in rows]
        assert len(fps) == len(set(fps)) == 105

    def test_均衡取样不泄漏rn列(self, store):
        """窗口函数的 ROW_NUMBER 是实现细节，不该出现在返回结果里。"""
        self._seed(store)
        assert "rn" not in store.query(limit=3, balanced=True)[0]

    def test_预览与实际取样一致(self, store):
        """两处各写一遍的话，预览说的调用次数会和实际对不上。"""
        self._seed(store)
        fps = store.iter_fingerprints(limit=20, balanced=True)
        rows = store.query(limit=20, balanced=True)
        assert fps == [r["fingerprint"] for r in rows]


class TestSkillsCache:
    def test_保存与读回一致(self, store):
        rows = [{"fingerprint": "fp1", "level": "高级", "years": "3年",
                 "hard_skills": ["Go", "K8s"], "domains": ["后端"],
                 "signals": ["高并发"]}]
        assert store.save_skills(rows, "m1", NOW1) == 1
        got = store.cached_skills(["fp1"])
        assert got["fp1"]["hard_skills"] == ["Go", "K8s"]
        assert got["fp1"]["level"] == "高级"
        assert got["fp1"]["fingerprint"] == "fp1", "缓存项必须带 fingerprint"

    def test_无fingerprint的行被丢弃(self, store):
        assert store.save_skills([{"level": "高级"}], "m1", NOW1) == 0

    def test_count_cached计数正确(self, store):
        store.save_skills(
            [{"fingerprint": f"fp{i}", "hard_skills": []} for i in range(3)],
            "m1", NOW1)
        assert store.count_cached(["fp0", "fp1", "fp9"]) == 2

    def test_空输入不炸(self, store):
        assert store.cached_skills([]) == {}
        assert store.count_cached([]) == 0
        assert store.save_skills([], "m1", NOW1) == 0


class TestQuery:
    def test_按方向过滤(self, store):
        store.upsert([make_job(1, track="backend"),
                      make_job(2, track="ai")], NOW1)
        assert len(store.query(tracks=["ai"])) == 1

    def test_limit与offset(self, store):
        store.upsert([make_job(i) for i in range(10)], NOW1)
        assert len(store.query(limit=3)) == 3
        page1 = store.query(limit=3)
        page2 = store.query(limit=3, offset=3)
        assert {r["fingerprint"] for r in page1} & {
            r["fingerprint"] for r in page2} == set()

    def test_cities反序列化(self, store):
        store.upsert([make_job(1)], NOW1)
        assert store.query()[0]["cities"] == ["北京"]

    def test_iter_fingerprints与query同序(self, store):
        store.upsert([make_job(i) for i in range(6)], NOW1)
        assert store.iter_fingerprints(limit=4) == [
            r["fingerprint"] for r in store.query(limit=4)]
