"""批注：高亮 / 加粗 / 批注 / 小结 / 问题标记。"""
from fastapi import APIRouter, Body, HTTPException

from ..db import ex, row, rows
from ..util import now

router = APIRouter(prefix="/api/papers/{pid}/annotations", tags=["annotations"])

KINDS = {"highlight", "bold", "note", "summary", "question"}


@router.get("")
def list_annotations(pid: str):
    return rows(
        "SELECT a.*, b.order_idx FROM annotations a "
        "LEFT JOIN blocks b ON b.id = a.block_id "
        "WHERE a.paper_id=? ORDER BY a.created_at DESC", (pid,))


@router.post("")
def create_annotation(pid: str, body: dict = Body(...)):
    kind = body.get("kind")
    if kind not in KINDS:
        raise HTTPException(400, f"kind 必须是 {KINDS}")
    aid = ex(
        "INSERT INTO annotations(paper_id, block_id, kind, surface, anchor_text, color, content, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, body.get("block_id"), kind, body.get("surface", "zh"),
         (body.get("anchor_text") or "")[:2000], body.get("color") or "",
         body.get("content") or "", now(), now()))
    # 问题标记同步建一条问题记录，进入问题追踪
    if kind == "question":
        ex("INSERT INTO questions(paper_id, annotation_id, content, status, created_at) "
           "VALUES(?,?,?,'open',?)", (pid, aid, body.get("content") or "", now()))
    return row("SELECT * FROM annotations WHERE id=?", (aid,))


@router.patch("/{aid}")
def update_annotation(pid: str, aid: int, body: dict = Body(...)):
    fields = {k: body[k] for k in ("content", "color") if k in body}
    if not fields:
        raise HTTPException(400, "无可更新字段")
    sets = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE annotations SET {sets}, updated_at=? WHERE id=? AND paper_id=?",
       (*fields.values(), now(), aid, pid))
    return row("SELECT * FROM annotations WHERE id=?", (aid,))


@router.delete("/{aid}")
def delete_annotation(pid: str, aid: int):
    ex("DELETE FROM annotations WHERE id=? AND paper_id=?", (aid, pid))
    ex("DELETE FROM questions WHERE annotation_id=?", (aid,))
    return {"ok": True}
