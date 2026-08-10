"""两阶段分析：分批抽取结构化技能项 -> 统计 -> 汇总成学习方向报告。

为什么分两阶段：几千条 JD 塞不进一次上下文，且直接问"缺什么人才"
模型只会给套话。先让它把每条 JD 拆成可统计的技能标签，用代码算频次，
再把频次事实喂回去做解读——结论就有据可依。
"""
from __future__ import annotations

import json
import logging
from collections import Counter

from .llm import LLM
from .models import TRACK_AI, TRACK_BACKEND

log = logging.getLogger(__name__)

# 连续失败到这个数就中止。不依赖错误文案，防止 _is_quota_error 漏判时
# 把剩余批次全撞一遍（每批还带 4 次重试和指数退避）。
MAX_CONSECUTIVE_FAILURES = 3

EXTRACT_SYSTEM = """你是技术招聘数据分析师。我会给你若干条中国互联网公司的岗位 JD，
每条以 [序号] 开头。请为每条 JD 抽取结构化标签，并通过 emit_skills 工具一次性提交全部结果。

规则：
- idx 必须与输入的 [序号] 严格对应，不要遗漏、不要编造序号。
- hard_skills 用业界通用写法，同义归一：k8s->Kubernetes，golang->Golang，
  pytorch->PyTorch，大模型->LLM，大语言模型->LLM。每条最多 12 个。
- 只抽 JD 里真实写了的技能，不要根据岗位名推测补充。
- level 必须从给定枚举里选，不要用英文。
- domains 最多 4 个，signals 最多 3 个，没有就给空数组。

重要：不要在回复里写分析过程或结果文本。你唯一的输出方式是调用
emit_skills 工具。把结果写成普通文字等于没有提交，这一批会被整个丢弃。"""

EXTRACT_TOOL = {
    "name": "emit_skills",
    "description": "提交每条 JD 的结构化抽取结果",
    "input_schema": {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "idx": {"type": "integer", "description": "输入的 [序号]"},
                        # 锁枚举：不锁的话模型会回 senior/mid 这类英文，统计就散了
                        "level": {
                            "type": "string",
                            "enum": ["初级", "中级", "高级", "专家", "不明"],
                        },
                        "years": {"type": "string", "description": "年限要求原文，无则空串"},
                        "hard_skills": {"type": "array", "items": {"type": "string"}},
                        "domains": {"type": "array", "items": {"type": "string"}},
                        "signals": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["idx", "level", "hard_skills", "domains", "signals"],
                },
            }
        },
        "required": ["jobs"],
    },
}

REPORT_SYSTEM = """你是资深技术招聘与人才发展顾问。我会给你一份基于真实抓取的 JD
统计出的频次数据。请据此写一份中文分析报告，给一个想明确学习方向的开发者看。

这是一份交付文档，不是对话。硬性要求：
1. 开头第一段必须先说样本局限：分析了多少个岗位、来自哪几家公司、覆盖了哪些方向，
   以及这个样本量能支撑什么结论、不能支撑什么。不要假装样本充分。
2. 每个判断都必须引用数据里的具体数字或标签，例如「LLM 出现在 15/30 个岗位」。
   数据里没有的，不要写。
3. 必须包含「学习路径」一节，且严格分成这三档，每档都要写：
   - 立刻补（1 个月内）
   - 3-6 个月
   - 长期
   每档说明：学什么、对应数据里的哪个信号、达到什么程度算够用。
4. 必须有一节讲「哪些技能频次高但供给通常不足」，这是最有价值的部分。
5. 禁止在结尾反问读者、禁止提供后续服务选项、禁止写「你想看什么我可以继续」。
   报告要能独立看完就够用。
6. 不确定的地方直接写「数据不足，无法判断」。

用 Markdown，不要用 emoji，控制在 1500 字以内。"""


