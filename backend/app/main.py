"""论文图书馆 · 本地后端入口。

启动：.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --app-dir backend
（或直接运行 启动.bat）
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from .config import DATA_DIR, FRONTEND_DIST
from .db import init_db
from .routers import annotations, dict, log, papers, questions, settings

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

# 论文静态资源（PDF / 图片）：/files/papers/{id}/...
app.mount("/files", StaticFiles(directory=DATA_DIR), name="files")


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


if __name__ == "__main__":
    import uvicorn
    from .config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)
