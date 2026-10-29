"""问题追踪。"""
from fastapi import APIRouter, Body, HTTPException, Query

from ..db import ex, row, rows
from ..util import now

router = APIRouter(prefix="/api/questions", tags=["questions"])


@router.get("")
def list_questions(paper_id: str | None = Query(default=None),
                   status: str | None = Query(default=None)):
    sql = "SELECT q.*, p.title, p.title_zh FROM questions q " \
          "LEFT JOIN papers p ON p.id=q.paper_id WHERE 1=1"
    args = []
    if paper_id:
        sql += " AND q.paper_id=?"
        args.append(paper_id)
    if status:
        sql += " AND q.status=?"
        args.append(status)
    sql += " ORDER BY (q.status='open') DESC, q.created_at DESC"
    return rows(sql, tuple(args))


@router.post("")
def create_question(body: dict = Body(...)):
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "问题内容不能为空")
    qid = ex("INSERT INTO questions(paper_id, content, status, created_at) VALUES(?,?,?,?)",
             (body.get("paper_id"), content, "open", now()))
    return row("SELECT * FROM questions WHERE id=?", (qid,))


@router.patch("/{qid}")
def update_question(qid: int, body: dict = Body(...)):
    q = row("SELECT * FROM questions WHERE id=?", (qid,))
    if not q:
        raise HTTPException(404, "问题不存在")
    if "content" in body:
        ex("UPDATE questions SET content=? WHERE id=?", (body["content"], qid))
    if "answer" in body:
        ex("UPDATE questions SET answer=? WHERE id=?", (body["answer"], qid))
    if body.get("status") in ("open", "resolved"):
        if body["status"] == "resolved":
            ex("UPDATE questions SET status='resolved', resolved_at=? WHERE id=?", (now(), qid))
        else:
            ex("UPDATE questions SET status='open', resolved_at='' WHERE id=?", (qid,))
    return row("SELECT * FROM questions WHERE id=?", (qid,))


@router.delete("/{qid}")
def delete_question(qid: int):
    ex("DELETE FROM questions WHERE id=?", (qid,))
    return {"ok": True}
