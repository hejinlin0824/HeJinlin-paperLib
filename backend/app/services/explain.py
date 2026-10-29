"""整篇导读生成（为什么读 / 带着什么问题 / 结构速览），存 papers.guide。

逐段解读与章节解读分属 services/sections.py。
"""
import json
import re

from ..db import ex, row, rows
from ..util import dumps, now
from .translate import chat, engine_info, engine_ready

GUIDE_SYSTEM = """你是学术论文导读助手。基于给出的论文标题、摘要开头与目录，生成一份中文导读，让读者带着问题和"地图"进入精读。只输出 JSON（不要代码块标记、不要任何解释）：
{"why_read": "为什么应该读这篇论文：150~300 字。先戳行业/研究中的真实痛点，再点出这篇论文独特做了什么、读完能获得什么。语气像一个懂行的学长在安利，可以有煽动性。关键词用 **加粗**，最关键的一句用 ==高亮==",
 "questions": ["读者应该带着的核心疑问，3~6 条，必须具体到这篇论文的内容，不要泛泛而谈"],
 "structure": [{"title": "章节标题（保留原文编号）", "summary": "这一节做什么、核心结论是什么，30~70 字"}]}
structure 必须覆盖目录中每个有实质内容的章节且顺序一致。"""


def _engine():
    info = engine_info()
    ok, err = engine_ready(info)
    if not ok:
        raise RuntimeError(f"引擎未就绪：{err}（请到设置页配置）")
    return info


async def generate_guide(pid: str) -> dict:
    info = _engine()
    paper = row("SELECT title, title_zh FROM papers WHERE id=?", (pid,))
    if not paper:
        raise RuntimeError("论文不存在")
    headings = rows("SELECT en, zh FROM blocks WHERE paper_id=? AND type='heading' "
                    "ORDER BY order_idx", (pid,))
    opening = rows("SELECT en FROM blocks WHERE paper_id=? AND type IN ('text','heading') "
                   "AND en!='' ORDER BY order_idx LIMIT 10", (pid,))
    outline = "\n".join(f"- {h['zh'] or h['en']}" for h in headings) or "（未识别出目录）"
    opening_text = "\n".join(t["en"] for t in opening)[:3500]
    msgs = [
        {"role": "system", "content": GUIDE_SYSTEM},
        {"role": "user", "content":
            f"【标题】{paper['title_zh'] or paper['title']}（{paper['title']}）\n\n"
            f"【摘要与开头】\n{opening_text}\n\n【目录】\n{outline}"},
    ]
    raw = await chat(info, msgs)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise RuntimeError("模型未返回 JSON：" + raw[:120])
    data = json.loads(m.group(0))
    data = {
        "why_read": str(data.get("why_read") or "")[:2500],
        "questions": [str(q)[:300] for q in (data.get("questions") or []) if q][:8],
        "structure": [{"title": str(s.get("title") or "")[:200],
                       "summary": str(s.get("summary") or "")[:500]}
                      for s in (data.get("structure") or []) if isinstance(s, dict)][:40],
    }
    ex("UPDATE papers SET guide=?, updated_at=? WHERE id=?", (dumps(data), now(), pid))
    return data
