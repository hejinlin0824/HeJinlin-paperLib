"""知识图谱 API：查询 + 抽取任务管理。所有列表查询都有上限，不暴露内部路径/密钥。"""
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query

from ..db import ex, row, rows
from ..services import kg_extract, kg_queries, kg_eligibility
from ..util import now

router = APIRouter(prefix="/api/knowledge-graph", tags=["knowledge-graph"])


@router.get("/overview")
def overview():
    return kg_queries.overview()


@router.get("/graph")
def graph(paper_id: str | None = Query(None),
          entity_ids: str | None = Query(None, description="逗号分隔的实体 id"),
          types: str | None = Query(None, description="逗号分隔的实体类型"),
          relation_types: str | None = Query(None, description="逗号分隔的关系类型"),
          evidence_types: str | None = Query("explicit", description="证据类型：explicit/inferred/similarity；默认仅原文明确证据"),
          q: str | None = Query(None, max_length=120),
          limit: int = Query(250, ge=20, le=400)):
    return kg_queries.graph(
        paper_id=paper_id,
        entity_ids=[x for x in (entity_ids or "").split(",") if x] or None,
        types=[x for x in (types or "").split(",") if x] or None,
        relation_types=[x for x in (relation_types or "").split(",") if x] or None,
        evidence_types=[x for x in (evidence_types or "").split(",") if x],
        q=q, limit=limit)


@router.get("/entities")
def search_entities(q: str = Query("", max_length=120),
                    type: str = Query("", max_length=40),
                    limit: int = Query(50, ge=1, le=200)):
    like = f"%{q.strip()}%"
    args: list = []
    conds = []
    if q.strip():
        conds.append("(e.canonical_name LIKE ? OR e.normalized_name LIKE ?)")
        args += [like, like]
    if type:
        conds.append("e.entity_type=?")
        args.append(type)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    args.append(max(1, min(limit, 200)))
    return rows(
        f"SELECT e.id, e.entity_type, e.canonical_name AS name, e.description, "
        f"(SELECT COUNT(*) FROM kg_visible_relations r WHERE r.source_entity_id=e.id OR "
        f"r.target_entity_id=e.id) degree FROM kg_visible_entities e {where} "
        f"ORDER BY degree DESC, LENGTH(e.canonical_name) LIMIT ?", tuple(args))


@router.patch("/papers/{pid}/scope")
def set_paper_scope(pid: str, body: dict = Body(...)):
    """把论文标记为正常文献、测试数据或排除项；不会删除任何原始数据。"""
    role = str(body.get("library_role") or "").strip().lower()
    try:
        updated = kg_eligibility.set_paper_role(pid, role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not updated:
        raise HTTPException(404, "论文不存在")
    return {"paper_id": pid, "library_role": role,
            "message": "分类已更新；测试/排除论文不会再出现在知识图谱中"}


@router.get("/entities/{eid}")
def entity_detail(eid: str):
    d = kg_queries.entity_detail(eid)
    if not d:
        raise HTTPException(404, "实体不存在")
    return d


@router.get("/entities/{eid}/neighbors")
def entity_neighbors(eid: str, limit: int = Query(60, ge=10, le=120)):
    if not row("SELECT id FROM kg_visible_entities WHERE id=?", (eid,)):
        raise HTTPException(404, "实体不存在")
    return kg_queries.neighbors(eid, limit)


@router.get("/relations/{rid}")
def relation_detail(rid: str):
    d = kg_queries.relation_detail(rid)
    if not d:
        raise HTTPException(404, "关系不存在")
    return d


@router.get("/relations/{rid}/evidence")
def relation_evidence(rid: str):
    d = kg_queries.relation_detail(rid)
    if not d:
        raise HTTPException(404, "关系不存在")
    return {"relation": {k: d[k] for k in ("id", "relation_type", "evidence_type",
                                           "confidence", "source", "target")},
            "evidence": d["evidence"]}


@router.get("/papers/{pid}")
def paper_knowledge(pid: str):
    d = kg_queries.paper_structure(pid)
    if not d:
        raise HTTPException(404, "论文不存在")
    return d


@router.get("/topics")
def topics():
    return kg_queries.topics()


@router.get("/jobs")
def jobs(limit: int = Query(50, ge=1, le=200)):
    return kg_queries.jobs_list(limit)


@router.post("/papers/{pid}/extract")
async def extract_paper(pid: str, background: BackgroundTasks):
    try:
        job = kg_extract.create_job(pid)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    background.add_task(kg_extract.run_job, job["id"])
    return {"job": job}


@router.post("/rebuild")
async def rebuild(background: BackgroundTasks):
    """只为缺当前版本抽取结果的论文建任务，不会重复抽取。"""
    job_ids = kg_extract.rebuild_missing()
    if job_ids:
        background.add_task(kg_extract.run_jobs_sequential, job_ids)
    return {"created": len(job_ids), "job_ids": job_ids}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, background: BackgroundTasks):
    job = row("SELECT * FROM kg_visible_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    if job["status"] in ("pending", "running"):
        raise HTTPException(409, "任务已在进行中")
    busy = row("SELECT id FROM kg_visible_jobs WHERE paper_id=? AND id!=? AND "
               "status IN ('pending','running')", (job["paper_id"], job_id))
    if busy:
        raise HTTPException(409, "该论文已有其他抽取任务在进行中")
    ex("UPDATE kg_extraction_jobs SET status='pending', error_message='', stage='', "
       "progress=0, updated_at=? WHERE id=?", (now(), job_id))
    background.add_task(kg_extract.run_job, job_id)
    return {"ok": True}


@router.post("/translate-names")
async def translate_names():
    """为还没有中文名的实体批量补翻（结果缓存进库，一次性成本）。"""
    try:
        updated = await kg_extract.translate_missing_names()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"updated": updated}