def _batch(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _extract_rows(data: dict) -> list:
    """从工具返回里取岗位数组。

    schema 定义的是 "jobs"，但模型偶尔会用 "results"/"items" 之类的同义 key
    （opus 上实测出现过一次）。不兜底的话整批 15 个岗位会被静默丢弃，
    日志只报"无法对应输入序号"，很难查。
    """
    if not isinstance(data, dict):
        return []
    for key in ("jobs", "results", "items", "data", "positions"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    # 只有一个值且是列表，那就是它
    vals = [v for v in data.values() if isinstance(v, list)]
    if len(vals) == 1:
        log.warning("工具返回用了预期外的 key: %s", list(data))
        return vals[0]
    return []


def _is_quota_error(exc: Exception) -> bool:
    """配额/额度类错误：再试也是白试，应当立刻停，别把剩余批次全撞一遍。"""
    s = str(exc).upper()
    return any(k in s for k in
               ("QUOTA", "INSUFFICIENT", "BALANCE", "EXHAUSTED", "BILLING"))


def extract_skills(llm: LLM, jobs: list[dict], batch_size: int = 15,
                   store=None, on_event=None, should_stop=None) -> list[dict]:
    """抽取技能标签。

    带缓存：已抽过的岗位直接复用，不重复消耗配额。
    每批抽完立刻落盘，中途失败已抽的部分不丢。
    """
    from datetime import datetime

    results: list[dict] = []

    def emit(kind, **kw):
        if on_event is not None:
            from .service import Event
            on_event(Event(kind, **kw))

    # 1) 先吃缓存
    todo = jobs
    if store is not None:
        cache = store.cached_skills([j["fingerprint"] for j in jobs])
        if cache:
            for j in jobs:
                hit = cache.get(j["fingerprint"])
                if hit:
                    results.append({**hit, "company": j["company"],
                                    "track": j.get("track") or "",
                                    "title": j["title"]})
            todo = [j for j in jobs if j["fingerprint"] not in cache]
            log.info("缓存命中 %s 个岗位，还需抽取 %s 个", len(cache), len(todo))
            emit("cache", phase="extract", current=len(cache),
                 message=f"缓存命中 {len(cache)} 个，还需抽取 {len(todo)} 个")
    if not todo:
        return results

    # 2) 剩下的分批抽
    batches = list(_batch(todo, batch_size))
    aborted = False
    consecutive_failures = 0
    for n, batch in enumerate(batches, 1):
        if should_stop is not None and should_stop():
            log.warning("收到取消信号，停止抽取。已完成 %s 条已存库。", len(results))
            break
        lines = []
        for i, job in enumerate(batch):
            jd = f"{job['title']}\n{job.get('responsibility', '')}\n{job.get('requirement', '')}"
            lines.append(f"[{i}] 公司:{job['company']}\n{jd[:1500]}")
        user = "\n\n---\n\n".join(lines)
        try:
            data = llm.chat_tool(EXTRACT_SYSTEM, user, EXTRACT_TOOL)
        except Exception as exc:
            if _is_quota_error(exc):
                log.error("第 %s/%s 批：额度已用尽，停止抽取。"
                          "已抽好的结果都已存库，补足额度后重跑会接着抽。",
                          n, len(batches))
                aborted = True
                break
            consecutive_failures += 1
            log.warning("第 %s/%s 批抽取失败，跳过: %s", n, len(batches), exc)
            # 不依赖错误文案的兜底：中转站若用中文报配额不足，
            # _is_quota_error 会漏判，连撞几十批既慢又烧钱
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("连续 %s 批失败，停止抽取以免空转。最后一个错误: %s",
                          consecutive_failures, exc)
                aborted = True
                break
            continue
        consecutive_failures = 0
        rows = _extract_rows(data)
        batch_rows, dropped = [], 0
        for row in rows or []:
            if not isinstance(row, dict):
                dropped += 1
                continue
            idx = row.get("idx")
            if not (isinstance(idx, int) and 0 <= idx < len(batch)):
                # 对不上源岗位就没法归因公司/方向，留着只会污染分方向统计
                dropped += 1
                continue
            src = batch[idx]
            row["fingerprint"] = src["fingerprint"]
            row["company"] = src["company"]
            row["track"] = src.get("track") or ""
            row["title"] = src["title"]
            batch_rows.append(row)
        if dropped:
            log.warning("第 %s/%s 批有 %s 条无法对应输入序号，已丢弃",
                        n, len(batches), dropped)
        # 3) 立刻落盘
        if store is not None and batch_rows:
            store.save_skills(batch_rows, llm.model,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        results.extend(batch_rows)
        log.info("抽取进度 %s/%s 批，累计 %s 条", n, len(batches), len(results))
        emit("batch", phase="extract", current=n, total=len(batches),
             message=f"抽取进度 {n}/{len(batches)} 批，累计 {len(results)} 条")

    if aborted:
        log.warning("本轮因额度中断，已完成 %s 条。重跑 `jr 报告` 会复用这些结果。",
                    len(results))
    return results


def aggregate(extracted: list[dict], top_n: int = 30) -> dict:
    skills, domains, signals, levels = Counter(), Counter(), Counter(), Counter()
    by_track_skill: dict[str, Counter] = {}
    for row in extracted:
        track = row.get("track") or "other"
        bucket = by_track_skill.setdefault(track, Counter())
        for s in row.get("hard_skills") or []:
            key = str(s).strip()
            if key:
                skills[key] += 1
                bucket[key] += 1
        for d in row.get("domains") or []:
            if str(d).strip():
                domains[str(d).strip()] += 1
        for g in row.get("signals") or []:
            if str(g).strip():
                signals[str(g).strip()] += 1
        levels[row.get("level") or "不明"] += 1
    return {
        "sample_size": len(extracted),
        "top_skills": skills.most_common(top_n),
        "top_domains": domains.most_common(20),
        "top_signals": signals.most_common(20),
        "levels": levels.most_common(),
        "skills_by_track": {
            t: c.most_common(15) for t, c in by_track_skill.items()
        },
    }


def build_report(llm: LLM, stats: dict, meta: dict) -> str:
    payload = {
        "样本说明": meta,
        "岗位总数": stats["sample_size"],
        "职级分布": stats["levels"],
        "高频硬技能": stats["top_skills"],
        "高频领域": stats["top_domains"],
        "值得注意的信号": stats["top_signals"],
        "分方向技能": {
            {"backend": "后端/服务端", "ai": "算法/AI/大模型"}.get(k, k): v
            for k, v in stats["skills_by_track"].items()
            if k in (TRACK_BACKEND, TRACK_AI)
        },
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    # 不传 temperature：opus 系列已废弃这个参数，传了会 400
    return llm.chat(REPORT_SYSTEM, user)
