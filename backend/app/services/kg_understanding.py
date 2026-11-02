"""V3 paper-understanding pipeline.

The v2 entity/relation extractor remains untouched. V3 creates an isolated,
reviewable understanding draft; it never writes kg_entities/kg_relations.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..db import db, row, rows
from ..util import dumps, loads, new_id, now
from .translate import chat, engine_info, engine_ready

logger = logging.getLogger(__name__)

VERSION = "3.0"
V3_RELATION_TYPES = {"CONSISTS_OF", "SUPPORTS", "PROVES", "DERIVES", "NECESSARY_FOR", "SUFFICIENT_FOR", "IMPLIES", "HAS_NECESSARY_CONDITION", "JOINTLY_SUFFICIENT_FOR", "EQUIVALENT_TO", "NOT_REQUIRED", "SPECIALIZATION_OF", "EXCLUDES", "CONTRADICTS"}
MAX_SECTION_CHARS = 5200
MAX_INPUT_CHARS = 26000

UNDERSTANDING_SYSTEM = """你是一名严谨的学术论文分析员。你的任务是先理解整篇论文，而不是罗列名词。
必须区分作者真正提出的贡献、为贡献服务的支撑内容、普通背景/实验配置。
论文类型只能是 theory、algorithm、system、empirical、survey、other。
只返回 JSON，不要 Markdown。结构：
{
 "paper_type":"theory|algorithm|system|empirical|survey|other",
 "problem":"论文试图解决的核心问题，若证据不足写明不确定",
 "thesis":"作者的核心主张/中心思想",
 "contributions":[
  {"id":"c1","title":"简洁贡献名","description":"具体说明","kind":"Theorem|Proposition|Lemma|Definition|Assumption|Condition|Algorithm|Model|System|Finding|Dataset|Other","importance":"core|supporting|context","parent_id":null,"evidence":[{"block_id":123,"quote":"原文连续逐字摘录"}]}
 ],
 "theoretical_framework":[{"name":"名称","role":"定义/假设/定理/条件/证明等","evidence":[{"block_id":123,"quote":"原文逐字摘录"}]}],
 "method_architecture":[{"name":"模块或步骤","description":"作用","evidence":[{"block_id":123,"quote":"原文逐字摘录"}]}],
 "key_findings":[{"claim":"论文报告的结论","evidence":[{"block_id":123,"quote":"原文逐字摘录"}]}],
 "limitations":["论文明确承认的局限；没有明确说明则为空"],
 "uncertainties":["无法从提供内容确定的事项"]
}
规则：
1. 核心贡献必须是作者声称的新东西，不能把引用的已有方法、常见数据集、普通实验配置当贡献。
2. 理论论文优先识别 Definition/Assumption/Condition/Proposition/Theorem/Lemma/Corollary/Proof；若作者明确列出四个或多个条件，必须逐项建立独立节点并保留原始编号/名称，不要压成泛泛的“框架”，也不要自行补写条件。
3. 算法论文重点识别问题定义、算法本身、关键模块、目标函数/优化策略、复杂度和作者报告的性能主张；不要把常用 backbone 或数据集当创新。
4. 系统论文重点识别系统架构、组件职责、数据/控制流、设计约束和验证结果；不要把部署环境配置当贡献。
5. 实证论文重点识别研究假设、实验设计、变量、数据来源、指标和被结果支持的发现；相关性不能写成因果。
6. 综述论文重点识别作者提出的分类体系、比较维度、综合结论和研究空白；被综述的已有方法不是综述作者的新贡献。
7. 每个贡献最多2条证据；每条证据必须来自输入块，quote 必须逐字复制，block_id 必须真实。
8. importance=core 只用于论文主要创新；supporting 是定义、关键条件、模块、证明或关键结果；context 是背景信息。宁缺毋滥。
9. 不确定就明确列入 uncertainties，不要猜测。"""

SECTION_SYSTEM = """你是论文结构分析员。只总结本章节在整篇论文中的学术角色和核心信息。
不要抽取实体清单，不要将相关工作误判为本文贡献。输出 JSON：
{"section_role":"abstract|introduction|related_work|preliminaries|method|theory|experiment|discussion|conclusion|references|other",
 "summary":"最多120字的中文摘要","claims":["本章明确提出/证明/报告的关键主张，最多5条"],
 "candidate_contributions":["仅列本章可能属于作者贡献的内容，最多3条"],
 "open_questions":["本章无法确定的事项"]}
