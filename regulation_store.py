"""本地安全规程知识库：内置极小规程集 → sentence-transformers 向量 → FAISS 检索。

⚠️ 说明：下列条款为**演示用模拟文本**，参照《煤矿安全规程》的体例与常见指标编写，
并非真实规程原文，不得用于实际生产决策。

检索层对外只暴露两个函数：
    search_regulation(query, top_k=3) -> list[Clause]
    get_clause(clause_id)             -> dict | None
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections import Counter
from typing import Any

# HuggingFace 直连在本机不可用（502），默认走镜像；可用 HF_ENDPOINT 覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

DEFAULT_EMBEDDING_MODEL = os.environ.get("REGULATION_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# --------------------------------------------------------------------------- #
# 模拟规程集（16 条，覆盖通风 / 瓦斯 / 有限空间 / 机电温度）
# --------------------------------------------------------------------------- #
REGULATIONS: tuple[dict[str, Any], ...] = (
    {
        "clause_id": "第一百零一条",
        "topic": "通风",
        "title": "主要通风机连续运转与停风撤人",
        "text": "主要通风机必须连续运转，备用通风机必须能在10分钟内启动。主要通风机停止运转时，"
        "必须立即切断井下电源、撤出人员，并报告矿调度室。",
    },
    {
        "clause_id": "第一百零二条",
        "topic": "通风",
        "title": "配风量与回风流超限处置",
        "text": "矿井必须按实际需要风量配风。采掘工作面回风流中甲烷浓度超过1.0%或二氧化碳浓度"
        "超过1.5%时，必须停止作业、撤出人员、切断电源并采取措施处理。",
    },
    {
        "clause_id": "第一百零三条",
        "topic": "通风",
        "title": "严禁微风无风作业",
        "text": "严禁在微风、无风状态下作业。掘进工作面风量不足，或风筒末端距工作面距离超过"
        "作业规程规定时，必须停止作业、撤出人员。",
    },
    {
        "clause_id": "第一百零四条",
        "topic": "通风",
        "title": "局部通风机三专两闭锁",
        "text": "局部通风机必须实行三专两闭锁：专用变压器、专用开关、专用线路，风电闭锁与瓦斯电"
        "闭锁。局部通风机停风时，必须自动切断其供风区域内所有非本质安全型电气设备电源。",
    },
    {
        "clause_id": "第一百三十五条",
        "topic": "瓦斯",
        "title": "甲烷浓度超限停止作业",
        "text": "采掘工作面及其他作业地点风流中甲烷浓度达到1.0%时，必须停止用电钻打眼；达到"
        "1.5%时，必须停止工作、切断电源、撤出人员，并由通风部门采取措施处理。",
    },
    {
        "clause_id": "第一百三十六条",
        "topic": "瓦斯",
        "title": "瓦斯检查频次与记录",
        "text": "高瓦斯矿井采掘工作面每班至少检查瓦斯3次，低瓦斯矿井每班至少检查2次。检查结果"
        "必须记入瓦斯检查手册和现场牌板，做到手册、牌板、调度台账三对口。",
    },
    {
        "clause_id": "第一百三十七条",
        "topic": "瓦斯",
        "title": "瓦斯超限作业的处置",
        "text": "瓦斯超限作业属于严重违章行为。发现瓦斯超限必须立即停止作业、撤出人员、切断电源，"
        "查明原因并制定专项措施，未处理完毕不得恢复作业。",
    },
    {
        "clause_id": "第一百三十八条",
        "topic": "瓦斯",
        "title": "瓦斯涌出异常的判定与撤人",
        "text": "出现下列情形之一，判定为瓦斯涌出异常：甲烷浓度在1分钟内上升0.5个百分点以上；"
        "掘进工作面回风流甲烷浓度持续上升；瓦斯浓度忽高忽低且伴随风量下降。必须立即停止作业、"
        "撤出人员、加强通风并报告矿调度室。",
    },
    {
        "clause_id": "第一百三十九条",
        "topic": "瓦斯",
        "title": "盲巷与废巷管理",
        "text": "盲巷、废巷必须及时封闭，或设置栅栏、警示标志并禁止人员进入。恢复盲巷作业前必须先"
        "通风、检测气体，确认合格后方可进入。",
    },
    {
        "clause_id": "第一百四十条",
        "topic": "瓦斯",
        "title": "排放瓦斯专项措施",
        "text": "排放瓦斯必须编制专项安全技术措施，由通风部门负责人现场指挥，控制排放浓度与排放量，"
        "严禁一风吹。排放期间必须停止相关区域作业并撤出人员。",
    },
    {
        "clause_id": "第一百四十五条",
        "topic": "火灾",
        "title": "一氧化碳浓度限值",
        "text": "井下作业地点空气中一氧化碳浓度不得超过24ppm（0.0024%）。超过限值时必须停止作业、"
        "撤出人员、佩戴自救器撤离，并查明一氧化碳来源。",
    },
    {
        "clause_id": "第一百四十六条",
        "topic": "火灾",
        "title": "自然发火标志气体监测",
        "text": "开采容易自燃煤层必须建立自然发火监测系统，连续监测一氧化碳、二氧化碳、烯烃等"
        "标志性气体。标志气体浓度异常升高且氧气浓度下降时，必须按自然发火预兆处理。",
    },
    {
        "clause_id": "第一百五十条",
        "topic": "有限空间",
        "title": "有限空间进入前气体检测",
        "text": "进入有限空间（密闭容器、地下管沟、污水池、巷道盲区等）作业前，必须先通风并检测"
        "氧气、甲烷、一氧化碳、硫化氢。合格标准：氧气19.5%~23.5%、甲烷低于1.0%、一氧化碳低于"
        "24ppm、硫化氢低于10ppm，方可进入。",
    },
    {
        "clause_id": "第一百五十一条",
        "topic": "有限空间",
        "title": "有限空间作业监护与持续通风",
        "text": "有限空间作业必须设专人监护，作业期间保持持续通风与连续气体监测，监护人不得离岗，"
        "并配备应急救援装备。",
    },
    {
        "clause_id": "第一百五十二条",
        "topic": "有限空间",
        "title": "有限空间作业审批制度",
        "text": "有限空间作业实行作业审批制度，作业票必须经安全负责人批准后方可作业。作业中断超过"
        "30分钟再次进入前，必须重新通风检测并确认合格。",
    },
    {
        "clause_id": "第四百三十条",
        "topic": "机电",
        "title": "设备轴承温度限值",
        "text": "机电设备轴承温度不得超过75℃，温升不得超过40K。温度超限必须停机检查，禁止设备"
        "带病运转。",
    },
    {
        "clause_id": "第四百三十一条",
        "topic": "机电",
        "title": "设备振动烈度限值与停机要求",
        "text": "机电设备振动速度有效值不得超过4.5mm/s；达到4.5mm/s时必须安排停机检查轴承与"
        "联轴器，超过7.1mm/s时立即停机，禁止带病运行。振动持续上升而未超过限值时，应加密监测"
        "并排查基础松动、对中不良与轴承磨损。",
    },
)

_BY_ID: dict[str, dict[str, Any]] = {c["clause_id"]: c for c in REGULATIONS}

_LOCK = threading.Lock()
_MODEL = None
_INDEX = None
_CLAUSE_IDS: list[str] = []


def get_clause(clause_id: str) -> dict[str, Any] | None:
    """按条款编号精确取原文；支持「第四百三十条」以及少量别名（如「第430条」）。"""
    cid = (clause_id or "").strip().strip("《》 ")
    if cid in _BY_ID:
        return _BY_ID[cid]
    digits = cid.removeprefix("第").removesuffix("条")
    if digits.isdigit():
        return _BY_ID.get(_chinese_to_clause(int(digits)))
    return None


def all_clauses() -> list[dict[str, Any]]:
    return list(REGULATIONS)


def clause_ids() -> list[str]:
    return [c["clause_id"] for c in REGULATIONS]


# --------------------------------------------------------------------------- #
# 向量索引（懒加载，进程内单例）
# --------------------------------------------------------------------------- #
def _model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer  # 延迟导入：重依赖

        _MODEL = SentenceTransformer(DEFAULT_EMBEDDING_MODEL, device="cpu")
    return _MODEL


def _build_index():
    global _INDEX, _CLAUSE_IDS
    if _INDEX is not None:
        return
    import faiss
    import numpy as np

    model = _model()
    texts = [f"{c['title']}。{c['text']}" for c in REGULATIONS]
    vectors = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    vectors = np.asarray(vectors, dtype="float32")
    index = faiss.IndexFlatIP(vectors.shape[1])  # 归一化后内积 == 余弦相似度
    index.add(vectors)
    _INDEX = index
    _CLAUSE_IDS = [c["clause_id"] for c in REGULATIONS]


def search_regulation(query: str, top_k: int = 3, mode: str | None = None) -> list[dict[str, Any]]:
    """返回 top-k 规程片段：[{clause_id, title, text, topic, score, vector_score, lexical_score}]。

    mode:
      - "hybrid"（默认）：FAISS 余弦 + 字符 n-gram BM25 混合。
        all-MiniLM-L6-v2 是英文模型，对中文条款的纯语义排序接近随机（实测 top-1 常错），
        所以用词法分补偿；换成多语言 embedding 时可设 mode="vector"。
      - "vector"：纯 FAISS 语义检索。
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    if top_k <= 0:
        return []

    mode = mode or os.environ.get("REGULATION_RETRIEVAL", "hybrid")

    with _LOCK:
        _build_index()
        import numpy as np

        q = _model().encode([query], normalize_embeddings=True, convert_to_numpy=True)
        q = np.asarray(q, dtype="float32")
        scores, idxs = _INDEX.search(q, len(_CLAUSE_IDS))

    vector_scores = {int(i): float(s) for s, i in zip(scores[0], idxs[0]) if i >= 0}
    lexical = _bm25_scores(query) if mode == "hybrid" else {}

    hits: list[dict[str, Any]] = []
    for i, clause in enumerate(REGULATIONS):
        vector_score = vector_scores.get(i, 0.0)
        lexical_score = lexical.get(i, 0.0)
        score = vector_score if mode == "vector" else 0.35 * vector_score + 0.65 * lexical_score
        hits.append(
            {
                "clause_id": clause["clause_id"],
                "title": clause["title"],
                "text": clause["text"],
                "topic": clause["topic"],
                "score": round(float(score), 4),
                "vector_score": round(vector_score, 4),
                "lexical_score": round(lexical_score, 4),
            }
        )

    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[: min(top_k, len(hits))]


