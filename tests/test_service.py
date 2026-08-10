"""service 层测试。全部用假 Spider / 假 LLM，不出网、不烧配额。"""
from __future__ import annotations

import pytest

from jobradar import service
from jobradar.service import (
    AnalyzeStatus, compute_coverage, preview_analyze, run_analyze, run_crawl,
)
from jobradar.spiders.base import Spider
from jobradar.store import Store
from tests.conftest import make_job

NOW = "2026-08-01 00:00:00"


class FakeSpider(Spider):
    slug = "fake"
    company = "假公司"
    page_size = 10
    n_pages = 2
    boom_at: int | None = None       # 第几页抛异常

    def fetch_page(self, page):
        if self.boom_at is not None and page == self.boom_at:
            raise RuntimeError("模拟接口挂了")
        if page > self.n_pages:
            return [], False
        jobs = [make_job(page * 100 + i, company=self.company)
                for i in range(self.page_size)]
        return jobs, page < self.n_pages


@pytest.fixture
def fake_registry(monkeypatch):
    def _install(cls):
        monkeypatch.setattr(service, "get_spiders", lambda slugs=None: [cls])
    return _install


class TestRunCrawl:
    def test_返回结构与落库(self, db_path, fake_registry):
        fake_registry(FakeSpider)
        res = run_crawl(db_path, interval=0.0)
        assert res.total_fetched == 20
        assert res.total_inserted == 20
        assert len(res.companies) == 1
        assert res.companies[0].company == "假公司"
        assert res.companies[0].error is None
        assert res.counts["total"] == 20
        assert res.session_id and len(res.session_id) == 12

    def test_on_event被调用(self, db_path, fake_registry):
        fake_registry(FakeSpider)
        events = []
        run_crawl(db_path, interval=0.0, on_event=events.append)
        kinds = [e.kind for e in events]
        assert "spider_start" in kinds
        assert "spider_done" in kinds

    def test_中途异常已抓部分入库且记录error(self, db_path, fake_registry):
        class Boom(FakeSpider):
            n_pages = 5
            boom_at = 3
        fake_registry(Boom)
        res = run_crawl(db_path, interval=0.0)
        # 第 1、2 页共 20 条已 yield，第 3 页炸
        assert res.total_fetched == 20
        assert res.companies[0].error is not None
        assert "模拟接口挂了" in res.companies[0].error
        assert Store(db_path).counts()["total"] == 20

    def test_页级异常写进runs的note(self, db_path, fake_registry):
        """crawl() 吞掉页级异常，若不透传，中断会被记成成功。"""
        from jobradar.db import connect

        class Boom(FakeSpider):
            n_pages = 5
            boom_at = 2
        fake_registry(Boom)
        run_crawl(db_path, interval=0.0)
        with connect(db_path, readonly=True) as conn:
            note = conn.execute("SELECT note FROM runs").fetchone()["note"]
        assert note.startswith("failed:"), f"中断必须留痕，实际 note={note!r}"

    def test_should_stop生效(self, db_path, fake_registry):
        fake_registry(FakeSpider)
        res = run_crawl(db_path, interval=0.0, should_stop=lambda: True)
        assert res.cancelled is True

    def test_写入runs含session与slug(self, db_path, fake_registry):
        from jobradar.db import connect
        fake_registry(FakeSpider)
        res = run_crawl(db_path, interval=0.0)
        with connect(db_path, readonly=True) as conn:
            row = conn.execute("SELECT * FROM runs").fetchone()
        assert row["session_id"] == res.session_id
        assert row["slug"] == "fake"
        assert row["note"] == ""


class DetailSpider(FakeSpider):
    """模拟 MokaHR 那类平台：列表只有标题，JD 要逐条拉。"""
    slug = "detail"
    company = "详情公司"
    needs_detail = True
    n_pages = 1
    page_size = 5
    boom_on_detail: set[str] | None = None

    def fetch_page(self, page):
        if page > self.n_pages:
            return [], False
        jobs = []
        for i in range(self.page_size):
            j = make_job(i, company=self.company)
            j.responsibility = ""        # 列表里没有 JD
            j.requirement = ""
            jobs.append(j)
        return jobs, False

    def fetch_detail(self, job):
        if self.boom_on_detail and job.title in self.boom_on_detail:
            raise RuntimeError("详情接口挂了")
        job.responsibility = f"补全的职责 {job.title}"
        job.requirement = "补全的要求"
        return job


