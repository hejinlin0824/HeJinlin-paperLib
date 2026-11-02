"""Human-review API for isolated V3 paper-understanding drafts."""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Body, Query
from ..services import kg_understanding_v31 as service

router = APIRouter(prefix="/api/knowledge-graph/v3", tags=["knowledge-graph-v3"])

@router.get("/drafts")
def drafts(paper_id: str | None = Query(None)):
    return service.list_drafts(paper_id)

@router.get("/drafts/{draft_id}")
def draft_detail(draft_id: str):
    value = service.get_draft(draft_id)
    if not value: raise HTTPException(404, "理解档案不存在")
    return value

@router.post("/papers/{paper_id}/understand")
def start_understanding(paper_id: str, background: BackgroundTasks):
    try:
        draft = service.create_draft(paper_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background.add_task(service.run_understanding, draft["id"])
    return {"draft": draft, "message": "已启动全文理解；完成后可在审阅页查看贡献树和关系候选"}

@router.put("/drafts/{draft_id}")
def save_draft(draft_id: str, body: dict = Body(...)):
    payload = body.get("payload")
    if not isinstance(payload, dict): raise HTTPException(400, "payload 必须是对象")
    try: return service.update_review(draft_id, payload)
    except ValueError as exc: raise HTTPException(400, str(exc))

@router.post("/drafts/{draft_id}/review")
def review_draft(draft_id: str, body: dict = Body(...)):
    status = str(body.get("status") or "")
    payload = body.get("payload")
    if not isinstance(payload, dict): raise HTTPException(400, "必须提交审阅后的 payload")
    if status not in ("approved","rejected"): raise HTTPException(400, "status 必须为 approved 或 rejected")
    try: return service.update_review(draft_id, payload, status)
    except ValueError as exc: raise HTTPException(400, str(exc))
