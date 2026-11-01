"""知识图谱数据契约：实体/关系类型枚举、端点约束、抽取结果的解析与校验。

设计铁律（与 docs/知识图谱技术规格.md 一致）：
- 没有原文证据的实体/关系不允许入库；
- evidence_text 必须能在所指文本块中逐字找到（规范化后包含即可）；
- 关系两端类型必须符合受控约束表；不符合的结果直接丢弃并记录告警，绝不"猜"。
"""
import json
import re
from difflib import SequenceMatcher

from pydantic import BaseModel, Field, field_validator

# ---------- 受控枚举 ----------

ENTITY_TYPES = {
    "Paper", "Author", "ResearchProblem", "Algorithm", "Model", "Dataset",
    "Task", "Metric", "Concept", "Finding", "ResearchArea", "Institution", "Venue",
}

# relation_type -> 允许的 (起点类型, 终点类型) 集合
RELATION_ENDPOINTS = {
    "PROPOSES": {("Paper", "Algorithm")},
    "ADDRESSES": {("Paper", "ResearchProblem")},
    "USES": {("Paper", "Dataset"), ("Paper", "Model")},
    "USES_METRIC": {("Paper", "Metric")},
    "BELONGS_TO": {("Paper", "ResearchArea"), ("Algorithm", "Concept")},
    "AUTHORED_BY": {("Paper", "Author")},
    "PUBLISHED_IN": {("Paper", "Venue")},
    "REPORTS": {("Paper", "Finding")},
    "MITIGATES": {("Algorithm", "ResearchProblem")},
    "IMPROVES": {("Algorithm", "Algorithm")},
    "EVALUATED_ON": {("Finding", "Dataset")},
    "USED_FOR": {("Model", "Task")},
    "COMPARES": {("Paper", "Algorithm")},
    "EXTENDS": {("Paper", "Paper")},
}

EVIDENCE_TYPES = {"explicit", "inferred", "similarity"}

# 抽取结果里表示"本文论文"的占位名（存储层负责替换成真实论文实体）
THIS_PAPER = "THIS_PAPER"

# ---------- Pydantic 结构校验（模型输出的形状约束） ----------


class EntityItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entity_type: str
    description: str = Field(default="", max_length=500)
    aliases: list[str] = Field(default_factory=list)
    evidence_block_id: int | None = None
    evidence_text: str = Field(default="", max_length=400)
    confidence: float = 0.5

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.5


