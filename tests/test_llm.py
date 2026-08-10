"""LLM 客户端测试。不出网，用假 transport。

锁住的都是实际踩过的坑：opus 的 temperature 被废弃、
模型把结果写成文本不调工具、工具返回用了预期外的 key。
"""
from __future__ import annotations

import json

import httpx
import pytest

from jobradar.llm import LLM, LLMError


def _llm(handler, **kw):
    """用假 transport 造一个 LLM，不出网。"""
    llm = LLM(base_url="https://fake.test", token="t", **kw)
    llm.client = httpx.Client(transport=httpx.MockTransport(handler))
    return llm


def _reply(blocks, stop="end_turn"):
    return httpx.Response(200, json={
        "content": blocks, "model": "m", "stop_reason": stop,
        "usage": {"input_tokens": 1, "output_tokens": 1}})


TOOL = {"name": "emit", "description": "d",
        "input_schema": {"type": "object",
                         "properties": {"jobs": {"type": "array"}}}}


class TestTemperature:
    def test_默认不传temperature(self):
        """opus 系列已废弃这个参数，传了直接 400。"""
        seen = {}

        def handler(req):
            seen["body"] = json.loads(req.content)
            return _reply([{"type": "text", "text": "hi"}])

        _llm(handler).chat("s", "u")
        assert "temperature" not in seen["body"]

    def test_显式传了才带上(self):
        seen = {}

        def handler(req):
            seen["body"] = json.loads(req.content)
            return _reply([{"type": "text", "text": "hi"}])

        _llm(handler).chat("s", "u", temperature=0.5)
        assert seen["body"]["temperature"] == 0.5


class TestThinkingBlocks:
    def test_跳过thinking只取text(self):
        """opus 返回带 thinking 块，混进正文会污染报告。"""
        def handler(req):
            return _reply([
                {"type": "thinking", "thinking": "内部推理不该出现在报告里"},
                {"type": "text", "text": "正文"},
            ])

        assert _llm(handler).chat("s", "u") == "正文"

    def test_只有thinking没有text时报错(self):
        def handler(req):
            return _reply([{"type": "thinking", "thinking": "只想不说"}])

        with pytest.raises(LLMError, match="无文本内容"):
            _llm(handler).chat("s", "u")


class TestToolCall:
    def test_正常取tool_use(self):
        def handler(req):
            return _reply([{"type": "tool_use", "name": "emit",
                            "input": {"jobs": [{"idx": 0}]}}], stop="tool_use")

        got = _llm(handler).chat_tool("s", "u", TOOL)
        assert got == {"jobs": [{"idx": 0}]}

    def test_thinking在前也能取到(self):
        def handler(req):
            return _reply([
                {"type": "thinking", "thinking": "让我想想"},
                {"type": "tool_use", "name": "emit", "input": {"jobs": []}},
            ], stop="tool_use")

        assert _llm(handler).chat_tool("s", "u", TOOL) == {"jobs": []}

    def test_第一次没调工具则重试并加压(self):
        """opus 会把答案写成 <thinking> 文本然后收尾，白烧一次配额。"""
        calls = []

        def handler(req):
            body = json.loads(req.content)
            calls.append(body["messages"])
            if len(calls) == 1:
                return _reply([{"type": "text",
                                "text": "<thinking>算好了但没调工具</thinking>"}])
            return _reply([{"type": "tool_use", "name": "emit",
                            "input": {"jobs": [{"idx": 0}]}}], stop="tool_use")

        got = _llm(handler).chat_tool("s", "u", TOOL)
        assert got == {"jobs": [{"idx": 0}]}
        assert len(calls) == 2
        # 重试必须加压，原样重发多半还是同样结果
        assert len(calls[1]) > len(calls[0])
        assert "emit" in calls[1][-1]["content"]

    def test_重试只追加user消息(self):
        """这个中转站不支持 assistant 预填，加了会 400。"""
        calls = []

        def handler(req):
            calls.append(json.loads(req.content)["messages"])
            return _reply([{"type": "text", "text": "还是不调"}])

        with pytest.raises(LLMError):
            _llm(handler).chat_tool("s", "u", TOOL)
        assert all(m["role"] == "user" for m in calls[1])

    def test_两次都失败才抛错(self):
        def handler(req):
            return _reply([{"type": "text", "text": "就是不调"}])

        with pytest.raises(LLMError, match="两次都未调用工具"):
            _llm(handler).chat_tool("s", "u", TOOL)


class TestRetryable:
    def test_429会重试(self):
        n = {"c": 0}

        def handler(req):
            n["c"] += 1
            if n["c"] < 2:
                return httpx.Response(429, text="rate limited")
            return _reply([{"type": "text", "text": "ok"}])

        llm = _llm(handler)
        llm._post.retry.wait = lambda *a, **k: 0    # 别真的等
        assert llm.chat("s", "u") == "ok"

    def test_400直接抛不重试(self):
        n = {"c": 0}

        def handler(req):
            n["c"] += 1
            return httpx.Response(400, text="bad request")

        with pytest.raises(LLMError, match="HTTP 400"):
            _llm(handler).chat("s", "u")
        assert n["c"] == 1


class TestExtractRows:
    def test_容忍预期外的顶层key(self):
        """模型偶尔用 results 而不是 schema 里的 jobs，
        不兜底的话整批被静默丢弃。"""
        from jobradar.analyze import _extract_rows
        assert _extract_rows({"jobs": [1, 2]}) == [1, 2]
        assert _extract_rows({"results": [3]}) == [3]
        assert _extract_rows({"items": [4]}) == [4]

    def test_单个列表值也认(self):
        from jobradar.analyze import _extract_rows
        assert _extract_rows({"随便什么名字": [5]}) == [5]

    def test_认不出来返回空不抛(self):
        from jobradar.analyze import _extract_rows
        assert _extract_rows({"a": 1, "b": 2}) == []
        assert _extract_rows(None) == []
