"""知识抽取任务（P1）：按章节调用 LLM 抽取科研实体与关系，校验后整体入库。

- 输入复用已有解析结果（sections 边界 + blocks 原文），不重新解析 PDF；
- 每章一次 LLM 调用；所有章节输出合并后做一次全文校验（允许后章引用前章实体，
  证据文本仍要求逐字存在于原文块）；
- 任务状态持久化在 kg_extraction_jobs，支持重试；失败时旧图谱原样保留；
- 同一篇论文同一时间只允许一个任务（幂等防重）。
"""
from ..db import ex, get_settings, row, rows
from ..util import dumps, new_id, now
from .kg_schema import parse_llm_json, validate_extraction
from .translate import chat, engine_info, engine_ready

EXTRACTION_VERSION = "1.0"
PROMPT_VERSION = "v1"
SECTION_CHAR_BUDGET = 7000
MAX_ENTITIES = 120
MAX_RELATIONS = 150

KG_SYSTEM = """你是科研知识图谱抽取助手。从论文的一个章节中抽取科研实体与关系，输出严格 JSON。

只输出 JSON（无代码块标记、无解释、无多余字段）：
{"entities": [{"name": "实体名", "entity_type": "Algorithm", "description": "一句话中文说明",
              "aliases": ["别名"], "evidence_block_id": 123, "confidence": 0.9}],
 "relations": [{"source": "FedProx 或 THIS_PAPER", "target": "Non-IID Data",
               "relation_type": "MITIGATES", "evidence_type": "explicit",
               "evidence_block_id": 123, "evidence_text": "原文支持句逐字摘录", "confidence": 0.9}]}

实体类型只能用：Paper, Author, ResearchProblem, Algorithm, Model, Dataset, Task, Metric,
Concept, Finding, ResearchArea, Institution, Venue。

关系类型与方向约束（source类型→target类型）：
- THIS_PAPER（代表本文）：PROPOSES→Algorithm；ADDRESSES→ResearchProblem；USES→Dataset 或 Model；
  USES_METRIC→Metric；REPORTS→Finding；COMPARES→Algorithm；AUTHORED_BY→Author；
  PUBLISHED_IN→Venue；BELONGS_TO→ResearchArea；EXTENDS→Paper
- Algorithm：MITIGATES→ResearchProblem；IMPROVES→Algorithm；BELONGS_TO→Concept
- Finding：EVALUATED_ON→Dataset
- Model：USED_FOR→Task

铁律（违反任何一条的关系宁可漏抽也不许编造）：
1. 本文论文一律写 "THIS_PAPER"，不要输出论文标题当实体；库外的其他论文用其标题或通行简称（如 FedAvg）。
2. evidence_block_id 必须是输入中 <blk> 标签的真实编号；evidence_text 必须从该块原文【逐字摘录】
   支持该关系的句子（≤160 字符，可截关键部分但禁止改写、禁止翻译）。实体也要给 evidence_block_id。
3. 只抽原文明确表达的：论文没说 A 改进/解决了 B，就不能造 IMPROVES/MITIGATES；
   同属一个领域、名字相似、用了同一数据集，都不构成 IMREATS 类关系。
4. evidence_type：原文明说=explicit；由论文内容谨慎推理（如相关工作指出的问题正是本文所解决）=inferred；
   拿不准就不要输出这条。
5. 实体名用原文规范形式（FedProx、CIFAR-10、Non-IID Data）；同一实体各章保持同一名称与类型。
6. 忽略：参考文献列表、页眉页脚、水印、作者单位以外的机构列表、公式细节。
7. 数量控制：每章至多 20 个实体、20 条关系，只保留对理解论文最重要的。"""


def _sections_with_blocks(pid: str) -> list[dict]:
    """把 blocks 按 sections 边界归章；跳过参考文献章，控制每章字符预算。"""
    blocks = rows("SELECT id, type, en FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    secs = rows("SELECT id, title, title_en, block_start FROM sections "
                "WHERE paper_id=? AND skip=0 ORDER BY order_idx", (pid,))
    start_at = {s["block_start"]: s for s in secs}
    out, cur = [], None
    for b in blocks:
        s = start_at.get(b["id"])
        if s:
            cur = {"title": s["title_en"] or s["title"] or f"章节{s['id']}",
                   "blocks": [], "chars": 0, "truncated": False}
            out.append(cur)
        if cur is None or b["type"] not in ("text", "heading", "list", "table"):
            continue
        t = (b["en"] or "").strip()
        if not t or len(t) < 20:
            continue
        if cur["chars"] + len(t) > SECTION_CHAR_BUDGET:
            cur["truncated"] = True
            continue
        cur["blocks"].append({"id": b["id"], "en": t})
        cur["chars"] += len(t)
    return [s for s in out if s["blocks"]]


