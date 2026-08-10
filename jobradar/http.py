"""带限速和重试的 HTTP 客户端。公开接口也要克制请求频率。"""
from __future__ import annotations

import fcntl
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class RateLimiter:
    """进程内最小请求间隔，线程安全。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


# 进程级共享限速器。两个并发抓取任务各建一个 Http 会让限速翻倍，
# 对目标站是双倍压力。shared=True 时共用这一个。
_SHARED_LOCK = threading.Lock()
_shared_limiter: RateLimiter | None = None


def get_shared_limiter(min_interval: float) -> RateLimiter:
    global _shared_limiter
    with _SHARED_LOCK:
        if _shared_limiter is None:
            _shared_limiter = RateLimiter(min_interval)
        else:
            # 取更保守的那个间隔，不让后来者放宽限制
            _shared_limiter.min_interval = max(
                _shared_limiter.min_interval, min_interval)
        return _shared_limiter


class CrawlBusy(RuntimeError):
    """另一个进程正在抓取。"""


@contextmanager
def crawl_lock(path):
    """跨进程互斥。

    进程内的 Lock 拦不住「服务在抓取时用户又开终端敲 ./jr 抓」——
    那是两个进程，共享不了限速器，对目标站就是双倍请求。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CrawlBusy("另一个 jobradar 进程正在抓取，稍后再试") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


class Http:
    def __init__(self, min_interval: float = 1.5, timeout: float = 25.0,
                 shared: bool = False) -> None:
        self.limiter = (get_shared_limiter(min_interval) if shared
                        else RateLimiter(min_interval))
        self.client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=False,   # 重定向往往意味着要登录，不盲目跟随
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        reraise=True,
    )
    def request(self, method: str, url: str, **kw) -> httpx.Response:
        self.limiter.wait()
        resp = self.client.request(method, url, **kw)
        # 429/5xx 值得重试；4xx 其余多为反爬或需鉴权，重试无意义
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    def json(self, method: str, url: str, **kw) -> dict | None:
        resp = self.request(method, url, **kw)
        if resp.status_code != 200:
            log.warning("%s %s -> HTTP %s", method, url, resp.status_code)
            return None
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            log.warning("%s 返回非 JSON (%s)，大概是反爬或需登录", url, ctype)
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("%s 响应无法解析为 JSON", url)
            return None

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Http":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
