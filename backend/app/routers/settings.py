"""设置、术语表、引擎测试、数据备份。"""
import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from ..config import DATA_DIR, DB_PATH
from ..db import ex, get_settings, row, rows, set_setting
from ..services.translate import PROVIDER_PRESETS, chat, engine_info, engine_ready
from ..util import now

router = APIRouter(prefix="/api", tags=["settings"])

ALLOWED_KEYS = {"translate_provider", "translate_base_url", "translate_api_key",
                "translate_model", "translate_temperature", "translate_concurrency",
                "parse_engine", "mineru_token", "auto_translate"}


SECRET_KEYS = {"translate_api_key", "mineru_token"}
CLEAR_SECRET_FLAGS = {
    "clear_translate_api_key": "translate_api_key",
    "clear_mineru_token": "mineru_token",
}


def _public_settings(settings=None):
    """Never return configured secrets to the browser; only return presence flags."""
    values = dict(settings if settings is not None else get_settings())
    for key in SECRET_KEYS:
        values[f"{key}_configured"] = bool(values.get(key))
        values[key] = ""
    return values


def _save_settings(body):
    """Empty secret fields mean 'keep existing'; clearing requires an explicit flag."""
    for flag, key in CLEAR_SECRET_FLAGS.items():
        if body.get(flag):
            set_setting(key, "")
    for key, value in body.items():
        if key not in ALLOWED_KEYS:
            continue
        if key in SECRET_KEYS:
            if body.get(f"clear_{key}") or not str(value or "").strip():
                continue
        set_setting(key, str(value))
    return _public_settings()


@router.get("/settings")
def get_all():
    return {"settings": _public_settings(), "presets": PROVIDER_PRESETS}


@router.put("/settings")
def put_settings(body: dict = Body(...)):
    return _save_settings(body)


@router.post("/settings/test-translate")
async def test_translate(body: dict = Body(default={})):
    if body:
        _save_settings(body)
    info = engine_info()
    ok, err = engine_ready(info)
    if not ok:
        return {"ok": False, "message": err}
    try:
        import time
        t0 = time.time()
        reply = await chat(info, [{"role": "user", "content": "请只回复两个字：成功"}],
                           max_tokens=2000)
        return {"ok": True, "message": f"连接成功，模型回复：{reply[:50]}",
                "latency": round(time.time() - t0, 1),
                "engine": f"{info['provider']}/{info['model']}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"调用失败：{str(e)[:300]}"}


@router.post("/settings/test-parse")
async def test_parse(body: dict = Body(default={})):
    engine = body.get("engine") or get_settings().get("parse_engine", "builtin")
    if engine == "builtin":
        try:
            import fitz  # noqa: F401
            return {"ok": True, "message": "内置解析引擎可用（零依赖、速度快；复杂公式建议用 MinerU）"}
        except ImportError:
            return {"ok": False, "message": "PyMuPDF 未安装，请 pip install PyMuPDF"}
    if engine == "mineru_api":
        token = body.get("token") or get_settings().get("mineru_token", "")
        if not token:
            return {"ok": False, "message": "请先填写 MinerU API Token（mineru.net 获取）"}
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://mineru.net/api/v4/extract-results/batch/0",
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (200, 400, 404):
            return {"ok": True, "message": "Token 已配置，网络可达（真实解析以实际任务为准）"}
        if r.status_code == 401:
            return {"ok": False, "message": "Token 无效或已过期（HTTP 401）"}
        return {"ok": False, "message": f"MinerU 服务不可达：HTTP {r.status_code}"}
    if engine == "mineru_local":
        import shutil
        exe = shutil.which("mineru") or shutil.which("magic-pdf")
        return ({"ok": True, "message": f"检测到本地 MinerU：{exe}"} if exe
                else {"ok": False, "message": "未检测到 mineru 命令，请 pip install mineru"})
    return {"ok": False, "message": f"未知引擎：{engine}"}


# ---------- 术语表 ----------

@router.get("/glossary")
def glossary_list():
    return rows("SELECT * FROM glossary ORDER BY term_en")


@router.post("/glossary")
def glossary_add(body: dict = Body(...)):
    en = (body.get("term_en") or "").strip()
    if not en:
        raise HTTPException(400, "英文术语不能为空")
    ex("INSERT OR IGNORE INTO glossary(term_en, term_zh, note, source) VALUES(?,?,?, 'manual')",
       (en, body.get("term_zh", ""), body.get("note", "")))
    ex("UPDATE glossary SET term_zh=?, note=? WHERE term_en=?",
       (body.get("term_zh", ""), body.get("note", ""), en))
    return row("SELECT * FROM glossary WHERE term_en=?", (en,))


@router.patch("/glossary/{gid}")
def glossary_update(gid: int, body: dict = Body(...)):
    ex("UPDATE glossary SET term_zh=?, note=? WHERE id=?",
       (body.get("term_zh", ""), body.get("note", ""), gid))
    return row("SELECT * FROM glossary WHERE id=?", (gid,))


@router.delete("/glossary/{gid}")
def glossary_delete(gid: int):
    ex("DELETE FROM glossary WHERE id=?", (gid,))
    return {"ok": True}


# ---------- 备份 ----------

@router.get("/backup.zip")
def backup():
    """Create a consistent SQLite snapshot, then package it with paper assets."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="paperlib-backup-") as temp_dir:
        snapshot = Path(temp_dir) / DB_PATH.name
        # sqlite3.Connection.backup includes committed WAL changes in a consistent snapshot.
        with sqlite3.connect(DB_PATH, timeout=20) as source:
            with sqlite3.connect(snapshot) as target:
                source.backup(target)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, DB_PATH.name)
            for p in sorted(DATA_DIR.rglob("*")):
                if not p.is_file():
                    continue
                # The snapshot above is authoritative; do not mix live DB / WAL / SHM files.
                if p.name in {DB_PATH.name, f"{DB_PATH.name}-wal", f"{DB_PATH.name}-shm"}:
                    continue
                if p.name.startswith(f"{DB_PATH.name}.preclean-") or "mineru_" in p.name:
                    continue
                zf.write(p, p.relative_to(DATA_DIR))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition":
                 f"attachment; filename=paper-library-backup-{now()[:10]}.zip"})
