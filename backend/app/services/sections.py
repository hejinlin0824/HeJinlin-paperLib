"""章节（section）服务：切分、章解读生成、思考问题、回答 AI 点评。

- 切分：解析完成后按 heading（lv<=2 / Abstract / References）把 blocks 归章，纯本地无 AI；
- 章解读：一次调用生成 通俗理解 summary + 本章要点 takeaway + 思考问题 questions；
- 问题围绕【导读核心问题】×【本章内容】提出；读者作答后可提交，AI 点评并补充参考答案。
"""
import asyncio
import json
import re

from ..db import ex, row, rows
from ..util import dumps, loads, now
from .translate import _clean, chat, engine_info, engine_ready

HEADING_SPLIT_LEVEL = 1
SECTION_CONTENT_MAX = 9000

MAIN_NUM = re.compile(r"^\d{1,2}[\s.]+\S")       # 主编号章："1 Introduction"
SUB_NUM = re.compile(r"^\d{1,2}(?:\.\d+)+[\s.]*\S")  # 子编号小节："2.1.1 xxx"（不开新章）

SECTION_SYSTEM = """你是论文精读陪练。读者按章节顺序精读论文，你为【本章】生成本章解读与思考题。你掌握的信息：论文标题、导读核心问题、读者已读章节、读者此前的思考摘录、本章英文内容、后续章节的标题（仅标题，严禁据此提问）。

只输出 JSON（不要代码块标记、不要解释）：
{"summary": "本章通俗总结，结构化输出：第一行=一句话主线；随后 3~6 条编号要点（大白话拆解本章，可用比喻，关键处 **加粗**、最关键处 ==高亮==）；若本章有多个并列概念/方法/系统，插入一个 markdown 表格对比（用 | 分隔的表格行）；最后一行以 💡 开头给'一句话重点'。总长 250~450 字",
 "takeaway": "本章核心要点，3~5 条，每条一句话（可含 **加粗**），用 \\n 分隔",
 "questions": [{"kind": "导读|应用|痛点|创新|方案|对比|批判|迁移|展望", "q": "思考问题"}]}

【若本章性质=开篇导读章】读者还没进入正文，问题全部是"引人入胜"型、kind=导读：
- 勾起好奇（如"作者为什么不直接举例定义，非要造一套充要条件？"）、征集读者基于摘要的直觉与经验（"你直觉上觉得 X 和 Y 的差别在哪？"）、联系读者自己的研究/工程经历；
- 不设应用判断题/方案题；每题读者凭读完摘要的直觉就能说上两句，没有标准答案压力。

【正文章】questions 出 3~4 条，铁律（因果系统）：
1. 立足本章：每道题的核心必须是本章讲的具体内容。读者读完本章就必须能作答；需要未读章节知识才能回答的问题都是事故；
2. 出题形式多样化，优先顺序：
   a)【应用判断题·常用】构造一个具体系统/场景/反例，让读者用本章的标准、方法、分类做出自己的判断——像出一道有趣的判断题，不是考卷（例：'某脚本每轮需要人工点击才继续，你觉得它满足本章的 X 条件吗？'）；
   b)【对比辨析题】本章内部或本章与已读章节的 A vs B 关键差异；
   c)【机制题】为什么这样设计、去掉某条件会怎样；
   d)【迁移题】结合读者自己的研究/工程怎么用；
3. 衔接已读：可要求读者联系已读章节的概念，或回应【读者此前的思考】里读者自己的回答，但勿重复已问过的问题；
4. 后文展望至多 1 题放最后、kind=展望、题面注明"（可选·带着往下读）"；
5. 禁止考察记忆力、禁止抠字眼、禁止要求全文层面的整体理解。

所有思考题的语气铁律：
- 问题是"邀请思考"，不是"考试"：读者读完就该想说两句，而且敢说——答错了 AI 会补，没有任何惩罚；
- 一道题只问一件事（至多两个小问）；措辞口语化（"你觉得…""直觉上…""如果是你会怎么做"），不堆"请给出/请说明/请评估"这类考卷用语；
- 一题的文字量控制在 2~3 行以内。"""