class TestDetailEnrich:
    def test_列表无JD时逐条补全(self, db_path, fake_registry):
        fake_registry(DetailSpider)
        res = run_crawl(db_path, interval=0.0)
        assert res.total_fetched == 5
        rows = Store(db_path).query()
        assert all(r["responsibility"].startswith("补全的职责") for r in rows)

    def test_已有JD的岗位跳过详情(self, db_path, fake_registry):
        """这是省时间的关键：几千个岗位逐条拉要几十分钟。"""
        fake_registry(DetailSpider)
        run_crawl(db_path, interval=0.0)          # 第一轮全部补全

        spy = {"calls": 0}

        class Counting(DetailSpider):
            def fetch_detail(self, job):
                spy["calls"] += 1
                return super().fetch_detail(job)

        fake_registry(Counting)
        run_crawl(db_path, interval=0.0)          # 第二轮应全部跳过
        assert spy["calls"] == 0, "已有 JD 的岗位不该再拉详情"

    def test_详情失败不丢岗位(self, db_path, fake_registry):
        class Boom(DetailSpider):
            boom_on_detail = {"岗位0", "岗位1"}
        fake_registry(Boom)
        res = run_crawl(db_path, interval=0.0)
        # 5 条全部入库，只是两条缺 JD
        assert res.total_fetched == 5
        rows = {r["title"]: r for r in Store(db_path).query()}
        assert rows["岗位0"]["responsibility"] == ""
        assert rows["岗位2"]["responsibility"].startswith("补全的职责")

    def test_未声明needs_detail则不调详情(self, db_path, fake_registry):
        """普通爬虫的路径不能被这个改动影响。"""
        spy = {"calls": 0}

        class Plain(FakeSpider):
            def fetch_detail(self, job):
                spy["calls"] += 1
                return job

        fake_registry(Plain)
        run_crawl(db_path, interval=0.0)
        assert spy["calls"] == 0

    def test_声明needs_detail但没实现会被容错(self, db_path, fake_registry):
        """基类抛 NotImplementedError，应被当成普通详情失败吞掉。"""
        class Broken(FakeSpider):
            needs_detail = True
            n_pages = 1
        fake_registry(Broken)
        res = run_crawl(db_path, interval=0.0)
        assert res.total_fetched == 10        # 岗位仍然入库
        assert res.companies[0].error is None


class TestRecruitTypeRouting:
    def test_不支持的类型直接跳过(self, db_path, fake_registry):
        """网易没有校招接口，抓校招时不该白跑一趟。"""
        from jobradar.models import RECRUIT_CAMPUS, RECRUIT_SOCIAL

        class SocialOnly(FakeSpider):
            slug, company = "so", "只有社招"
            supports = (RECRUIT_SOCIAL,)

        fake_registry(SocialOnly)
        res = run_crawl(db_path, interval=0.0, recruit_types=[RECRUIT_CAMPUS])
        assert res.companies == [], "不支持的公司应被跳过，不产生结果行"
        assert res.total_fetched == 0

    def test_取交集后只抓支持的类型(self, db_path, fake_registry):
        from jobradar.models import (
            RECRUIT_CAMPUS, RECRUIT_INTERN, RECRUIT_SOCIAL)
        seen = {}

        class PartialSupport(FakeSpider):
            slug, company = "ps", "只支持实习"
            supports = (RECRUIT_SOCIAL, RECRUIT_INTERN)

            def fetch_page(self, page):
                seen["types"] = self.recruit_types
                return super().fetch_page(page)

        fake_registry(PartialSupport)
        run_crawl(db_path, interval=0.0,
                  recruit_types=[RECRUIT_CAMPUS, RECRUIT_INTERN])
        assert seen["types"] == (RECRUIT_INTERN,), "校招不支持，只该传实习"

    def test_默认抓社招(self, db_path, fake_registry):
        from jobradar.models import RECRUIT_SOCIAL
        seen = {}

        class S(FakeSpider):
            def fetch_page(self, page):
                seen["types"] = self.recruit_types
                return super().fetch_page(page)

        fake_registry(S)
        run_crawl(db_path, interval=0.0)
        assert seen["types"] == (RECRUIT_SOCIAL,)


class FakeLLM:
    model = "fake-model"

    def __init__(self, fail_extract=False, fail_report=False, quota=False):
        self.fail_extract = fail_extract
        self.fail_report = fail_report
        self.quota = quota
        self.calls = 0

    def chat_tool(self, system, user, tool, **kw):
        self.calls += 1
        if self.quota:
            raise RuntimeError("HTTP 403: SUBUSER_QUOTA_EXHAUSTED")
        if self.fail_extract:
            raise RuntimeError("boom")
        n = user.count("[")
        return {"jobs": [{"idx": i, "level": "高级", "years": "3年",
                          "hard_skills": ["Go"], "domains": ["后端"],
                          "signals": ["高并发"]} for i in range(n)]}

    def chat(self, system, user, **kw):
        if self.fail_report:
            raise RuntimeError("报告生成炸了")
        return "# 测试报告\n\n正文"

    def close(self):
        pass


@pytest.fixture
def seeded(db_path):
    store = Store(db_path)
    store.upsert([make_job(i, track="backend") for i in range(30)], NOW)
    return db_path


def _patch_llm(monkeypatch, llm):
    monkeypatch.setattr("jobradar.llm.LLM", lambda *a, **k: llm)


