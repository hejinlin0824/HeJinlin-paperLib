"""V4 API: local-first paper intelligence and isolated understanding drafts."""
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query
from ..services import kg_understanding_v4 as service

router = APIRouter(prefix="/api/knowledge-graph/v4", tags=["knowledge-graph-v4"])


@router.get("/drafts")
def drafts(paper_id: str | None = Query(None)):
    return service.list_drafts(paper_id)


@router.get("/drafts/{draft_id}")
def draft_detail(draft_id: str):
    value = service.get_draft(draft_id)
    if not value:
        raise HTTPException(404, "理解档案不存在")
    return value


@router.post("/papers/{paper_id}/understand")
def start_understanding(paper_id: str, background: BackgroundTasks):
    try:
        draft = service.create_draft(paper_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background.add_task(service.run_understanding, draft["id"])
    return {"draft": draft, "message": "V4 论文理解任务已启动；本地检索索引与规则候选先行，模型推理失败时保留部分结果"}


@router.put("/drafts/{draft_id}")
def save_draft(draft_id: str, body: dict = Body(...)):
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload 必须是对象")
    try:
        return service.update_review(draft_id, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/drafts/{draft_id}/review")
def review_draft(draft_id: str, body: dict = Body(...)):
    status = str(body.get("status") or "")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(400, "必须提交审阅后的 payload")
    if status not in ("approved", "rejected"):
        raise HTTPException(400, "status 必须为 approved 或 rejected")
    try:
        return service.update_review(draft_id, payload, status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/papers/{paper_id}/index")
def index_paper(paper_id: str, body: dict = Body(default={})):
    try:
        return service.index_paper(paper_id, bool(body.get("include_embeddings", False)))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.get("/papers/{paper_id}/index")
def index_status(paper_id: str):
    return service.index_status(paper_id) or {"paper_id": paper_id, "index_status": "not_indexed"}


@router.get("/search")
def search(q: str = Query(..., min_length=1), paper_id: str | None = Query(None),
           top_k: int = Query(8, ge=1, le=50)):
    try:
        return service.search_blocks(q, paper_id, top_k)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@router.get("/graph-context")
def graph_context(q: str = Query(..., min_length=1), paper_id: str | None = Query(None),
                 top_k: int = Query(10, ge=1, le=50)):
    return service.search_graph_context(q, paper_id, top_k)


@router.get("/papers/{paper_id}/structure-candidates")
def structure_candidates(paper_id: str):
    try:
        return service.structure_candidates(paper_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))



@router.post("/structured-output/probe")
async def probe_structured_output():
    """Explicitly test whether the configured provider/model accepts strict JSON Schema."""
    return await service.probe_structured_output()
