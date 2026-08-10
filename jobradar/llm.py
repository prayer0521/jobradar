"""Anthropic Messages API 客户端，适配 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 中转站。

实测要点（针对 console.tars-ai.com）：
- 必须用 `Authorization: Bearer`。换成 `x-api-key` 会被路由到别的后端模型
  （实测返回 gemini-3.1-flash-lite），拿不到 Claude。
- 不支持 assistant 预填（prefill），会报 400 upstream_error。
  所以结构化输出走 tool_use 强制调用，而不是预填 "{"。
- /v1/models 可查可用模型，该站当前只开放 claude-haiku-4-5。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
API_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


class _Retryable(LLMError):
    """限流或服务端错误，值得重试。"""


def load_env(path: str | Path = ".env") -> None:
    """极简 .env 加载，避免为一个文件引入额外依赖。已存在的环境变量优先。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


class LLM:
    def __init__(self, model: str | None = None, base_url: str | None = None,
                 token: str | None = None, timeout: float = 300.0) -> None:
        load_env()
        self.base_url = (base_url or os.getenv("ANTHROPIC_BASE_URL") or "").rstrip("/")
        self.token = token or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("JOBRADAR_MODEL") or DEFAULT_MODEL
        if not self.base_url:
            raise LLMError("缺少 ANTHROPIC_BASE_URL，请检查 .env")
        if not self.token:
            raise LLMError("缺少 ANTHROPIC_AUTH_TOKEN，请检查 .env")
        self.client = httpx.Client(timeout=timeout)

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",   # 不要改成 x-api-key
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[str]:
        try:
            r = self.client.get(f"{self.base_url}/v1/models", headers=self._headers)
            if r.status_code != 200:
                return []
            return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            return []

    @retry(stop=stop_after_attempt(4),
           wait=wait_exponential(multiplier=2, min=3, max=40),
           retry=retry_if_exception_type((_Retryable, httpx.TransportError)),
           reraise=True)
    def _post(self, payload: dict) -> dict:
        r = self.client.post(f"{self.base_url}/v1/messages",
                             headers=self._headers, json=payload)
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def chat(self, system: str, user: str, max_tokens: int = 4096,
             temperature: float | None = None) -> str:
        """返回纯文本。用于生成报告。"""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # opus 系列已废弃 temperature，传了直接 400。默认不传。
        if temperature is not None:
            payload["temperature"] = temperature
        data = self._post(payload)
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]      # 跳过可能出现的 thinking 块
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            raise LLMError(f"响应无文本内容: {str(data)[:300]}")
        return text

    def chat_tool(self, system: str, user: str, tool: dict,
                  max_tokens: int = 8192,
                  temperature: float | None = None) -> dict:
        """强制模型调用指定工具，直接拿到符合 schema 的 dict。

        比让模型自由输出 JSON 再解析可靠得多：不会有围栏、前言、截断的花括号。
        """
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
        }
        if temperature is not None:
            payload["temperature"] = temperature

        # 偶尔模型会把推理写成 <thinking> 文本块然后收尾，不调工具
        # （opus 上实测过，而且答案其实已经想好了，只是没走工具通道）。
        # 原样重发多半还是同样结果，所以第二次追加一条硬性指令。
        last = None
        for attempt in range(2):
            if attempt:
                # 注意：这个中转站不支持 assistant 预填（会 400），
                # 所以只能追加 user 消息来加压
                payload = {**payload, "messages": [
                    *payload["messages"],
                    {"role": "user",
                     "content": "请直接调用 emit_skills 工具提交结果，"
                                "不要输出任何普通文本或分析过程。"},
                ]}
            data = self._post(payload)
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    return block.get("input") or {}
            last = data
            log.warning("模型未调用工具（第 %s 次），stop_reason=%s",
                        attempt + 1, data.get("stop_reason"))
        stop = (last or {}).get("stop_reason")
        raise LLMError(f"模型两次都未调用工具 (stop_reason={stop}): "
                       f"{str(last)[:300]}")

    def close(self) -> None:
        self.client.close()