若为参考文献、致谢或附录噪声，section_role 标记 references/other。"""

RELATION_SYSTEM = """你是学术关系语义审查员。判断给定原文引文是否真正蕴含 source --relation--> target。
仅输出 JSON：{"verdict":"entailed|not_entailed|uncertain","reason":"中文理由"}。
必须检查关系方向、谓词含义和引文语义。仅仅同时出现两个实体、使用某数据集、引用某方法，都不证明改进/解决/提出/包含关系。
若证据只是“evaluate method on dataset”，只支持 EVALUATED_ON/USES，不支持 IMPROVES。
没有明确语义蕴含时，选择 not_entailed 或 uncertain。"""

def ensure_tables() -> None:
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS kg_understanding_drafts(
            id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', model_name TEXT DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}', warnings_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(paper_id, version, id))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_understanding_paper ON kg_understanding_drafts(paper_id, created_at)")

def _json(raw: str) -> dict | None:
    """Extract the first complete JSON object without breaking on braces in strings."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _persist_progress(
    draft_id: str,
    model_name: str,
    payload: dict,
    warnings: list[str],
    status: str = "running",
) -> None:
    """Persist a safe checkpoint in the isolated V3 table."""
    with db() as c:
        c.execute(
            "UPDATE kg_understanding_drafts SET status=?, model_name=?, payload_json=?, "
            "warnings_json=?, updated_at=? WHERE id=?",
            (status, model_name, dumps(payload), dumps(warnings), now(), draft_id),
        )

