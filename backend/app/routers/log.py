"""研究总表（reading log）与导出。"""
import csv
import io

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from ..db import ex, row, rows
from ..util import now

router = APIRouter(prefix="/api/log", tags=["log"])

FIELDS = ["research_problem", "solution", "innovation", "my_thoughts", "takeaway", "status"]

LIST_SQL = """
SELECT p.id, p.title, p.title_zh, p.authors, p.year, p.venue, p.tags, p.rating,
       p.updated_at, p.reading_progress,
       l.research_problem, l.solution, l.innovation, l.my_thoughts, l.takeaway, l.status AS log_status,
       (SELECT COUNT(*) FROM questions q WHERE q.paper_id=p.id AND q.status='open') AS open_questions,
       (SELECT COUNT(*) FROM questions q WHERE q.paper_id=p.id) AS total_questions
FROM papers p LEFT JOIN research_log l ON l.paper_id = p.id
ORDER BY p.updated_at DESC
"""


@router.get("")
def list_log():
    return rows(LIST_SQL)


@router.put("/{pid}")
def upsert_log(pid: str, body: dict = Body(...)):
    if not row("SELECT id FROM papers WHERE id=?", (pid,)):
        raise HTTPException(404, "论文不存在")
    old = row("SELECT * FROM research_log WHERE paper_id=?", (pid,)) or {}
    vals = {f: (body.get(f) if f in body else old.get(f, "")) for f in FIELDS}
    if vals.get("status") not in ("在读", "读完", "搁置"):
        vals["status"] = "在读"
    ex("INSERT INTO research_log(paper_id, research_problem, solution, innovation, my_thoughts, "
       "takeaway, status, updated_at) VALUES(?,?,?,?,?,?,?,?) "
       "ON CONFLICT(paper_id) DO UPDATE SET research_problem=excluded.research_problem, "
       "solution=excluded.solution, innovation=excluded.innovation, "
       "my_thoughts=excluded.my_thoughts, takeaway=excluded.takeaway, "
       "status=excluded.status, updated_at=excluded.updated_at",
       (pid, vals["research_problem"], vals["solution"], vals["innovation"],
        vals["my_thoughts"], vals["takeaway"], vals["status"], now()))
    ex("UPDATE papers SET updated_at=? WHERE id=?", (now(), pid))
    return row("SELECT * FROM research_log WHERE paper_id=?", (pid,))


CSV_COLUMNS = ["标题", "英文标题", "作者", "年份", "来源", "标签", "状态", "评分",
               "研究问题", "解决方案", "创新点", "我的思考", "一句话收获",
               "未解决问题", "总问题", "最后更新"]


@router.get("/export")
def export_log(format: str = "csv"):
    data = rows(LIST_SQL)
    if not data:
        raise HTTPException(400, "暂无数据可导出")
    if format == "md":
        lines = ["# 研究总表", "", f"> 共 {len(data)} 篇 ｜ 导出时间：{now()}", ""]
        for r in data:
            lines += [f"## {r['title_zh'] or r['title']}", ""]
            if r["title"] and r["title"] != r["title_zh"]:
                lines += [f"*{r['title']}*", ""]
            meta = [x for x in [f"作者：{r['authors'] or '-'}", f"年份：{r['year'] or '-'}",
                                f"来源：{r['venue'] or '-'}", f"标签：{r['tags'] or '-'}",
                                f"状态：{r['log_status'] or '未读'}", f"评分：{'★' * (r['rating'] or 0)}",
                                f"问题：{r['open_questions']}/{r['total_questions']}"] if x]
            lines += ["｜".join(meta), ""]
            for label, key in [("研究问题", "research_problem"), ("解决方案", "solution"),
                               ("创新点", "innovation"), ("我的思考", "my_thoughts"),
                               ("一句话收获", "takeaway")]:
                if (r[key] or "").strip():
                    lines += [f"**{label}**：{r[key]}", ""]
            lines += ["---", ""]
        return Response("\n".join(lines), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=research-log.md"})
    # csv（utf-8-sig 方便 Excel 直接打开）
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in data:
        w.writerow([r["title_zh"] or r["title"], r["title"], r["authors"], r["year"],
                    r["venue"], r["tags"], r["log_status"] or "未读", "★" * (r["rating"] or 0),
                    r["research_problem"], r["solution"], r["innovation"], r["my_thoughts"],
                    r["takeaway"], r["open_questions"], r["total_questions"], r["updated_at"]])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=research-log.csv"})