def _user_prompt(title: str, sec: dict) -> str:
    parts = [f"【论文】{title}", f"【章节】{sec['title']}"]
    if sec["truncated"]:
        parts.append("（本章过长已截断，只抽取截断前内容中出现的关系）")
    blk = "\n".join(f"<blk id={b['id']}>\n{b['en']}\n</blk>" for b in sec["blocks"])
    parts.append("【文本块】\n" + blk)
    return "\n\n".join(parts)


# ---------- 任务生命周期 ----------

def _mark(job_id: str, **kw):
    sets = ", ".join(f"{k}=?" for k in kw) + ", updated_at=?"
    ex(f"UPDATE kg_extraction_jobs SET {sets} WHERE id=?",
       (*kw.values(), now(), job_id))


def _new_job(pid: str, model: str = "") -> dict:
    jid = new_id()
    ex("INSERT INTO kg_extraction_jobs(id, paper_id, status, extraction_version, "
       "model_name, prompt_version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
       (jid, pid, "pending", EXTRACTION_VERSION, model, PROMPT_VERSION, now(), now()))
    return row("SELECT * FROM kg_extraction_jobs WHERE id=?", (jid,))


def create_job(pid: str) -> dict:
    """手动触发：校验论文可抽取且无进行中任务。"""
    p = row("SELECT status, library_role FROM papers WHERE id=?", (pid,))
    if not p:
        raise RuntimeError("论文不存在")
    if p.get("library_role", "normal") != "normal":
        raise RuntimeError("该论文已标记为测试/排除数据，不参与知识图谱抽取")
    if p["status"] in ("pending_parse", "parsing"):
        raise RuntimeError("论文还在解析中，解析完成后再抽取")
    if not rows("SELECT id FROM blocks WHERE paper_id=? LIMIT 1", (pid,)):
        raise RuntimeError("论文没有已解析的正文")
    busy = row("SELECT id FROM kg_extraction_jobs WHERE paper_id=? AND "
               "status IN ('pending','running')", (pid,))
    if busy:
        raise RuntimeError("该论文已有抽取任务在进行中")
    info = engine_info()
    return _new_job(pid, model=f"{info['provider']}/{info['model']}")


def rebuild_missing() -> list[str]:
    """为已解析但缺当前版本抽取结果的论文建任务（幂等，不重复抽取）。"""
    out = []
    for p in rows("SELECT id FROM papers WHERE status IN ('parsed','ready') AND COALESCE(library_role, 'normal')='normal'"):
        done = row("SELECT id FROM kg_extraction_jobs WHERE paper_id=? AND "
                   "status='completed' AND prompt_version=?",
                   (p["id"], PROMPT_VERSION))
        busy = row("SELECT id FROM kg_extraction_jobs WHERE paper_id=? AND "
                   "status IN ('pending','running')", (p["id"],))
        if done or busy:
            continue
        out.append(_new_job(p["id"])["id"])
    return out


