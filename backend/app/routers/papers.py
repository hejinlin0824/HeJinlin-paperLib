"""论文管理：上传、解析调度、翻译调度、块读写、导出。"""
import asyncio
import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, File, HTTPException, UploadFile
from fastapi.responses import Response

from ..config import PAPERS_DIR
from ..db import db, ex, get_settings, row, rows
from ..services import parser_builtin, parser_mineru
from ..services import explain as explain_svc
from ..services import sections as sec_svc
from ..services import translate as tsvc
from ..util import dumps, loads, new_id, now

router = APIRouter(prefix="/api/papers", tags=["papers"])

META_FIELDS = ["title", "title_zh", "authors", "year", "venue", "doi", "tags", "rating"]


def _paper_dir(pid: str) -> Path:
    return PAPERS_DIR / pid


async def process_paper(pid: str, engine: str | None = None, old_zh: dict | None = None):
    """后台：解析 → 存块（复用旧译文）→ 补元数据 →（按设置）自动翻译。"""
    pdir = _paper_dir(pid)
    pdf = pdir / "original.pdf"
    engine = engine or get_settings().get("parse_engine", "builtin")
    try:
        ex("UPDATE papers SET status='parsing', parse_engine=?, error='', updated_at=? WHERE id=?",
           (engine, now(), pid))
        if engine == "mineru_api":
            token = get_settings().get("mineru_token", "")
            result = await parser_mineru.parse_cloud(pdf, pdir, token)
        elif engine == "mineru_local":
            result = await parser_mineru.parse_local(pdf, pdir)
        else:
            result = await asyncio.to_thread(parser_builtin.parse, str(pdf), pdir)
        blocks, meta = result.get("blocks", []), result.get("meta", {})
        if not blocks:
            raise RuntimeError("解析结果为空（可能是扫描版 PDF，请改用 MinerU 引擎并开启 OCR）")
        # 头部结构化 + 噪声过滤（两引擎统一）：标题删除、作者/单位聚合为作者卡、
        # 版权/DOI/卷期/CC 许可等模板噪声剔除（MinerU 路径此前没有这层过滤）
        blocks, authors_en = parser_builtin.extract_front_matter(blocks)
        blocks = [b for b in blocks
                  if b.get("type") != "text" or not parser_builtin._noiseish(b.get("en", ""))]
        for i, b in enumerate(blocks):
            b["order_idx"] = i
        # 去掉与论文标题重复的首个标题块（阅读器已单独渲染标题）
        def _norm(s):
            return "".join(ch for ch in str(s).lower() if ch.isalnum())
        if (blocks[0].get("type") == "heading" and meta.get("title")
                and (_norm(blocks[0].get("en", "")) == _norm(meta["title"])
                     or _norm(blocks[0].get("en", "")) in _norm(meta["title"])
                     or _norm(meta["title"]) in _norm(blocks[0].get("en", "")))):
            blocks = blocks[1:]
            for i, b in enumerate(blocks):
                b["order_idx"] = i
        # 重新解析时按原文复用已有译文（省时省钱）
        old_zh = old_zh or {b["en"]: b["zh"] for b in
                            rows("SELECT en, zh FROM blocks WHERE paper_id=? AND IFNULL(zh,'')!=''", (pid,))
                            if b["en"]}
        with db() as c:
            c.execute("DELETE FROM blocks WHERE paper_id=?", (pid,))
            c.executemany(
                "INSERT INTO blocks(paper_id, order_idx, type, en, zh, meta, translate_status) "
                "VALUES(?,?,?,?,?,?,?)",
                [(pid, b["order_idx"], b["type"], b.get("en", ""),
                  old_zh.get(b.get("en", ""), b.get("zh", "")),
                  dumps(b.get("meta", {})),
                  "done" if b.get("en", "") in old_zh else "pending") for b in blocks])
        try:
            sec_svc.rebuild_sections(pid)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        p = row("SELECT title, year, title_locked, authors FROM papers WHERE id=?", (pid,))
        upd, args = ["status='parsed'", "updated_at=?"], [now()]
        if meta.get("title") and (not p or not p["title_locked"]):
            upd.append("title=?")
            args.append(meta["title"])
        if meta.get("year") and (not p or not p["year"]):
            upd.append("year=?")
            args.append(str(meta["year"]))
        if authors_en and (not p or not p["authors"]):
            upd.append("authors=?")
            args.append(authors_en.replace("\n", "；")[:500])
        args.append(pid)
        ex(f"UPDATE papers SET {', '.join(upd)} WHERE id=?", tuple(args))
    except Exception as e:  # noqa: BLE001
        ex("UPDATE papers SET status='failed', error=?, updated_at=? WHERE id=?",
           (f"解析失败：{str(e)[:400]}", now(), pid))
        return
    try:
        await tsvc.extract_metadata(pid)
    except Exception:  # noqa: BLE001
        pass
    if get_settings().get("auto_translate", "1") == "1":
        await tsvc.translate_paper_job(pid, force=False)


