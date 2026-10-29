"""MinerU 解析适配器：云端 API（mineru.net）与本地 CLI 两种方式。

云端需要在设置页填入 mineru.net 的 API Token（有每日免费额度）；
本地需要 `pip install mineru`（对机器有要求，弱机不推荐）。
"""
import asyncio
import shutil
import zipfile
from pathlib import Path

import httpx

from .md2blocks import from_mineru_dir

API_BASE = "https://mineru.net"


async def parse_cloud(pdf_path: Path, work_dir: Path, token: str) -> dict:
    """官方 v4 batch 上传流程：申请链接 → PUT 上传（自动建任务）→ 轮询 batch 结果 → 下载 zip。"""
    if not token:
        raise RuntimeError("未配置 MinerU API Token，请到设置页填写")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{API_BASE}/api/v4/file-urls/batch", headers=headers, json={
            "enable_formula": True, "enable_table": True, "language": "en",
            "model_version": "vlm",
            "files": [{"name": pdf_path.name}],
        })
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"MinerU 申请上传链接失败：{d.get('msg')}")
        batch_id = d["data"]["batch_id"]
        put_url = d["data"]["file_urls"][0]

        # 预签名 URL：不能带任何请求头（官方文档明确要求）
        pr = await c.put(put_url, content=pdf_path.read_bytes())
        if pr.status_code not in (200, 201):
            raise RuntimeError(f"MinerU 上传文件失败：HTTP {pr.status_code}")

        # 上传完成后系统自动创建解析任务，轮询 batch 结果（容网络抖动；上限 25 分钟）
        zip_url = None
        net_errs = 0
        for _ in range(300):
            await asyncio.sleep(5)
            try:
                rr = await c.get(f"{API_BASE}/api/v4/extract-results/batch/{batch_id}",
                                 headers=headers)
                j = rr.json()
            except Exception:  # noqa: BLE001 单次轮询网络抖动不致命
                net_errs += 1
                if net_errs > 20:
                    raise RuntimeError("MinerU 轮询持续网络失败")
                continue
            results = (j.get("data") or {}).get("extract_result") or []
            item = results[0] if results else {}
            state = item.get("state")
            if state == "done":
                zip_url = item.get("full_zip_url")
                break
            if state == "failed":
                raise RuntimeError("MinerU 解析失败：" + str(item.get("err_msg") or ""))
        if not zip_url:
            raise TimeoutError("MinerU 解析超时（>25 分钟）")

    # 下载解析结果 zip：优先系统 curl（部分网络下 httpx 对该 CDN 的连接被重置）
    tmp = work_dir / "_mineru_result.zip"
    content = None
    for use_curl in (True, True, False):
        try:
            if use_curl:
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sL", "-m", "280", "-o", str(tmp), zip_url)
                if (await proc.wait()) == 0 and tmp.exists() and tmp.stat().st_size > 1000:
                    content = tmp.read_bytes()
                    break
            else:
                async with httpx.AsyncClient(timeout=300) as c:
                    zr = await c.get(zip_url)
                    zr.raise_for_status()
                    content = zr.content
                break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(3)
    tmp.unlink(missing_ok=True)
    if content is None:
        raise RuntimeError("下载 MinerU 解析结果失败（CDN 不可达，请检查网络/代理）")
    extract_dir = work_dir / "mineru_extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    zf = zipfile.ZipFile(__import__("io").BytesIO(content))
    zf.extractall(extract_dir)
    return from_mineru_dir(extract_dir, work_dir)


async def parse_local(pdf_path: Path, work_dir: Path) -> dict:
    exe = shutil.which("mineru") or shutil.which("magic-pdf")
    if not exe:
        raise RuntimeError("未检测到 MinerU，请先 `pip install mineru`，或改用其他解析引擎")
    out = work_dir / "mineru_out"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        exe, "-p", str(pdf_path), "-o", str(out),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("MinerU 本地解析失败：" + stdout.decode(errors="ignore")[-800:])
    return from_mineru_dir(out, work_dir)