def _sections(pid: str) -> list[dict]:
    blocks = rows("SELECT id,type,en FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    sections = rows("SELECT id,title,title_en,block_start,skip FROM sections WHERE paper_id=? ORDER BY order_idx", (pid,))
    starts = {s["block_start"]: s for s in sections if s.get("block_start") is not None}
    out, current = [], None
    for b in blocks:
        s = starts.get(b["id"])
        if s:
            title = s.get("title_en") or s.get("title") or "Untitled section"
            is_backmatter = bool(re.search(r"references|bibliography|acknowledg(e)?ments|参考文献|致谢", title, re.I))
            current = {"title": title, "skip": bool(s.get("skip")) or is_backmatter,
                       "blocks": [], "chars": 0, "truncated": False}
            out.append(current)
        if current is None or current["skip"] or b["type"] not in ("text","heading","list","table"):
            continue
        text = (b.get("en") or "").strip()
        if len(text) < 25: continue
        remaining = MAX_SECTION_CHARS - current["chars"]
        if remaining <= 0: continue
        if len(text) > remaining:
            current["truncated"] = True
        text = text[:remaining]
        current["blocks"].append({"id": b["id"], "text": text})
        current["chars"] += len(text)
    return [s for s in out if s["blocks"]]

def _evidence_index(sections: list[dict]) -> dict[int,str]:
    return {b["id"]: b["text"] for s in sections for b in s["blocks"]}

def _validate_evidence(payload: dict, block_map: dict[int,str]) -> list[str]:
    warnings = []
    def check_items(items, label):
        valid = []
        for item in items if isinstance(items,list) else []:
            if not isinstance(item,dict): continue
            evidence = []
            for ev in item.get("evidence",[]) if isinstance(item.get("evidence"),list) else []:
                try: bid = int(ev.get("block_id"))
                except (TypeError,ValueError,AttributeError): continue
                quote = str(ev.get("quote") or "").strip()
                source = block_map.get(bid,"")
                if len(quote) >= 12 and quote in source:
                    evidence.append({"block_id":bid,"quote":quote})
                else:
                    warnings.append(f"{label}“{str(item.get('title') or item.get('name') or item.get('claim') or '')[:50]}”证据无法逐字定位，已移除")
            item["evidence"] = evidence[:2]
            if evidence:
                valid.append(item)
            else:
                warnings.append(f"{label}缺少可定位原文证据，已移除")
        return valid
    payload["contributions"] = check_items(payload.get("contributions"), "贡献")
    payload["theoretical_framework"] = check_items(payload.get("theoretical_framework"), "理论结构")
    payload["method_architecture"] = check_items(payload.get("method_architecture"), "方法结构")
    payload["key_findings"] = check_items(payload.get("key_findings"), "结论")
    return warnings

def _normalise_contributions(payload: dict) -> list[str]:
    warnings = []
    raw = payload.get("contributions", [])
    valid, seen = [], set()
    allowed_importance = {"core", "supporting", "context"}
    for node in raw if isinstance(raw, list) else []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id or node_id in seen:
            warnings.append("贡献树存在空 ID 或重复 ID，重复节点已移除")
            continue
        seen.add(node_id)
        if node.get("importance") not in allowed_importance:
            node["importance"] = "supporting"
        node["id"] = node_id
        valid.append(node)
    ids = {n["id"] for n in valid}
    for node in valid:
        parent = node.get("parent_id")
        if parent and (str(parent) not in ids or str(parent) == node["id"]):
            node["parent_id"] = None
            warnings.append(f"贡献节点 {node['id']} 的父节点无效，已置为根节点")
    by_id = {n["id"]: n for n in valid}
    for node in valid:
        visited, cursor = set(), node["id"]
        while cursor and cursor in by_id:
            if cursor in visited:
                node["parent_id"] = None
                warnings.append("贡献树检测到循环父子关系，已断开一条边")
                break
            visited.add(cursor)
            parent = by_id[cursor].get("parent_id")
            cursor = str(parent) if parent else ""
    payload["contributions"] = valid[:80]
    return warnings


def _normalise_relations(payload: dict, block_map: dict[int,str]) -> list[str]:
    warnings = []
    nodes = {str(x.get("id")): x for x in payload.get("contributions",[]) if x.get("id")}
    relations = payload.get("relations",[])
    accepted = []
    for rel in relations if isinstance(relations,list) else []:
        if not isinstance(rel,dict): continue
        src, tgt = str(rel.get("source_id") or ""), str(rel.get("target_id") or "")
        ev = rel.get("evidence") or {}
        try: bid = int(ev.get("block_id"))
        except (TypeError,ValueError,AttributeError): bid = -1
        quote = str(ev.get("quote") or "").strip()
        if src not in nodes or tgt not in nodes or src == tgt or len(quote) < 12 or quote not in block_map.get(bid,""):
            warnings.append("一条候选关系缺少有效端点或原文证据，已移除")
            continue
        relation_type = str(rel.get("relation_type") or "").upper()
        if relation_type not in V3_RELATION_TYPES:
            warnings.append(f"未知关系类型 {relation_type!r}，已移除")
            continue
        accepted.append({"source_id":src,"target_id":tgt,"relation_type":relation_type,
                         "evidence":{"block_id":bid,"quote":quote},"verification":"unverified","reason":""})
    payload["relations"] = accepted
    return warnings

async def run_understanding(draft_id: str) -> None:
    """Run the isolated V3 pipeline and persist checkpoints for diagnosis/retry."""
    ensure_tables()
    draft = row("SELECT * FROM kg_understanding_drafts WHERE id=?", (draft_id,))
    if not draft:
        return

    pid = draft["paper_id"]
    info = engine_info()
    model_name = f"{info.get('provider') or 'unknown'}/{info.get('model') or 'unknown'}"
    partial_payload: dict[str, Any] = {"version": VERSION, "section_map": []}
    section_notes: list[dict] = []
    warnings: list[str] = []
    stage = "engine_check"
    _persist_progress(draft_id, model_name, partial_payload, warnings)

    ok, err = engine_ready(info)
    if not ok:
        warnings.append(f"模型未就绪：{err}")
        _persist_progress(draft_id, model_name, partial_payload, warnings, "failed")
        return

    paper = row("SELECT title,title_zh FROM papers WHERE id=?", (pid,))
    if not paper:
        warnings.append("论文记录不存在，无法生成理解档案")
        _persist_progress(draft_id, model_name, partial_payload, warnings, "failed")
        return

    sections = _sections(pid)
    if not sections:
        warnings.append("没有可用的已解析正文")
        _persist_progress(draft_id, model_name, partial_payload, warnings, "failed")
        return

    title = paper.get("title_zh") or paper.get("title") or "未命名论文"
    partial_payload["paper_title"] = title
    warnings.extend(
        f"章节“{section['title']}”超过单章输入预算，已截断部分正文"
        for section in sections if section.get("truncated")
    )

    try:
        # Phase 1: section summaries. A single bad chapter must not erase the others.
        stage = "section_summary"
        for section in sections:
            prompt = f"论文：{title}\n章节：{section['title']}\n" + "\n".join(
                f"<block id={block['id']}>\n{block['text']}\n</block>"
                for block in section["blocks"]
            )
            try:
                raw_summary = await chat(
                    info,
                    [{"role": "system", "content": SECTION_SYSTEM},
                     {"role": "user", "content": prompt}],
                    max_tokens=1800,
                )
                result = _json(raw_summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "V3 section summary failed: draft_id=%s section=%s error=%s",
                    draft_id, section["title"], type(exc).__name__,
                )
                warnings.append(
                    f"章节“{section['title']}”摘要请求失败（{type(exc).__name__}）：{str(exc)[:180]}"
                )
                continue

            if not result:
                warnings.append(f"章节“{section['title']}”摘要未返回合法 JSON")
                continue

            section_notes.append({"title": section["title"], **result})
            partial_payload["section_map"] = section_notes
            partial_payload["progress_stage"] = stage
            _persist_progress(draft_id, model_name, partial_payload, warnings)

        # Phase 2: balanced excerpts preserve block IDs and both ends of each section.
        stage = "full_text_synthesis"
        excerpt_parts = []
        per_section_budget = max(1, MAX_INPUT_CHARS // max(1, len(sections)))
        truncated_sections = []
        for section in sections:
            rendered = [
                {"header": f"<block id={block['id']} section={section['title']}>",
                 "text": block["text"]}
                for block in section["blocks"]
            ]
            full_section = "\n".join(
                item["header"] + item["text"] + "</block>" for item in rendered
            )
            budget = max(1, per_section_budget - 80)
            if len(full_section) <= budget:
                excerpt_parts.append(full_section)
                continue

            truncated_sections.append(section["title"])
            head_budget = max(1, int(budget * 0.55))
            tail_budget = max(1, budget - head_budget)
            head_parts, remaining = [], head_budget
            for item in rendered:
                if remaining <= 0:
                    break
                header = item["header"]
                room = max(0, remaining - len(header) - len("</block>"))
                piece = item["text"][:room]
                if room > 0:
                    head_parts.append(header + piece + "</block>")
                    remaining -= len(header) + len(piece) + len("</block>")

            tail_parts, remaining = [], tail_budget
            for item in reversed(rendered):
                if remaining <= 0:
                    break
                header = item["header"]
                room = max(0, remaining - len(header) - len("</block>"))
                piece = item["text"][-room:] if room else ""
                if room > 0:
                    tail_parts.insert(0, header + piece + "</block>")
                    remaining -= len(header) + len(piece) + len("</block>")
            excerpt_parts.append("\n".join(head_parts + tail_parts))

        source = "\n".join(excerpt_parts)
        if truncated_sections:
            warnings.append(
                "全文综合输入预算不足，已按章节均衡保留各章开头与结尾；"
                "以下章节仍有正文未进入综合输入：" + "、".join(truncated_sections)
            )

        partial_payload["section_map"] = section_notes
        partial_payload["progress_stage"] = stage
        _persist_progress(draft_id, model_name, partial_payload, warnings)
        synthesis_prompt = (
            f"论文标题：{title}\n章节角色与摘要：{json.dumps(section_notes, ensure_ascii=False)}\n"
            f"可核验原文块（可能因长度上限未覆盖全文；不可引用未提供内容）：\n{source}\n"
            "请生成全文理解档案。只可引用上述原文块的 block_id 和逐字 quote。"
        )
        raw_payload = await chat(
            info,
            [{"role": "system", "content": UNDERSTANDING_SYSTEM},
             {"role": "user", "content": synthesis_prompt}],
            max_tokens=6500,
        )
        payload = _json(raw_payload)
        if not payload:
            raise RuntimeError("全文综合阶段未返回合法 JSON（响应为空、被截断或格式不符合 JSON）")

        for key in ("contributions", "theoretical_framework", "method_architecture",
                    "key_findings", "limitations", "uncertainties"):
            payload.setdefault(key, [])
        payload["version"] = VERSION
        payload["paper_title"] = title
        payload["section_map"] = section_notes
        payload["relations"] = []
        payload["progress_stage"] = "evidence_validation"
        partial_payload = payload
        _persist_progress(draft_id, model_name, payload, warnings)

        block_map = _evidence_index(sections)
        warnings.extend(_validate_evidence(payload, block_map))
        warnings.extend(_normalise_contributions(payload))
        if payload.get("paper_type") not in {
            "theory", "algorithm", "system", "empirical", "survey", "other"
        }:
            payload["paper_type"] = "other"
        _persist_progress(draft_id, model_name, payload, warnings)

        nodes = payload["contributions"]
        if nodes:
            stage = "relation_candidate_generation"
            payload["progress_stage"] = stage
            partial_payload = payload
            _persist_progress(draft_id, model_name, payload, warnings)
            relation_prompt = json.dumps(
                {
                    "nodes": [
                        {"id": node.get("id"), "title": node.get("title"),
                         "description": node.get("description"), "kind": node.get("kind"),
                         "importance": node.get("importance")}
                        for node in nodes
                    ],
                    "evidence_blocks": [
                        {"block_id": block_id, "text": text}
                        for block_id, text in block_map.items()
                    ],
                },
                ensure_ascii=False,
            )[:MAX_INPUT_CHARS]
            relation_result = _json(await chat(
                info,
                [
                    {"role": "system", "content":
                     "根据贡献树节点生成极少量、方向明确的学术关系候选。只输出 JSON："
                     "{\"relations\":[{\"source_id\":\"c1\",\"target_id\":\"c2\","
                     "\"relation_type\":\"CONSISTS_OF|SUPPORTS|PROVES|DERIVES|NECESSARY_FOR|"
                     "SUFFICIENT_FOR|IMPLIES\",\"evidence\":{\"block_id\":123,"
                     "\"quote\":\"原文逐字摘录\"}}]}。只保留原文明确表达的关系，宁可空数组。"},
                    {"role": "user", "content": relation_prompt},
                ],
                max_tokens=2400,
            ))
            if relation_result:
                payload["relations"] = relation_result.get("relations", [])[:12]

        warnings.extend(_normalise_relations(payload, block_map))

        stage = "relation_verification"
        payload["progress_stage"] = stage
        partial_payload = payload
        _persist_progress(draft_id, model_name, payload, warnings)
        for relation in payload["relations"]:
            source_node = nodes_by_id(payload).get(relation["source_id"])
            target_node = nodes_by_id(payload).get(relation["target_id"])
            verify_prompt = json.dumps(
                {"source": source_node, "relation": relation["relation_type"],
                 "target": target_node, "quote": relation["evidence"]["quote"]},
                ensure_ascii=False,
            )
            verdict = _json(await chat(
                info,
                [{"role": "system", "content": RELATION_SYSTEM},
                 {"role": "user", "content": verify_prompt}],
                max_tokens=600,
            ))
            if verdict and verdict.get("verdict") in ("entailed", "not_entailed", "uncertain"):
                relation["verification"] = verdict["verdict"]
                relation["reason"] = str(verdict.get("reason") or "")[:500]
            else:
                relation["verification"] = "uncertain"
                relation["reason"] = "语义验证器未能给出有效结论"
            _persist_progress(draft_id, model_name, payload, warnings)

        payload.pop("progress_stage", None)
        _persist_progress(draft_id, model_name, payload, warnings, "draft")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "V3 paper understanding failed: draft_id=%s paper_id=%s stage=%s",
            draft_id, pid, stage,
        )
        warnings.append(
            f"V3理解流程失败，阶段={stage}（{type(exc).__name__}）：{str(exc)[:300]}"
        )
        partial_payload["section_map"] = section_notes
        partial_payload["progress_stage"] = stage
        _persist_progress(draft_id, model_name, partial_payload, warnings, "failed")

def nodes_by_id(payload: dict) -> dict:
    return {str(n.get("id")):n for n in payload.get("contributions",[]) if n.get("id")}

def create_draft(pid: str) -> dict:
    ensure_tables()
    paper = row("SELECT id,status,library_role FROM papers WHERE id=?",(pid,))
    if not paper: raise ValueError("论文不存在")
    if paper.get("library_role","normal") != "normal": raise ValueError("测试/排除论文不能生成理解档案")
    if paper["status"] in ("pending_parse","parsing"): raise ValueError("论文仍在解析中")
    if not rows("SELECT id FROM blocks WHERE paper_id=? LIMIT 1",(pid,)): raise ValueError("论文没有已解析正文")
    active = row("SELECT id FROM kg_understanding_drafts WHERE paper_id=? AND status='running' ORDER BY created_at DESC LIMIT 1",(pid,))
    if active: raise ValueError("该论文已有理解任务正在运行")
    jid = new_id()
    with db() as c:
        c.execute("INSERT INTO kg_understanding_drafts(id,paper_id,version,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (jid,pid,VERSION,"running",now(),now()))
    return row("SELECT id,paper_id,version,status,created_at FROM kg_understanding_drafts WHERE id=?",(jid,))

def get_draft(draft_id: str) -> dict | None:
    ensure_tables()
    d = row("SELECT * FROM kg_understanding_drafts WHERE id=?",(draft_id,))
    if not d: return None
    d["payload"] = loads(d.pop("payload_json"),{})
    d["warnings"] = loads(d.pop("warnings_json"),[])
    return d

def list_drafts(pid: str | None = None) -> list[dict]:
    ensure_tables()
    sql = "SELECT id,paper_id,version,status,model_name,created_at,updated_at FROM kg_understanding_drafts"
    args = ()
    if pid: sql += " WHERE paper_id=?"; args=(pid,)
    sql += " ORDER BY created_at DESC LIMIT 100"
    return rows(sql,args)

def update_review(draft_id: str, payload: dict, status: str | None = None) -> dict:
    ensure_tables()
    if status not in (None,"approved","rejected"): raise ValueError("状态只能是 approved 或 rejected")
    current = row("SELECT status FROM kg_understanding_drafts WHERE id=?",(draft_id,))
    if not current: raise ValueError("理解档案不存在")
    if current["status"] in ("running","failed"): raise ValueError("任务尚未成功完成，不能审核")
    encoded = dumps(payload)
    with db() as c:
        if status:
            c.execute("UPDATE kg_understanding_drafts SET payload_json=?,status=?,updated_at=? WHERE id=?",
                      (encoded,status,now(),draft_id))
        else:
            c.execute("UPDATE kg_understanding_drafts SET payload_json=?,updated_at=? WHERE id=?",
                      (encoded,now(),draft_id))
    return get_draft(draft_id)
