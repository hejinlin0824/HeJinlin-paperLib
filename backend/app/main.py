"""论文图书馆 · 本地后端入口。

启动：.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --app-dir backend
（或直接运行 启动.bat）
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from .config import FRONTEND_DIST, PAPERS_DIR
from .db import init_db
from .util import now
from .routers import annotations, dict, kg, kg_understanding, kg_intelligence_v4, log, papers, questions, settings

app = FastAPI(title="论文图书馆", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(papers.router)
app.include_router(annotations.router)
app.include_router(questions.router)
app.include_router(log.router)
app.include_router(settings.router)
app.include_router(dict.router)
app.include_router(kg.router)
app.include_router(kg_understanding.router)\napp.include_router(kg_intelligence_v4.router)

# 只公开论文 PDF / 图片目录；绝不能把整个 data/ 挂载出去（其中包含 library.db 和密钥）。
# URL 继续保持 /files/papers/{paper_id}/...，与阅读器现有链接兼容。
app.mount("/files/papers", StaticFiles(directory=PAPERS_DIR), name="paper-files")


class SPAStatic(StaticFiles):
    """前端 SPA：未命中的路径回退到 index.html（history 路由）。"""

    async def get_response(self, path: str, scope):
        from starlette.exceptions import HTTPException as StarletteHTTPException
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404:
                return FileResponse(self.directory / "index.html")
            raise
        if resp.status_code == 404:
            return FileResponse(self.directory / "index.html")
        return resp


if (FRONTEND_DIST / "index.html").exists():
    app.mount("/", SPAStatic(directory=FRONTEND_DIST, html=True), name="spa")


@app.on_event("startup")
def _startup():
    init_db()
    # V3 理解任务也需要在异常退出后恢复，避免草稿永久停留在 running。
    from .services import kg_understanding
    kg_understanding.ensure_tables()
    from .db import db as _db
    with _db() as c:
        c.execute("UPDATE kg_understanding_drafts SET status='failed', warnings_json=?, updated_at=? WHERE status='running'", ('["服务重启导致任务中断，请重新生成理解档案"]', now()))
    # 僵尸任务恢复：进程刚启动，不可能有正在运行的任务；
    # 把上次服务退出时卡住的翻译/解析状态复位，让用户可以重新触发
    from .db import db
    with db() as c:
        c.execute("UPDATE papers SET status='parsed' WHERE status IN ('translating', 'queued')")
        c.execute("UPDATE papers SET explain_total=0, explain_done=0")
        c.execute(
            "UPDATE papers SET status=CASE WHEN (SELECT COUNT(*) FROM blocks b "
            "WHERE b.paper_id=papers.id)>0 THEN 'parsed' ELSE 'failed' END, "
            "error=CASE WHEN (SELECT COUNT(*) FROM blocks b WHERE b.paper_id=papers.id)>0 "
            "THEN error ELSE '任务因服务重启中断，请重新解析' END "
            "WHERE status IN ('parsing', 'pending_parse')")
        # 知识抽取任务同样做僵尸恢复：进程刚启动不可能有正在运行的任务
        c.execute("UPDATE kg_extraction_jobs SET status='failed', stage='', "
                  "error_message='任务因服务重启中断，可重试' "
                  "WHERE status IN ('running', 'pending')")


if __name__ == "__main__":
    import uvicorn
    from .config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)