# --------------------------------------------------------------------------- #
# 词法检索（BM25，中文按字符 bigram 切分）——补偿英文 embedding 对中文的语义盲区
# --------------------------------------------------------------------------- #
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9._%-]*")


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens = _ASCII_RE.findall(text)
    for seg in _CJK_RE.findall(text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def _bm25_scores(query: str, k1: float = 1.5, b: float = 0.75) -> dict[int, float]:
    docs = [_tokenize(f"{c['title']}。{c['text']}") for c in REGULATIONS]
    n_docs = len(docs)
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
    avgdl = sum(len(d) for d in docs) / n_docs or 1.0

    q_terms = _tokenize(query)
    raw: dict[int, float] = {}
    for i, doc in enumerate(docs):
        tf = Counter(doc)
        dl = len(doc)
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        raw[i] = score

    top = max(raw.values()) if raw else 0.0
    if top <= 0:
        return {i: 0.0 for i in raw}
    return {i: v / top for i, v in raw.items()}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
_CN_DIGITS = "零一二三四五六七八九"


def _chinese_to_clause(n: int) -> str:
    """430 -> 第四百三十条（够用即可，不做完整中文数字库）。"""
    if n <= 0:
        return ""

    def section(num: int) -> str:
        if num == 0:
            return ""
        if num < 10:
            return _CN_DIGITS[num]
        if num < 20:
            return "十" + (_CN_DIGITS[num % 10] if num % 10 else "")
        if num < 100:
            return _CN_DIGITS[num // 10] + "十" + (_CN_DIGITS[num % 10] if num % 10 else "")
        return _CN_DIGITS[num // 100] + "百" + (section(num % 100) or "")

    hundreds, rest = divmod(n, 100)
    body = ""
    if hundreds:
        body += _CN_DIGITS[hundreds] + "百"
        if rest and rest < 10:
            body += "零"
    body += section(rest)
    return f"第{body}条"