@router.post("/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    pid = new_id()
    pdir = _paper_dir(pid)
    (pdir / "assets").mkdir(parents=True, exist_ok=True)
    (pdir / "original.pdf").write_bytes(await file.read())
    title = Path(file.filename).stem[:300]
    ex("INSERT INTO papers(id, title, status, created_at, updated_at) VALUES(?,?,?,?,?)",
       (pid, title, "pending_parse", now(), now()))
    background.add_task(process_paper, pid)
    return {"id": pid, "title": title}


LIST_SQL = """
SELECT p.id, p.title, p.title_zh, p.authors, p.year, p.venue, p.tags, p.rating,
       p.status, p.parse_engine, p.translate_engine, p.translate_done, p.translate_total,
       p.guide, p.explain_done, p.explain_total,
       p.error, p.reading_progress, p.created_at, p.updated_at,
       COALESCE(l.status, '') AS log_status,
       (SELECT COUNT(*) FROM questions q WHERE q.paper_id = p.id AND q.status='open') AS open_questions,
       (SELECT COUNT(*) FROM questions q WHERE q.paper_id = p.id) AS total_questions
FROM papers p LEFT JOIN research_log l ON l.paper_id = p.id
"""


@router.get("")
def list_papers():
    return rows(LIST_SQL + " ORDER BY p.updated_at DESC")


@router.get("/dashboard")
def dashboard():
    stats = {
        "total": row("SELECT COUNT(*) n FROM papers")["n"],
        "done": row("SELECT COUNT(*) n FROM research_log WHERE status='读完'")["n"],
        "annotations": row("SELECT COUNT(*) n FROM annotations")["n"],
        "open_questions": row("SELECT COUNT(*) n FROM questions WHERE status='open'")["n"],
    }
    recent = rows(LIST_SQL + " ORDER BY p.updated_at DESC LIMIT 6")
    open_q = rows(
        "SELECT q.id, q.paper_id, q.content, q.created_at, p.title_zh, p.title "
        "FROM questions q LEFT JOIN papers p ON p.id=q.paper_id "
        "WHERE q.status='open' ORDER BY q.created_at DESC LIMIT 8")
    return {"stats": stats, "recent": recent, "open_questions": open_q}


@router.get("/{pid}")
def get_paper(pid: str):
    p = row(LIST_SQL + " WHERE p.id=?", (pid,))
    if not p:
        raise HTTPException(404, "论文不存在")
    return p


@router.patch("/{pid}")
def patch_paper(pid: str, body: dict = Body(...)):
    fields = {k: body[k] for k in META_FIELDS if k in body}
    if not fields:
        raise HTTPException(400, "无可更新字段")
    sets = ", ".join(f"{k}=?" for k in fields)
    extra = ", title_locked=1" if "title" in fields else ""
    ex(f"UPDATE papers SET {sets}{extra}, updated_at=? WHERE id=?",
       (*fields.values(), now(), pid))
    return row("SELECT * FROM papers WHERE id=?", (pid,))


@router.delete("/{pid}")
def delete_paper(pid: str):
    with db() as c:
        for t in ("blocks", "annotations", "questions", "research_log"):
            c.execute(f"DELETE FROM {t} WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM papers WHERE id=?", (pid,))
    shutil.rmtree(_paper_dir(pid), ignore_errors=True)
    return {"ok": True}


@router.post("/{pid}/reparse")
async def reparse(pid: str, background: BackgroundTasks, body: dict = Body(default={})):
    if not row("SELECT id FROM papers WHERE id=?", (pid,)):
        raise HTTPException(404, "论文不存在")
    engine = body.get("engine")
    # 先读出旧译文供复用，再清块（顺序不能反）
    old_zh = {b["en"]: b["zh"] for b in
              rows("SELECT en, zh FROM blocks WHERE paper_id=? AND IFNULL(zh,'')!=''", (pid,))
              if b["en"]}
    ex("DELETE FROM blocks WHERE paper_id=?", (pid,))
    ex("UPDATE papers SET status='pending_parse', translate_done=0, translate_total=0, "
       "title_zh='', updated_at=? WHERE id=?", (now(), pid))
    background.add_task(process_paper, pid, engine, old_zh)
    return {"ok": True}


@router.post("/{pid}/translate")
async def translate_paper(pid: str, background: BackgroundTasks,
                          body: dict = Body(default={})):
    p = row("SELECT status FROM papers WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "论文不存在")
    if p["status"] == "translating":
        raise HTTPException(409, "正在翻译中，请稍候")
    ex("UPDATE papers SET status='queued', updated_at=? WHERE id=?", (now(), pid))
    background.add_task(tsvc.translate_paper_job, pid, bool(body.get("force")))
    return {"ok": True}


@router.get("/{pid}/blocks")
def get_blocks(pid: str):
    bs = rows("SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    for b in bs:
        b["meta"] = loads(b["meta"])
    return bs


@router.post("/{pid}/blocks/{bid}/retranslate")
async def retranslate_block(pid: str, bid: int):
    try:
        return await tsvc.translate_block_sync(pid, bid)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/{pid}/guide")
async def make_guide(pid: str):
    """生成/重新生成整篇导读（同步，一次模型调用）。"""
    try:
        return await explain_svc.generate_guide(pid)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"导读生成失败：{str(e)[:200]}")


@router.get("/{pid}/sections")
def get_sections(pid: str):
    return sec_svc.list_sections(pid)


@router.post("/{pid}/sections/{sid}/generate")
async def generate_section(pid: str, sid: int):
    try:
        return await sec_svc.generate_section(sid)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"章解读生成失败：{str(e)[:200]}")


@router.patch("/{pid}/sections/{sid}/answer")
async def answer_section(pid: str, sid: int, body: dict = Body(...)):
    qid = body.get("qid")
    answer = body.get("answer", "")
    if not qid:
        raise HTTPException(400, "缺少 qid")
    try:
        return await sec_svc.review_answer(pid, sid, int(qid), answer)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"点评失败：{str(e)[:200]}")


