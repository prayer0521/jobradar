"""分类器回归测试：这些用例全部来自实跑中真实误判过的岗位标题。"""
from jobradar.models import classify_track, TRACK_AI, TRACK_BACKEND, TRACK_OTHER


def t(title, category="", jd=""):
    return classify_track(title, category, jd)


class TestAI:
    def test_大模型算法岗(self):
        assert t("微信搜索-LLM大模型算法工程师") == TRACK_AI
        assert t("混元AI搜索算法工程师（北京/深圳）") == TRACK_AI
        assert t("微信视频号-AIGC算法工程师") == TRACK_AI

    def test_推荐与语音也算AI(self):
        assert t("公益平台-推荐算法") == TRACK_AI
        assert t("微信输入法-语音识别算法工程师 / 研究员") == TRACK_AI

    def test_AI基建岗优先归AI而非后端(self):
        # 标题同时含 AI 与 SRE/后台开发，招的是 AI 方向的人
        assert t("AI Infra SRE工程师（深圳/北京）") == TRACK_AI
        assert t("AI后台开发工程师") == TRACK_AI


class TestBackend:
    def test_服务端与基础设施(self):
        assert t("高级服务端开发工程师") == TRACK_BACKEND
        assert t("微信-分布式文件系统研发工程师") == TRACK_BACKEND
        assert t("Go/PHP服务端研发工程师（J69711）") == TRACK_BACKEND

    def test_数据库运维靠JD兜底(self):
        # 标题里“数据库”命中；曾因关键词表缺 mongodb/数据库而漏判
        assert t("数据库运维研发工程师(MongoDB)") == TRACK_BACKEND
        assert t("云数据库内核研发工程师(北京/上海/深圳)") == TRACK_BACKEND


class TestOther:
    def test_产品与运营岗不算技术(self):
        # 这些曾被“AI”“数据”字样误拉进 ai
        assert t("数据平台产品（AI方向）") == TRACK_OTHER
        assert t("资深产品策划（AI云协作）") == TRACK_OTHER
        assert t("数据产品BP（营销方向）") == TRACK_OTHER
        assert t("小红书运营专员") == TRACK_OTHER

    def test_游戏美术与策划不算后端(self):
        assert t("高级/资深技术美术工程师-渲染向") == TRACK_OTHER
        assert t("关卡技术策划（归唐）") == TRACK_OTHER

    def test_英文ai需整词匹配(self):
        # “detail/said”里的 ai 不应命中
        assert t("Retail Operations Specialist") == TRACK_OTHER


class TestJDFallback:
    def test_JD命中数需明显领先(self):
        # 后端岗 JD 顺口提一句大模型，不应翻成 ai
        jd = "负责高并发服务端开发，熟悉 MySQL、Redis、Kafka，了解大模型应用即可"
        assert t("资深工程师", jd=jd) == TRACK_BACKEND

    def test_JD为空时归other(self):
        assert t("某岗位", jd="") == TRACK_OTHER