async def run_job(job_id: str):
    """执行抽取任务。任何失败都只标记任务，不动已有图谱。"""
    job = row("SELECT * FROM kg_extraction_jobs WHERE id=?", (job_id,))
    if not job:
        return
    pid = job["paper_id"]
    paper_scope = row("SELECT library_role FROM papers WHERE id=?", (pid,))
    if not paper_scope or paper_scope.get("library_role", "normal") != "normal":
        _mark(job_id, status="cancelled", stage="", error_message="论文已标记为测试/排除数据，跳过图谱抽取")
        return
    info = engine_info()
    ok, err = engine_ready(info)
    if not ok:
        _mark(job_id, status="failed", stage="", error_message=f"抽取引擎未就绪：{err}（设置页配置后重试）")
        return
    _mark(job_id, status="running", stage="准备输入", error_message="", progress=0)
    paper = row("SELECT title, title_zh FROM papers WHERE id=?", (pid,))
    title = (paper["title_zh"] or paper["title"] or "").strip() if paper else ""
    secs = _sections_with_blocks(pid)
    if not secs:
        _mark(job_id, status="failed", stage="", error_message="论文没有可抽取的正文内容")
        return
    warnings: list[str] = []
    combined: dict = {"entities": [], "relations": []}
    union_blocks: dict[int, str] = {}
    for i, sec in enumerate(secs):
        _mark(job_id, stage=f"抽取 {i + 1}/{len(secs)}：{sec['title'][:40]}",
              progress=round(i / len(secs), 3))
        for b in sec["blocks"]:
            union_blocks[b["id"]] = b["en"]
        msgs = [{"role": "system", "content": KG_SYSTEM},
                {"role": "user", "content": _user_prompt(title, sec)}]
        try:
            raw = await chat(info, msgs)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"章节“{sec['title']}”调用失败：{str(e)[:120]}")
            continue
        data = parse_llm_json(raw)
        if not data:
            warnings.append(f"章节“{sec['title']}”模型输出无法解析为 JSON")
            continue
        combined["entities"] += (data.get("entities") or [])[:30]
        combined["relations"] += (data.get("relations") or [])[:30]
    ents, rels, vw = validate_extraction(combined, union_blocks, title)
    warnings += vw
    if not ents and not rels:
        detail = "；".join(warnings[:3]) or "模型没有返回任何通过校验的实体/关系"
        _mark(job_id, status="failed", stage="",
              error_message=f"全部抽取未通过校验（已有图谱保持不变）：{detail}")
        return
    # 分类可能在耗时的 LLM 调用期间被用户修改；写库前再次检查。
    latest_scope = row("SELECT library_role FROM papers WHERE id=?", (pid,))
    if not latest_scope or latest_scope.get("library_role", "normal") != "normal":
        _mark(job_id, status="cancelled", stage="", error_message="抽取期间论文被标记为测试/排除数据，结果未写入图谱")
        return
    from .kg_store import replace_paper_extraction
    stats = replace_paper_extraction(pid, ents[:MAX_ENTITIES], rels[:MAX_RELATIONS])
    stats["warnings"] = warnings[:20]
    stats["model"] = f"{info['provider']}/{info['model']}"
    _mark(job_id, status="completed", stage="", progress=1.0, stats_json=dumps(stats))
    # 新实体补中文名（尽力而为，失败不影响抽取结果）
    try:
        await translate_missing_names()
    except Exception:  # noqa: BLE001
        pass


async def run_jobs_sequential(job_ids: list[str]):
    for jid in job_ids:
        try:
            await run_job(jid)
        except Exception as e:  # noqa: BLE001
            _mark(jid, status="failed", error_message=str(e)[:300])


async def auto_extract(pid: str):
    """解析/翻译完成后自动触发（kg_auto_extract=1 且云端引擎可用时；mock 引擎跳过）。"""
    if get_settings().get("kg_auto_extract", "1") != "1":
        return
    info = engine_info()
    ok, _ = engine_ready(info)
    if not ok or info["provider"] == "mock":
        return
    p = row("SELECT status, library_role FROM papers WHERE id=?", (pid,))
    if not p or p.get("library_role", "normal") != "normal" or p["status"] not in ("parsed", "ready"):
        return
    if row("SELECT id FROM kg_extraction_jobs WHERE paper_id=? AND "
           "status IN ('pending','running')", (pid,)):
        return
    job = _new_job(pid, model=f"{info['provider']}/{info['model']}")
    await run_job(job["id"])


# ---------- 实体中文名翻译（图谱展示用：一次性批量、结果缓存进库不重复花钱） ----------

NAME_ZH_SYSTEM = """你是学术术语翻译助手。把给定的科研实体名翻译成简洁通用的中文术语。
规则：算法/模型/系统的专名保留通行写法（如 FedProx、BERT、OpenHands 可不翻）；
数据集名保留原名（如 CIFAR-10）；研究领域、技术概念、方法类名词翻成中文学界通用叫法；
人名保留原文；论文标题翻成中文标题。只输出 JSON：{"原文名": "中文名", ...}，不要解释。"""


async def translate_missing_names(limit: int = 300) -> int:
    """为还没有中文名的实体批量翻译（每批 60 个）。返回更新数量。"""
    ents = rows("SELECT id, canonical_name FROM kg_visible_entities "
                "WHERE IFNULL(name_zh,'')='' LIMIT ?", (int(limit),))
    if not ents:
        return 0
    info = engine_info()
    ok, _ = engine_ready(info)
    if not ok or info["provider"] == "mock":
        return 0
    updated = 0
    for i in range(0, len(ents), 60):
        batch = ents[i:i + 60]
        names = [e["canonical_name"] for e in batch]
        msgs = [{"role": "system", "content": NAME_ZH_SYSTEM},
                {"role": "user", "content": "\n".join(names)}]
        try:
            raw = await chat(info, msgs, max_tokens=2000)
        except Exception:  # noqa: BLE001
            continue
        import json as _json
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", raw)
        if not m:
            continue
        try:
            mapping = _json.loads(m.group(0))
        except _json.JSONDecodeError:
            continue
        for e in batch:
            zh = str(mapping.get(e["canonical_name"]) or "").strip()
            if zh:
                ex("UPDATE kg_entities SET name_zh=?, updated_at=? WHERE id=?",
                   (zh[:120], now(), e["id"]))
                updated += 1
    return updated
