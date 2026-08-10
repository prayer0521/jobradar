"""统一岗位数据结构。各公司接口字段名不同，全部归一化到 Job。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict, field

# 抓取方向：与用户关注的两个方向对应
TRACK_BACKEND = "backend"
TRACK_AI = "ai"
TRACK_OTHER = "other"

# 招聘类型。校招和实习是两回事：校招是应届生正式岗，实习是在校生短期岗。
RECRUIT_SOCIAL = "social"      # 社招
RECRUIT_CAMPUS = "campus"      # 校招（应届生）
RECRUIT_INTERN = "intern"      # 实习
RECRUIT_TYPES = (RECRUIT_SOCIAL, RECRUIT_CAMPUS, RECRUIT_INTERN)
RECRUIT_LABELS = {
    RECRUIT_SOCIAL: "社招",
    RECRUIT_CAMPUS: "校招",
    RECRUIT_INTERN: "实习",
}

_BACKEND_KW = [
    "后端", "服务端", "java", "golang", "c++", "微服务", "中间件",
    "分布式", "高并发", "服务器开发", "server", "backend", "存储研发",
    "数据库", "基础架构", "云原生", "kubernetes", "k8s", "mysql",
    "redis", "kafka", "mongodb", "elasticsearch", "sre", "后台开发",
]
_AI_KW = [
    "算法", "机器学习", "深度学习", "大模型", "llm", "nlp", "推荐系统",
    "计算机视觉", "多模态", "aigc", "强化学习", "模型训练", "模型推理",
    "pytorch", "自然语言处理", "语音识别", "知识图谱",
]
# 这些词一旦出现在标题里，说明岗位重心不是算法/后端研发本身
_NON_ENGINEERING = [
    "产品经理", "产品专家", "产品策划", "产品运营", "产品设计",
    "数据产品", "产品bp", "产品（", "产品(",     # “数据平台产品（AI方向）”
    "运营", "市场", "销售", "hr", "人力", "招聘",
    "法务", "财务", "采购", "设计师", "ui ", "视觉设计", "交互设计",
    "编辑", "客服", "解决方案架构师", "售前", "商务", "渠道经理",
    "项目管理", "版本策划", "游戏策划",
]
_FRONTEND_KW = ["前端", "客户端", "android", "ios", "小程序", "web 开发", "flutter"]


def _tokens(text: str) -> set[str]:
    """切出英文词，用于“ai”这类需要整词匹配的关键词（避免命中 said/detail）。"""
    return set(re.findall(r"[a-z0-9\+#]+", text))


def classify_track(title: str, category: str = "",
                   jd: str = "") -> str:
    """按标题优先、JD 正文兜底来分方向。宁可归 other，也不要误标。

    标题里的非研发信号（产品/运营/售前）直接判 other——这类岗位即使 JD 里
    提到大模型，需要的也不是算法工程能力，混进来会污染技能统计。
    """
    title_l = f"{title} {category}".lower()
    if any(k in title_l for k in _NON_ENGINEERING):
        return TRACK_OTHER
    if any(k in title_l for k in _FRONTEND_KW):
        return TRACK_OTHER

    # AI 优先：“AI Infra SRE”“AI 后台开发”这类岗位同时含两侧关键词，
    # 但招的是 AI 方向的人，归 ai 更贴近“行业缺什么人才”的判断。
    if any(k in title_l for k in _AI_KW) or "ai" in _tokens(title_l):
        return TRACK_AI
    if any(k in title_l for k in _BACKEND_KW):
        return TRACK_BACKEND

    # 标题看不出来时用 JD 正文。命中数须明显领先另一方向，避免
    # “JD 里顺口提一句大模型”的普通岗位被误标。
    body = jd.lower()
    if body:
        ai_hits = sum(1 for k in _AI_KW if k in body)
        be_hits = sum(1 for k in _BACKEND_KW if k in body)
        if ai_hits >= 2 and ai_hits > be_hits:
            return TRACK_AI
        if be_hits >= 2 and be_hits > ai_hits:
            return TRACK_BACKEND
    return TRACK_OTHER


def clean(text: str | None) -> str:
    """压掉 JD 里的多余空白，保留换行结构以便模型读。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)          # 少量接口返回富文本
    text = text.replace("\xa0", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def make_fingerprint(company: str, title: str, cities: list[str],
                     recruit_type: str = RECRUIT_SOCIAL) -> str:
    """岗位指纹。跨轮次去重用。

    招聘类型必须参与：同一家公司的"算法工程师·北京"校招岗和社招岗
    是两个不同的岗位，不区分的话会互相覆盖丢数据。

    抽成模块级函数是为了让数据迁移脚本能复用同一套算法 ——
    两处各写一遍迟早会不一致，那会让整个 skills 缓存静默失效。
    """
    raw = (f"{company}|{title}|{','.join(sorted(cities))}"
           f"|{recruit_type or RECRUIT_SOCIAL}")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Job:
    company: str
    job_id: str
    title: str
    responsibility: str = ""      # 岗位职责
    requirement: str = ""         # 任职要求
    cities: list[str] = field(default_factory=list)
    department: str = ""
    category: str = ""
    education: str = ""
    work_years: str = ""
    publish_date: str = ""        # YYYY-MM-DD
    url: str = ""
    track: str = ""
    recruit_type: str = RECRUIT_SOCIAL

    def __post_init__(self) -> None:
        self.title = clean(self.title)
        self.responsibility = clean(self.responsibility)
        self.requirement = clean(self.requirement)
        if not self.track:
            self.track = classify_track(
                self.title, self.category,
                f"{self.responsibility}\n{self.requirement}",
            )

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(self.company, self.title, self.cities,
                               self.recruit_type)

    @property
    def jd_text(self) -> str:
        parts = [f"【岗位】{self.title}"]
        if self.department:
            parts.append(f"【部门】{self.department}")
        if self.cities:
            parts.append(f"【城市】{'、'.join(self.cities)}")
        if self.work_years or self.education:
            parts.append(f"【要求】{self.work_years} {self.education}".strip())
        if self.responsibility:
            parts.append(f"【职责】\n{self.responsibility}")
        if self.requirement:
            parts.append(f"【任职要求】\n{self.requirement}")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d