@router.post("/{pid}/explain")
async def explain_paper(pid: str, background: BackgroundTasks):
    """全部章节解读（后台任务，进度走 papers.explain_done/total）。"""
    if not row("SELECT id FROM papers WHERE id=?", (pid,)):
        raise HTTPException(404, "论文不存在")
    background.add_task(sec_svc.explain_all_job, pid)
    return {"ok": True}


@router.patch("/{pid}/blocks/{bid}")
def edit_block_zh(pid: str, bid: int, body: dict = Body(...)):
    zh = body.get("zh")
    if zh is None:
        raise HTTPException(400, "缺少 zh 字段")
    ex("UPDATE blocks SET zh=?, translate_status='done' WHERE id=? AND paper_id=?",
       (zh, bid, pid))
    return row("SELECT * FROM blocks WHERE id=?", (bid,))


@router.put("/{pid}/progress")
def put_progress(pid: str, body: dict = Body(...)):
    ex("UPDATE papers SET reading_progress=? WHERE id=?",
       (dumps({"block_idx": body.get("block_idx", 0),
               "ratio": float(body.get("ratio", 0))}), pid))
    return {"ok": True}


@router.get("/{pid}/export.md")
def export_paper(pid: str):
    p = row("SELECT * FROM papers WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "论文不存在")
    bs = rows("SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    lines = [f"# {p['title_zh'] or p['title']}", ""]
    if p["title"] and p["title"] != (p["title_zh"] or ""):
        lines += [f"> Original: {p['title']}", ""]
    lines += [f"> 作者：{p['authors'] or '-'} ｜ 年份：{p['year'] or '-'} ｜ 来源：{p['venue'] or '-'}", ""]
    for b in bs:
        m = loads(b["meta"])
        t = b["type"]
        if t == "heading":
            lv = min(int(m.get("level", 2) or 2), 5) + 1
            zh, en = (b["zh"] or "").strip(), (b["en"] or "").strip()
            if zh:
                lines += ["#" * lv + " " + zh, ""]
            if en and en != zh:
                lines += ["> " + en, ""]
        elif t == "formula":
            latex = m.get("latex") or b["en"]
            lines += ["$$", latex, "$$", ""]
        elif t == "image":
            url = tsvc.image_data_url(pid, m.get("path") or "")
            src = url or f"assets/{m.get('path', '').split('/')[-1]}"
            lines += [f"![figure]({src})", ""]
            cap = (b["zh"] or b["en"] or "").strip()
            if cap:
                lines += [f"*{cap}*", ""]
        elif t == "table":
            cap = (b["zh"] or b["en"] or "").strip()
            if cap:
                lines += [f"**{cap}**", ""]
            if m.get("html"):
                lines += [m["html"], ""]
            elif b["en"]:
                lines += [b["en"], ""]
        elif t == "references":
            lines += ["## 参考文献", ""]
            lines += [f"{i+1}. {e}" for i, e in enumerate(m.get("entries", []))]
            lines += [""]
        else:
            if (b["zh"] or "").strip():
                lines += [b["zh"], ""]
            if (b["en"] or "").strip():
                lines += ["> " + b["en"].replace("\n", "\n> "), ""]
    content = "\n".join(lines)
    from urllib.parse import quote
    safe = (p["title_zh"] or p["title"] or pid).replace("\n", " ")[:50]
    return Response(content, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition":
            f"attachment; filename=\"paper-{pid}.md\"; filename*=UTF-8''{quote(safe)}"})


@router.get("/{pid}/export.notes.md")
def export_notes(pid: str):
    """整篇精读笔记：标题、导读、每章（译文/通俗解释/本章要点/思考问题+我的回答+AI点评）。"""
    p = row("SELECT * FROM papers WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "论文不存在")
    blocks = rows("SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    for b in blocks:
        b["meta"] = loads(b["meta"])
    secs = rows("SELECT * FROM sections WHERE paper_id=? ORDER BY order_idx", (pid,))
    guide = None
    try:
        guide = json.loads(p["guide"] or "null")
    except Exception:  # noqa: BLE001
        pass

    lines = [f"# {p['title_zh'] or p['title']}", ""]
    if p["title"] and p["title"] != p["title_zh"]:
        lines += [f"> {p['title']}", ""]
    lines += [f"> 作者：{p['authors'] or '-'} ｜ 年份：{p['year'] or '-'} ｜ "
              f"来源：{p['venue'] or '-'} ｜ 导出：{now()}", ""]

    if guide:
        lines += ["## 🧭 导读", "", "### 🎯 为什么应该读这篇论文", "",
                  guide.get("why_read") or "", ""]
        qs = guide.get("questions") or []
        if qs:
            lines += ["### ❓ 带着这些问题读", ""]
            lines += [f"{i}. {q}" for i, q in enumerate(qs, 1)] + [""]
        st = guide.get("structure") or []
        if st:
            lines += ["### 🗺️ 结构速览", ""]
            lines += [f"- **{x.get('title', '')}** — {x.get('summary', '')}" for x in st] + [""]

    # 章节与块的归属
    start_map = {s["block_start"]: s for s in secs}
    by_sec, cur = {}, None
    for b in blocks:
        if b["id"] in start_map:
            cur = start_map[b["id"]]["id"]
            by_sec.setdefault(cur, [])
        if cur is not None:
            by_sec[cur].append(b)

    for s in secs:
        title = s["title_en"] or s["title"]
        lines += [f"## {s['title'] or title}", ""]
        if s["skip"]:
            lines += ["（参考文献，笔记略）", ""]
            continue
        qs = loads(s["questions"], [])
        # 译文
        lines += ["### 📄 译文", ""]
        wrote = False
        for b in by_sec.get(s["id"], []):
            t = b["type"]
            zh = (b["zh"] or "").strip()
            en = (b["en"] or "").strip()
            if t == "heading":
                if zh or en:
                    lines += [f"**{zh or en}**", ""]
                    wrote = True
            elif t == "table":
                if zh:
                    lines += [zh, "", f"*（原文：{en[:100]}…）*" if len(en) > 100 else "", ""]
                    wrote = True
            elif t == "image":
                cap = zh or en
                if cap:
                    lines += [f"（图：{cap}）", ""]
                    wrote = True
            elif zh:
                lines += [zh, ""]
                wrote = True
        if not wrote:
            lines += ["（本章无译文内容）", ""]
        # 解读
        lines += ["### 💡 通俗解释", "", s["summary"] or "（未生成）", ""]
        if (s["takeaway"] or "").strip():
            lines += ["### 📌 本章要点", ""]
            lines += [f"- {x.strip()}" for x in re.split(r"[\n；]", s["takeaway"])
                      if x.strip()] + [""]
        lines += ["### 🤔 思考问题", ""]
        if not qs:
            lines += ["（未生成）", ""]
        for q in qs:
            kind = f"`{q.get('kind')}` " if q.get("kind") else ""
            lines += [f"**Q{q.get('id')}. {kind}{q.get('q')}**", ""]
            lines += [f"- **我的回答**：{q.get('my') or '（未作答）'}", ""]
            lines += [f"- **AI 点评**：{q.get('review') or '（未提交）'}", ""]
    content = "\n".join(lines)
    from urllib.parse import quote
    safe = (p["title_zh"] or p["title"] or pid).replace("\n", " ")[:50]
    return Response(content, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition":
            f"attachment; filename=\"notes-{pid}.md\"; filename*=UTF-8''{quote(safe)}"})