REVIEW_SYSTEM = """你是论文精读陪练。读者刚读完【本章】（后续章节完全未读，除非标注本章是最后一章），针对本章一道思考题提交了回答。

评判基准只有【本章内容】与读者已读章节。严禁站在全文或后文的高度评判局部回答；读者答了超纲内容时，回复"这一点后文会给出答案，你现在的理解到这里已经成立"。

点评风格（150~350 字，中文，判定先行，像学长批作业）：
第一行：即时判定——理解到位用 ✅（如"✅ 完全正确""✅ 抓准核心""✅ 结论正确"）；有偏差用 💡（如"💡 方向对但需要修正""💡 部分正确"），并用半句话点明对在哪/偏在哪；
随后：纠偏或补充——引用【本章内容】的原文事实精确纠正，==高亮== 最容易记错的点；逐条分析类题目（如对照多个条件判定）要逐条给出 ✅/❌ 与一句依据；
最后：补充一层原文视角或延伸（可选）。
读者的回答哪怕很简短、很直觉，只要方向对就大方给 ✅ 并补充；不重复读者已说对的大段内容，不说客套话。"""


def _engine():
    info = engine_info()
    ok, err = engine_ready(info)
    if not ok:
        raise RuntimeError(f"引擎未就绪：{err}（请到设置页配置）")
    return info


# ---------- 章节切分（纯本地） ----------

NUM_SPLIT = re.compile(r"^(\d+(?:\.\d+)*)[\s.]+[^\d\s]")


def _clean_title(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def rebuild_sections(pid: str):
    """按 heading 把 blocks 归章；重复解析时按英文标题复用已有解读与问答。

    开新章的判断：编号标题（"1 Introduction" / "2.3 xxx"，中英文都认）或
    Abstract / References 特判或解析器标注的高级别标题（lv<=2）。
    不依赖 lv 单独判断——arXiv 等模板的章节标题常被标为 lv3。
    """
    blocks = rows("SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    old = {(_clean_title(s["title_en"]) or _clean_title(s["title"])): s
           for s in rows("SELECT * FROM sections WHERE paper_id=?", (pid,))}
    secs, cur = [], None
    for b in blocks:
        t = b["type"]
        en = (b["en"] or "").strip()
        zh = (b["zh"] or "").strip()
        if t == "heading":
            meta = loads(b["meta"], {})
            lv = meta.get("level", 2) or 2
            is_abstract = bool(re.match(r"^(abstract|摘\s*要)\b", en, re.I)) or \
                bool(re.match(r"^(abstract|摘\s*要)", zh, re.I))
            is_refs = bool(re.match(r"^(references?\b|bibliography|参考文献)", en, re.I)) or \
                bool(re.match(r"^参考文献", zh))
            # 只主编号开新章；子编号小节（1.1 / 2.1.1）留在当前章内；公式伪标题不开章
            mathish = len(re.findall(r"[\U0001D400-\U0001D7FF]", en)) >= 2
            numbered = not mathish and (
                (bool(MAIN_NUM.match(en)) and not SUB_NUM.match(en)) or
                (bool(MAIN_NUM.match(zh)) and not SUB_NUM.match(zh)))
            if (cur is None or lv <= HEADING_SPLIT_LEVEL or is_abstract or is_refs
                    or numbered):
                cur = {"title": _clean_title(zh) or _clean_title(en),
                       "title_en": _clean_title(en), "block_start": b["id"],
                       "order_idx": len(secs), "skip": is_refs, "en_text": en}
                secs.append(cur)
                continue
        if cur is None:
            # 首个 heading 之前的内容（通常是摘要）归入"开篇"章
            if t in ("text", "list", "table", "formula") and en:
                cur = {"title": "摘要 · 开篇", "title_en": "Abstract",
                       "block_start": b["id"], "order_idx": len(secs),
                       "skip": False, "en_text": ""}
                secs.append(cur)
            else:
                continue
        if t == "references":
            if not cur["skip"]:  # 无 References 标题的论文
                cur = {"title": _clean_title(zh) or "参考文献", "title_en": "References",
                       "block_start": b["id"], "order_idx": len(secs),
                       "skip": True, "en_text": ""}
                secs.append(cur)
            continue
        if t == "image":
            continue
        if t == "formula":
            meta = loads(b["meta"], {})
            cur["en_text"] += "\n" + (meta.get("latex") or en or "")
            continue
        cur["en_text"] += "\n" + en
    ex("DELETE FROM sections WHERE paper_id=?", (pid,))
    for s in secs:
        o = old.get(_clean_title(s["title_en"]) or _clean_title(s["title"])) or {}
        ex("INSERT INTO sections(paper_id, title, title_en, block_start, order_idx, skip, "
           "en_text, summary, takeaway, questions, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (pid, s["title"][:300], s["title_en"][:300], s["block_start"], s["order_idx"],
            1 if s["skip"] else 0, s["en_text"][:60000],
            o.get("summary") or "", o.get("takeaway") or "",
            o.get("questions") or "[]", now()))