class RelationItem(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    relation_type: str
    evidence_type: str = "explicit"
    evidence_block_id: int | None = None
    evidence_text: str = Field(min_length=1, max_length=400)
    confidence: float = 0.5

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.5


# ---------- 文本规范化与证据校验工具 ----------

_PUNCT = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def norm_text(s: str) -> str:
    """证据比对用的强规范化：小写、去掉所有非字母数字（含空格/连字符）。"""
    return _PUNCT.sub("", str(s or "").lower())


def parse_llm_json(raw: str) -> dict | None:
    """从模型输出中稳健地取第一个 JSON 对象；失败返回 None。"""
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def validate_extraction(data: dict, block_map: dict[int, str], paper_title: str = ""):
    """业务规则校验。返回 (entities, relations, warnings)。

    约定：实体列表里不出现 THIS_PAPER（提示词已禁止）；关系的 source/target
    允许 THIS_PAPER，由存储层解析为本文论文实体。
    关系两端必须能在本次实体列表（或 THIS_PAPER）中找到，否则丢弃。
    证据文本必须在所指块（规范化后）逐字包含；找不到时尝试全文扫描回挂；仍找不到则丢弃。
    """
    warnings: list[str] = []
    norm_blocks = {bid: norm_text(t) for bid, t in block_map.items()}

    # --- 实体 ---
    ents: list[EntityItem] = []
    ent_names: dict[str, EntityItem] = {}
    for raw in data.get("entities") or []:
        try:
            item = EntityItem.model_validate(raw)
        except Exception:  # noqa: BLE001
            warnings.append(f"实体格式非法，已丢弃：{str(raw)[:80]}")
            continue
        name = re.sub(r"\s+", " ", item.name).strip()
        if not name or name == THIS_PAPER:
            warnings.append(f"实体名为空或使用了保留字 THIS_PAPER：{name!r}")
            continue
        if item.entity_type not in ENTITY_TYPES:
            warnings.append(f"实体类型非法（{item.entity_type}）：{name}")
            continue
        # 证据定位：所指块必须包含实体名；否则全文扫描；再不行丢弃
        block_id, found = item.evidence_block_id, False
        nm = norm_text(name)
        if block_id in norm_blocks and nm and nm in norm_blocks[block_id]:
            found = True
        elif nm:
            hit = next((bid for bid, t in norm_blocks.items() if nm in t), None)
            if hit is not None:
                block_id, found = hit, True
        if not found:
            warnings.append(f"实体在原文中找不到证据，已丢弃：{name}")
            continue
        item.name = name
        item.evidence_block_id = block_id
        item.aliases = [re.sub(r"\s+", " ", str(a)).strip()
                        for a in (item.aliases or []) if str(a).strip()][:8]
        ents.append(item)
        ent_names.setdefault(norm_text(name), item)

    # --- 关系 ---
    rels: list[RelationItem] = []
    seen_sigs: set[tuple] = set()
    for raw in data.get("relations") or []:
        try:
            item = RelationItem.model_validate(raw)
        except Exception:  # noqa: BLE001
            warnings.append(f"关系格式非法，已丢弃：{str(raw)[:80]}")
            continue
        src, tgt = re.sub(r"\s+", " ", item.source).strip(), re.sub(r"\s+", " ", item.target).strip()
        if item.relation_type not in RELATION_ENDPOINTS:
            warnings.append(f"关系类型非法（{item.relation_type}）：{src}→{tgt}")
            continue
        if not (src and tgt):
            warnings.append("关系端点为空，已丢弃")
            continue
        # 端点可解析：本次实体列表 或 THIS_PAPER
        def _etype(nm: str) -> str | None:
            if nm == THIS_PAPER:
                return "Paper"
            e = ent_names.get(norm_text(nm))
            return e.entity_type if e else None
        st, tt = _etype(src), _etype(tgt)
        if st is None or tt is None:
            warnings.append(f"关系端点未在实体列表中出现，已丢弃：{src}→{tgt}")
            continue
        allowed = RELATION_ENDPOINTS[item.relation_type]
        if (st, tt) not in allowed:
            # 端点名与实体类型不匹配（如拿数据集当算法）；按名字匹配实体的真实类型判
            warnings.append(
                f"关系两端类型不符约束（{st}→{tt}，{item.relation_type}）：{src}→{tgt}")
            continue
        # 证据文本必须逐字存在于原文
        ev = norm_text(item.evidence_text)
        if len(ev) < 15:
            warnings.append(f"证据文本过短，已丢弃：{src}→{tgt} ({item.evidence_text[:40]})")
            continue
        block_id = item.evidence_block_id
        if not (block_id in norm_blocks and ev in norm_blocks[block_id]):
            # 所指块对不上：全文扫描能否找到同样逐字证据；找不到就是模型编的
            hit = next((bid for bid, t in norm_blocks.items() if ev in t), None)
            if hit is None:
                warnings.append(
                    f"证据文本在原文中找不到（可能是模型改写），已丢弃：{src}→{tgt} "
                    f"“{item.evidence_text[:60]}”")
                continue
            block_id = hit
        # The extraction pipeline is deliberately evidence-first: an exact quote is
        # necessary but not sufficient to prove a semantic relation. Do not persist
        # model-inferred claims as if they were source-asserted facts.
        if item.evidence_type != "explicit":
            warnings.append(
                f"关系不是原文明示（{item.evidence_type}），仅作为推测不入库：{src}→{tgt}")
            continue
        item.source, item.target = src, tgt
        item.evidence_block_id = block_id
        if item.evidence_type not in EVIDENCE_TYPES:
            item.evidence_type = "explicit"
        sig = (norm_text(src), norm_text(tgt), item.relation_type)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        rels.append(item)

    return ents, rels, warnings