class TestRunAnalyze:
    def test_空库返回NO_JOBS(self, db_path):
        res = run_analyze(db_path, tracks=["ai"])
        assert res.status is AnalyzeStatus.NO_JOBS

    def test_成功路径(self, seeded, monkeypatch):
        _patch_llm(monkeypatch, FakeLLM())
        res = run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        assert res.status is AnalyzeStatus.OK
        assert res.markdown.startswith("# 测试报告")
        assert res.jobs_selected == 30
        assert res.extracted_ok == 30
        assert res.report_id is not None
        assert res.coverage.skills_total == 30

    def test_报告失败返回REPORT_FAILED但仍存报告行(self, seeded, monkeypatch):
        _patch_llm(monkeypatch, FakeLLM(fail_report=True))
        res = run_analyze(seeded, tracks=["backend"], limit=30)
        assert res.status is AnalyzeStatus.REPORT_FAILED
        assert res.report_id is not None      # 失败也留痕
        assert res.markdown is None
        assert "报告生成炸了" in res.error

    def test_抽取全失败返回EXTRACT_FAILED(self, seeded, monkeypatch):
        _patch_llm(monkeypatch, FakeLLM(fail_extract=True))
        res = run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        assert res.status is AnalyzeStatus.EXTRACT_FAILED

    def test_配额错误立即中止不空转(self, seeded, monkeypatch):
        llm = FakeLLM(quota=True)
        _patch_llm(monkeypatch, llm)
        run_analyze(seeded, tracks=["backend"], limit=30, batch_size=5)
        # 6 批本该调 6 次，配额错误必须第一批就停
        assert llm.calls == 1

    def test_连续失败3次即中止(self, seeded, monkeypatch):
        llm = FakeLLM(fail_extract=True)
        _patch_llm(monkeypatch, llm)
        run_analyze(seeded, tracks=["backend"], limit=30, batch_size=2)
        # 15 批本该全撞，连续失败保护应在第 3 次停
        assert llm.calls == 3

    def test_缓存复用不重复调用(self, seeded, monkeypatch):
        _patch_llm(monkeypatch, FakeLLM())
        run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        llm2 = FakeLLM()
        _patch_llm(monkeypatch, llm2)
        res = run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        assert llm2.calls == 0, "全部命中缓存时不该有抽取调用"
        assert res.cache_hits == 30

    def test_覆盖率快照用抽取后的计数(self, seeded, monkeypatch):
        """报告反映它写入那一刻的库状态。

        用分析前的计数会把本次自己贡献的抽取结果漏掉，
        前端显著位置展示的覆盖率就会偏低。
        """
        from jobradar.api import queries as q
        _patch_llm(monkeypatch, FakeLLM())
        res = run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        row = q.get_report(seeded, res.report_id)
        # 30 个岗位全部抽取成功，覆盖率应是 30/30 而不是 0/30
        assert row["coverage_at_time"] == 100.0

    def test_报告失败时快照也含已抽取结果(self, seeded, monkeypatch):
        from jobradar.api import queries as q
        _patch_llm(monkeypatch, FakeLLM(fail_report=True))
        res = run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        assert res.status is AnalyzeStatus.REPORT_FAILED
        row = q.get_report(seeded, res.report_id)
        assert row["coverage_at_time"] == 100.0, "抽取已落库，快照不该记成 0"

    def test_写文件是可选的(self, seeded, monkeypatch, tmp_path):
        _patch_llm(monkeypatch, FakeLLM())
        res = run_analyze(seeded, tracks=["backend"], limit=30)
        assert res.out_path is None, "不传 out 就不该写文件"

        out = tmp_path / "r.md"
        res2 = run_analyze(seeded, tracks=["backend"], limit=30, out=out)
        assert out.exists()
        assert out.with_suffix(".stats.json").exists()
        assert res2.out_path == str(out)


class TestPreview:
    def test_预估调用次数(self, seeded):
        p = preview_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        assert p["selected"] == 30
        assert p["already_cached"] == 0
        assert p["to_extract"] == 30
        assert p["estimated_batches"] == 2
        assert p["estimated_llm_calls"] == 3     # 2 抽取 + 1 报告

    def test_全命中缓存时给出警告(self, seeded, monkeypatch):
        _patch_llm(monkeypatch, FakeLLM())
        run_analyze(seeded, tracks=["backend"], limit=30, batch_size=15)
        p = preview_analyze(seeded, tracks=["backend"], limit=30)
        assert p["to_extract"] == 0
        assert p["estimated_batches"] == 0
        assert p["warning"] is not None
        assert "调大 limit" in p["warning"]


class TestCoverage:
    def test_薄样本触发告警(self):
        cov = compute_coverage({"total": 5536, "cached": 15})
        assert cov.pct == 0.27
        assert cov.is_thin is True
        assert "5536" in cov.hint

    def test_充分样本不告警(self):
        cov = compute_coverage({"total": 100, "cached": 50})
        assert cov.pct == 50.0
        assert cov.is_thin is False
        assert cov.hint == ""

    def test_空库不除零(self):
        cov = compute_coverage({"total": 0, "cached": 0})
        assert cov.pct == 0.0