def _sec_out(s: dict) -> dict:
    return {k: s[k] for k in ("id", "paper_id", "title", "title_en", "block_start",
                              "order_idx", "skip", "summary", "takeaway", "updated_at")} | \
        {"questions": loads(s["questions"], [])}


def list_sections(pid: str):
    return [_sec_out(s) for s in
            rows("SELECT * FROM sections WHERE paper_id=? ORDER BY order_idx", (pid,))
            if (s["en_text"] or "").strip() or s["skip"] or s["summary"]]


# ---------- 章解读生成 ----------

async def generate_section(sid: int) -> dict:
    info = _engine()
    s = row("SELECT * FROM sections WHERE id=?", (sid,))
    if not s:
        raise RuntimeError("章节不存在")
    if s["skip"]:
        raise RuntimeError("参考文献章节无需解读")
    pid = s["paper_id"]
    p = row("SELECT title, title_zh, guide FROM papers WHERE id=?", (pid,)) or {}
    title = p.get("title_zh") or p.get("title") or ""
    # 章节序列：读者已读（本章之前）/ 未读（本章之后），用于设计循序渐进的问题
    all_secs = rows("SELECT id, title, title_en, order_idx FROM sections "
                    "WHERE paper_id=? AND skip=0 ORDER BY order_idx", (pid,))
    idx = next((i for i, x in enumerate(all_secs) if x["id"] == sid), -1)
    prev = "、".join(x["title"] or x["title_en"] for x in all_secs[:idx]) or "（本章是第一篇，读者刚读完摘要/开篇）"
    nxt = "、".join(x["title"] or x["title_en"] for x in all_secs[idx + 1:]) or "（本章是最后一章）"
    guide_qs = ""
    try:
        g = json.loads(p.get("guide") or "null")
        guide_qs = "\n".join(f"- {q}" for q in (g or {}).get("questions", []))
    except Exception:  # noqa: BLE001
        pass
    # 因果系统：读者此前章节的真实回答摘录（供问题衔接，不重复提问）
    prev_thinking = []
    for x in all_secs[:idx]:
        xs = row("SELECT questions FROM sections WHERE id=?", (x["id"],))
        for q in loads((xs or {}).get("questions", "[]"), []):
            if (q.get("my") or "").strip():
                prev_thinking.append(
                    f"[{x['title'] or x['title_en']}] 问：{q['q'][:60]} 答：{q['my'][:120]}")
    prev_ans_text = "\n".join(prev_thinking[-6:]) or "（读者还没写过回答）"
    is_opening = idx <= 0 or bool(re.match(r"^(abstract|摘\s*要)",
                                           s["title_en"] or s["title"], re.I))
    nature = ("开篇导读章（读者尚未进入正文：问题全部为引人入胜型，kind=导读）" if is_opening
              else f"正文第 {idx} 章" + ("（也是最后一章，点评可做全文收束）"
                                        if idx == len(all_secs) - 1 else ""))
    msgs = [
        {"role": "system", "content": SECTION_SYSTEM},
        {"role": "user", "content":
            f"【论文】{title}\n\n【导读核心问题】\n{guide_qs or '（尚未生成导读）'}\n\n"
            f"【读者已读章节】{prev}\n\n"
            f"【读者此前的思考摘录（可自然衔接，勿重复提问）】\n{prev_ans_text}\n\n"
            f"【本章标题】{s['title_en'] or s['title']}\n\n"
            f"【本章性质】{nature}\n\n"
            f"【后续章节标题（读者未读，严禁据此提问）】{nxt}\n\n"
            f"【本章内容】\n{(s['en_text'] or '')[:SECTION_CONTENT_MAX]}"},
    ]
    raw = await chat(info, msgs)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise RuntimeError("模型未返回 JSON：" + raw[:120])
    data = json.loads(m.group(0))
    questions = []
    for i, q in enumerate((data.get("questions") or [])[:5]):
        qtext = str(q.get("q") if isinstance(q, dict) else q or "").strip()
        if qtext:
            questions.append({"id": i + 1, "kind": str(q.get("kind") or "")[:6],
                              "q": qtext[:300], "my": "", "review": ""})
    ex("UPDATE sections SET summary=?, takeaway=?, questions=?, updated_at=? WHERE id=?",
       (str(data.get("summary") or "")[:3000], str(data.get("takeaway") or "")[:2000],
        dumps(questions), now(), sid))
    return _sec_out(row("SELECT * FROM sections WHERE id=?", (sid,)))


