"""共享 fixture。全部显式声明，不用 autouse，避免波及现有 18 个测试。"""
from __future__ import annotations

import pytest

from jobradar.db import migrate
from jobradar.models import Job
from jobradar.store import Store


@pytest.fixture
def db_path(tmp_path):
    """临时库。绝不碰真实的 data/jobs.db。"""
    p = tmp_path / "test.db"
    migrate(p)
    return p


@pytest.fixture
def store(db_path):
    return Store(db_path)


def make_job(n: int, company: str = "测试公司", track: str = "backend") -> Job:
    return Job(
        company=company,
        job_id=str(n),
        title=f"岗位{n}",
        responsibility=f"职责{n}",
        requirement=f"要求{n}",
        cities=["北京"],
        track=track,
        publish_date="2026-08-01",
    )


@pytest.fixture
def fake_jobs():
    return make_job
