"""API 层测试。用 TestClient，不出网、不烧配额。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from jobradar.api.app import create_app
from jobradar.store import Store
from tests.conftest import make_job
from tests.test_service import FakeLLM

NOW = "2026-08-01 00:00:00"


@pytest.fixture
def client(db_path):
    # 不启调度线程，避免测试间互相干扰
    app = create_app(db_path, start_scheduler=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_seeded(db_path):
    store = Store(db_path)
    store.upsert([make_job(i, track="backend") for i in range(20)]
                 + [make_job(100 + i, track="ai") for i in range(10)], NOW)
    app = create_app(db_path, start_scheduler=False)
    with TestClient(app) as c:
        yield c


class TestOverview:
    def test_空库也能返回(self, client):
        r = client.get("/api/overview")
        assert r.status_code == 200
        d = r.json()
        assert d["jobs_total"] == 0
        assert d["coverage"]["pct"] == 0.0
        assert d["latest_report"] is None

    def test_有数据时结构完整(self, client_seeded):
        d = client_seeded.get("/api/overview").json()
        assert d["jobs_total"] == 30
        assert {x["name"] for x in d["by_track"]} == {"backend", "ai"}
        assert d["matrix"][0]["total"] == 30
        assert d["scheduler"]["config"]["enabled"] is False

    def test_覆盖率为薄样本时标记(self, client_seeded):
        d = client_seeded.get("/api/coverage").json()
        assert d["jobs_total"] == 30
        assert d["skills_total"] == 0
        assert d["is_thin"] is True
        assert "30" in d["hint"]
        assert len(d["by_track"]) == 2

    def test_meta含爬虫列表(self, client):
        from jobradar.spiders import all_slugs
        d = client.get("/api/meta").json()
        slugs = {s["slug"] for s in d["spiders"]}
        # 不写死集合：接入新公司时这个测试不该失败
        assert slugs == set(all_slugs())
        assert all(s["company"] for s in d["spiders"]), "每家都要有中文名"


class TestTasks:
    def test_空闲时current为null(self, client):
        assert client.get("/api/tasks/current").json() is None

    def test_非法公司返回422(self, client):
        r = client.post("/api/tasks/crawl", json={"companies": ["nope"]})
        assert r.status_code == 422

    def test_参数越界被拒(self, client):
        assert client.post("/api/tasks/crawl",
                           json={"max_pages": 999}).status_code == 422
        assert client.post("/api/tasks/crawl",
                           json={"interval": 0.01}).status_code == 422
        assert client.post("/api/tasks/analyze",
                           json={"limit": 99999}).status_code == 422

    def test_忙时返回409(self, client, monkeypatch):
        import threading
        gate = threading.Event()
        client.app.state.tasks.submit("crawl", lambda rec: gate.wait(timeout=5))
        r = client.post("/api/tasks/crawl", json={})
        assert r.status_code == 409
        gate.set()

    def test_取消不存在的任务409(self, client):
        assert client.post("/api/tasks/nope/cancel").status_code == 409

    def test_查不存在的任务404(self, client):
        assert client.get("/api/tasks/nope").status_code == 404


class TestAnalyzePreview:
    def test_预估调用次数(self, client_seeded):
        r = client_seeded.get("/api/analyze/preview",
                              params={"tracks": ["backend"], "limit": 20,
                                      "batch_size": 10,
                                      "recruit_types": ["social"]})
        d = r.json()
        assert d["selected"] == 20
        assert d["to_extract"] == 20
        assert d["estimated_batches"] == 2
        assert d["estimated_llm_calls"] == 3

    def test_空库预览为零(self, client):
        d = client.get("/api/analyze/preview").json()
        assert d["selected"] == 0
        assert d["estimated_llm_calls"] == 0

    def test_确认数不符返回409(self, client_seeded):
        r = client_seeded.post("/api/tasks/analyze", json={
            "tracks": ["backend"], "limit": 20, "batch_size": 10,
            "recruit_types": ["social"], "confirm_llm_calls": 999})
        assert r.status_code == 409
        assert r.json()["detail"]["actual"] == 3

    def test_确认数相符则受理(self, client_seeded, monkeypatch):
        monkeypatch.setattr("jobradar.llm.LLM", lambda *a, **k: FakeLLM())
        r = client_seeded.post("/api/tasks/analyze", json={
            "tracks": ["backend"], "limit": 20, "batch_size": 10,
            "recruit_types": ["social"], "confirm_llm_calls": 3})
        assert r.status_code == 200
        assert r.json()["kind"] == "analyze"

    def test_没岗位时拒绝提交(self, client):
        r = client.post("/api/tasks/analyze", json={"tracks": ["ai"]})
        assert r.status_code == 422


class TestReports:
    def test_没报告时latest返回404(self, client):
        assert client.get("/api/reports/latest").status_code == 404

    def test_列表为空(self, client):
        assert client.get("/api/reports").json() == []

    def test_分析后可取到报告(self, client_seeded, monkeypatch):
        monkeypatch.setattr("jobradar.llm.LLM", lambda *a, **k: FakeLLM())
        r = client_seeded.post("/api/tasks/analyze",
                               json={"tracks": ["backend"], "limit": 20,
                                     "recruit_types": ["social"]})
        tid = r.json()["id"]
        for _ in range(200):
            st = client_seeded.get(f"/api/tasks/{tid}").json()["state"]
            if st in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        assert st == "succeeded", client_seeded.get(f"/api/tasks/{tid}").json()

        d = client_seeded.get("/api/reports/latest").json()
        assert d["status"] == "ok"
        assert d["markdown"].startswith("# 测试报告")
        assert d["top_skills"][0]["name"] == "Go"
        assert d["jobs_selected"] == 20

        lst = client_seeded.get("/api/reports").json()
        assert len(lst) == 1
        assert lst[0]["id"] == d["id"]

    def test_删除报告(self, client_seeded, monkeypatch):
        monkeypatch.setattr("jobradar.llm.LLM", lambda *a, **k: FakeLLM())
        from jobradar import service
        res = service.run_analyze(client_seeded.app.state.db_path,
                                  tracks=["backend"], limit=20)
        rid = res.report_id
        assert client_seeded.delete(f"/api/reports/{rid}").status_code == 200
        assert client_seeded.get(f"/api/reports/{rid}").status_code == 404

    def test_对比样本差异大时给警告(self, client_seeded, monkeypatch):
        monkeypatch.setattr("jobradar.llm.LLM", lambda *a, **k: FakeLLM())
        from jobradar import service
        db = client_seeded.app.state.db_path
        a = service.run_analyze(db, tracks=["backend"], limit=20).report_id
        b = service.run_analyze(db, tracks=["backend", "ai"], limit=5).report_id
        d = client_seeded.get("/api/reports/compare",
                              params={"a": a, "b": b}).json()
        assert d["warning"] is not None
        assert "不可直接比较" in d["warning"]

    def test_对比不存在的报告404(self, client):
        assert client.get("/api/reports/compare",
                          params={"a": 1, "b": 2}).status_code == 404


class TestSchedule:
    def test_默认未启用(self, client):
        d = client.get("/api/schedule").json()
        assert d["config"]["enabled"] is False
        assert d["next_run_at"] is None

    def test_启用后返回下次运行时间(self, client):
        r = client.put("/api/schedule", json={
            "enabled": True, "hour": 3, "minute": 30,
            "weekdays": [0, 1, 2, 3, 4, 5, 6], "max_pages": 30})
        assert r.status_code == 200
        d = r.json()
        assert d["config"]["enabled"] is True
        assert d["next_run_at"] is not None
        assert d["next_run_at"].endswith("03:30:00")

    def test_配置持久化(self, client):
        client.put("/api/schedule", json={"enabled": True, "hour": 7,
                                          "minute": 5, "weekdays": [1]})
        assert client.get("/api/schedule").json()["config"]["hour"] == 7

    def test_非法时间被拒(self, client):
        assert client.put("/api/schedule",
                          json={"hour": 99}).status_code == 422

    def test_非法公司被拒(self, client):
        assert client.put("/api/schedule",
                          json={"companies": ["nope"]}).status_code == 422


class TestCrawlHistory:
    def test_空库返回空(self, client):
        assert client.get("/api/crawl-history").json() == []

    def test_部分成功被标记(self, client, db_path):
        store = Store(db_path)
        store.log_run("2026-08-01 00:00:00", "网易", 340, 10,
                      "failed: 接口挂了", session_id="s1", slug="netease")
        d = client.get("/api/crawl-history").json()
        assert d[0]["partial"] is True, "抓到一半才失败要能表达出来"
        assert d[0]["failed"] == ["网易"]