async def explain_all_job(pid: str):
    """全部章节解读后台任务（进度 papers.explain_done/total）。"""
    try:
        _engine()
    except RuntimeError as e:
        ex("UPDATE papers SET explain_total=0, explain_done=0 WHERE id=?", (pid,))
        print("[sections]", e)
        return
    secs = rows("SELECT * FROM sections WHERE paper_id=? AND skip=0 ORDER BY order_idx", (pid,))
    todo = [s for s in secs
            if not (s["summary"] or "").strip() and (s["en_text"] or "").strip()]
    if not todo:
        return
    ex("UPDATE papers SET explain_total=?, explain_done=0 WHERE id=?", (len(todo), pid))
    sem = asyncio.Semaphore(2)
    state = {"done": 0}

    async def one(s):
        async with sem:
            try:
                await generate_section(s["id"])
            except Exception as e:  # noqa: BLE001
                print(f"[sections] {s['title']} failed:", str(e)[:150])
        state["done"] += 1
        ex("UPDATE papers SET explain_done=? WHERE id=?", (state["done"], pid))

    await asyncio.gather(*[one(s) for s in todo])
    ex("UPDATE papers SET explain_done=?, explain_total=? WHERE id=?",
       (state["done"], state["done"], pid))


# ---------- 回答点评 ----------

async def review_answer(pid: str, sid: int, qid: int, answer: str) -> dict:
    info = _engine()
    s = row("SELECT * FROM sections WHERE id=?", (sid,))
    if not s or s["paper_id"] != pid:
        raise RuntimeError("章节不存在")
    qs = loads(s["questions"], [])
    q = next((x for x in qs if x.get("id") == qid), None)
    if not q:
        raise RuntimeError("问题不存在")
    q["my"] = (answer or "").strip()[:3000]
    # 后续章节标题（供点评结尾给出阅读指引）
    all_secs = rows("SELECT id, title, title_en FROM sections "
                    "WHERE paper_id=? AND skip=0 ORDER BY order_idx", (pid,))
    idx = next((i for i, x in enumerate(all_secs) if x["id"] == sid), -1)
    nxt = "、".join(x["title"] or x["title_en"] for x in all_secs[idx + 1:]) or "（无）"
    content = (s["summary"] or "") + "\n" + (s["en_text"] or "")[:2500]
    msgs = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content":
            f"【论文】{p_title(pid)}\n【本章】{s['title_en'] or s['title']}\n"
            f"【后续章节（读者未读，点评末尾可引用其一作阅读指引）】{nxt}\n\n"
            f"【思考问题】({q.get('kind') or ''}) {q['q']}\n\n"
            f"【本章相关内容】\n{content}\n\n【我的回答】\n{q['my']}"},
    ]
    q["review"] = _clean(await chat(info, msgs))[:3000]
    ex("UPDATE sections SET questions=?, updated_at=? WHERE id=?", (dumps(qs), now(), sid))
    return _sec_out(row("SELECT * FROM sections WHERE id=?", (sid,)))


def p_title(pid: str) -> str:
    p = row("SELECT title_zh, title FROM papers WHERE id=?", (pid,)) or {}
    return p.get("title_zh") or p.get("title") or ""
