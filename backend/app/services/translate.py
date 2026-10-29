"""翻译引擎抽象（OpenAI 兼容协议）与调度。

- 云端：DeepSeek / 智谱GLM / Kimi / OpenAI / 任意兼容端点；
- 本地：Ollama（http://127.0.0.1:11434/v1，同协议，零改造接入）；
- mock：不联网的测试引擎，方便无 Key 时体验全流程。
"""
import asyncio
import base64
import json
import re

import httpx

from ..config import PAPERS_DIR, TRANSLATABLE_TYPES
from ..db import ex, get_settings, row, rows
from ..util import dumps, loads, now

PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
    "mock": {"base_url": "", "model": "mock"},
    "custom": {"base_url": "", "model": ""},
}

SYSTEM_PROMPT = """你是一位资深的学术论文翻译专家，负责将英文论文内容翻译为中文。要求：
1. 忠实原意、表达地道，符合中文学术论文的写作习惯，避免机翻腔；
2. 专业术语严格遵循【术语表】中的译法；表外术语采用学界通用译名，仅在全文首次出现时括注英文原文（如：注意力机制（attention））；
3. 严格保留原文中的所有格式标记：LaTeX 公式（$...$、\\(...\\) 等）、引用编号（如 [3]、[3,5]）、markdown 标记、专有名词与缩写（如 BERT、ResNet、GPT-4）一律原样保留，不增不减；
4. 图表标题风格："Figure 3:" 译为 "图 3："，"Table 2:" 译为 "表 2："；
5. 只输出译文本身，不要任何解释、前言、后记或代码块标记。"""


def engine_info() -> dict:
    s = get_settings()
    p = s.get("translate_provider", "mock")
    preset = PROVIDER_PRESETS.get(p, {})
    try:
        temp = float(s.get("translate_temperature", "0.3"))
    except ValueError:
        temp = 0.3
    try:
        conc = max(1, int(s.get("translate_concurrency", "4")))
    except ValueError:
        conc = 4
    return {
        "provider": p,
        "base_url": (s.get("translate_base_url") or preset.get("base_url", "")).rstrip("/"),
        "api_key": s.get("translate_api_key", ""),
        "model": s.get("translate_model") or preset.get("model", ""),
        "temperature": temp,
        "concurrency": conc,
    }


def engine_ready(info: dict) -> tuple:
    """返回 (ok, 错误消息)。"""
    if info["provider"] == "mock":
        return True, ""
    if not info["base_url"]:
        return False, "未配置 API 地址（Base URL）"
    if not info["model"]:
        return False, "未配置模型名称"
    if not info["api_key"] and info["provider"] != "ollama":
        return False, "未配置 API Key"
    return True, ""


def _mock(text: str) -> str:
    # 只回显【待翻译文本】之后的部分，方便无 Key 时体验真实排版
    marker = "【待翻译文本】"
    t = text.split(marker, 1)[-1] if marker in text else text
    t = t.strip()
    return "【Mock 译文】" + (t[:150] + "…" if len(t) > 150 else t)


def _clean(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    for pref in ("译文：", "译文:", "翻译：", "翻译:"):
        if t.startswith(pref):
            t = t[len(pref):]
    # 模型可能复述原文里的替换字符，剥掉
    return t.replace("\ufffd", "").strip()


async def chat(info: dict, messages: list, max_tokens: int | None = None) -> str:
    """调用 OpenAI 兼容 /chat/completions，带重试。"""
    if info["provider"] == "mock":
        await asyncio.sleep(0.05)
        return _mock(messages[-1]["content"])
    body = {"model": info["model"], "messages": messages,
            "temperature": info["temperature"]}
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"Content-Type": "application/json"}
    if info["api_key"]:
        headers["Authorization"] = f"Bearer {info['api_key']}"
    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180)) as c:
                r = await c.post(f"{info['base_url']}/chat/completions",
                                 headers=headers, json=body)
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            else:
                r.raise_for_status()
                data = r.json()
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if not content:
                    # 思考型模型可能把 max_tokens 耗在推理上
                    last_err = RuntimeError("模型返回了空内容（思考型模型？请勿限制 max_tokens）")
                else:
                    return _clean(content)
        except Exception as e:  # noqa: BLE001
            last_err = e
        await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"翻译请求失败（已重试3次）：{last_err}")


def _glossary_text() -> str:
    gs = rows("SELECT term_en, term_zh FROM glossary ORDER BY LENGTH(term_en) DESC LIMIT 200")
    return "\n".join(f"{g['term_en']} => {g['term_zh']}" for g in gs if g["term_zh"])


def _user_prompt(text: str, ctx: str, glossary: str, extra: str = "") -> str:
    parts = []
    if ctx:
        parts.append(f"【论文】{ctx[:300]}")
    if glossary:
        parts.append(f"【术语表】\n{glossary}")
    if extra:
        parts.append(extra)
    parts.append(f"【待翻译文本】\n{text}")
    return "\n\n".join(parts)


async def translate_text(info: dict, text: str, ctx: str = "", extra: str = "") -> str:
    gl = _glossary_text()
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(text, ctx, gl, extra)}]
    return await chat(info, msgs)


async def _translate_block(info: dict, b: dict, ctx: str, glossary: str) -> str:
    extra = ""
    if b["type"] == "heading":
        extra = "这是一个标题，译文需简洁凝练，不要加句号。"
    elif b["type"] == "image":
        extra = "这是图片的标题说明（caption），译为“图 N：xxx”风格，一句话。"
    elif b["type"] == "list":
        extra = ("这是分条列表：译文保持分条结构，每条一行并保留行首的破折号/圆点/编号符号，"
                 "条目顺序与数量不变。")
    elif b["type"] == "table":
        en = b["en"] or ""
        if en.lstrip().startswith("|") or "\n|" in en:
            extra = ("这是一个 markdown 表格：翻译所有单元格中的文本，"
                     "严格保持表格结构（| 分隔、行数列数）完全不变，不要输出解释。")
        else:
            extra = "这是表格的标题说明（caption），译为“表 N：xxx”风格，一句话。"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(b["en"], ctx, glossary, extra)}]
    return await chat(info, msgs)


# ---------- 批量翻译：一次请求带多段，<seg> 标记拆回，大幅提速 ----------

BATCH_MAX_CHARS = 2400
BATCH_MAX_SEGS = 12

BATCH_EXTRA = (
    "\n6. 本次是批量翻译：待翻译文本由多个 <seg n>…</seg> 段落组成。"
    "逐段翻译，输出时严格保持相同的 <seg n>…</seg> 标记、编号与顺序，"
    "段号一一对应，不合并、不拆分、不遗漏任何一段；"
    "若某段是 markdown 表格，翻译单元格内容但严格保持 | 结构不变。"
)


def _build_batches(todo: list, max_chars: int = BATCH_MAX_CHARS,
                   max_segs: int = BATCH_MAX_SEGS) -> list:
    batches, cur, cur_chars = [], [], 0
    for b in todo:
        length = len(b["en"] or "")
        if cur and (cur_chars + length > max_chars or len(cur) >= max_segs):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(b)
        cur_chars += length
    if cur:
        batches.append(cur)
    return batches


async def _translate_batch(info: dict, batch: list, ctx: str, glossary: str) -> list:
    """返回 [(block, zh), ...]；个别段解析失败时对该段单独兜底请求。"""
    segs = "\n".join(f"<seg {i + 1}>\n{b['en']}\n</seg>" for i, b in enumerate(batch))
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + BATCH_EXTRA},
            {"role": "user", "content": _user_prompt(segs, ctx, glossary)}]
    raw = await chat(info, msgs)
    got = {}
    for n, text in re.findall(r"<seg\s*(\d+)\s*>([\s\S]*?)</seg>", raw):
        got[int(n)] = re.sub(r"</?\s*seg\s*\d*\s*>", "", text).strip()
    out = []
    for i, b in enumerate(batch):
        zh = got.get(i + 1, "")
        if zh:
            out.append((b, zh))
        else:
            out.append((b, await _translate_block(info, b, ctx, glossary)))
    return out


async def translate_paper_job(pid: str, force: bool = False):
    """整篇翻译后台任务：逐块并发，进度写回 papers 表（前端轮询展示）。"""
    paper = row("SELECT * FROM papers WHERE id=?", (pid,))
    if not paper:
        return
    info = engine_info()
    ok, err = engine_ready(info)
    if not ok:
        ex("UPDATE papers SET status='failed', error=?, updated_at=? WHERE id=?",
           (f"翻译引擎未就绪：{err}（请到设置页配置）", now(), pid))
        return
    blocks = rows("SELECT * FROM blocks WHERE paper_id=? ORDER BY order_idx", (pid,))
    todo = [b for b in blocks
            if b["type"] in TRANSLATABLE_TYPES and (b["en"] or "").strip()
            and (force or not (b["zh"] or "").strip() or b["translate_status"] != "done")]
    if force:
        ex("UPDATE papers SET title_zh='' WHERE id=?", (pid,))
        paper = dict(paper, title_zh="")
    # 标题翻译：无论是否有待翻译正文块，标题缺译都先补上
    if not (paper["title_zh"] or "").strip() and (paper["title"] or "").strip():
        try:
            t = await translate_text(
                info, paper["title"], ctx=paper["title"],
                extra="这是论文标题：译文要简洁凝练、符合中文论文标题命名习惯，"
                      "避免连续堆叠'的'，可适当意译。")
            ex("UPDATE papers SET title_zh=? WHERE id=?", (t, pid))
            paper = dict(paper, title_zh=t)
        except Exception:  # noqa: BLE001
            pass
    if not todo:
        ex("UPDATE papers SET status='ready', updated_at=? WHERE id=?", (now(), pid))
        return
    ex("UPDATE papers SET status='translating', translate_done=0, translate_total=?, "
       "translate_engine=?, error='', updated_at=? WHERE id=?",
       (len(todo), f"{info['provider']}/{info['model']}", now(), pid))
    ctx = paper["title_zh"] or paper["title"] or ""
    glossary = _glossary_text()
    sem = asyncio.Semaphore(info["concurrency"])
    state = {"done": 0, "errors": 0}

    def _save_ok(b, zh):
        ex("UPDATE blocks SET zh=?, translate_status='done' WHERE id=?", (zh, b["id"]))

    def _save_err(b, e):
        state["errors"] += 1
        m = loads(b["meta"])
        m["error"] = str(e)[:300]
        ex("UPDATE blocks SET translate_status='error', meta=? WHERE id=?", (dumps(m), b["id"]))

    async def one_batch(batch):
        async with sem:
            try:
                for b, zh in await _translate_batch(info, batch, ctx, glossary):
                    _save_ok(b, zh)
            except Exception as e:  # noqa: BLE001
                # 整批失败 → 逐段降级，尽量抢救
                for b in batch:
                    try:
                        zh = await _translate_block(info, b, ctx, glossary)
                        _save_ok(b, zh)
                    except Exception as e2:  # noqa: BLE001
                        _save_err(b, e2)
        state["done"] += len(batch)
        ex("UPDATE papers SET translate_done=? WHERE id=?", (state["done"], pid))

    await asyncio.gather(*[one_batch(batch) for batch in _build_batches(todo)])

    # 失败段自动重试一轮（引擎偶发 500 等瞬时故障）
    retry = rows("SELECT * FROM blocks WHERE paper_id=? AND translate_status='error'", (pid,))
    for b in retry:
        try:
            zh = await _translate_block(info, b, ctx, glossary)
            _save_ok(b, zh)
        except Exception:  # noqa: BLE001
            pass
    final_err = row("SELECT COUNT(*) n FROM blocks WHERE paper_id=? AND translate_status='error'",
                    (pid,))["n"]
    if final_err and final_err >= len(todo):
        ex("UPDATE papers SET status='failed', error=?, updated_at=? WHERE id=?",
           (f"全部 {final_err} 个段落翻译失败，请检查引擎配置或换模型", now(), pid))
        return
    ex("UPDATE papers SET status='ready', error=?, updated_at=? WHERE id=?",
       (f"{final_err} 个段落翻译失败，可在阅读器中逐段重试" if final_err else "",
        now(), pid))
    if not state["errors"] and info["provider"] != "mock":
        try:
            await extract_terms(pid)
        except Exception:  # noqa: BLE001
            pass


async def translate_block_sync(pid: str, block_id: int) -> dict:
    """单段重译（阅读器里点按钮），同步返回更新后的块。"""
    b = row("SELECT * FROM blocks WHERE id=? AND paper_id=?", (block_id, pid))
    if not b:
        raise RuntimeError("段落不存在")
    info = engine_info()
    ok, err = engine_ready(info)
    if not ok:
        raise RuntimeError(f"翻译引擎未就绪：{err}")
    paper = row("SELECT title, title_zh FROM papers WHERE id=?", (pid,))
    ctx = (paper or {}).get("title_zh") or (paper or {}).get("title") or ""
    zh = await _translate_block(info, b, ctx, _glossary_text())
    ex("UPDATE blocks SET zh=?, translate_status='done' WHERE id=?", (zh, block_id))
    return row("SELECT * FROM blocks WHERE id=?", (block_id,))


async def extract_metadata(pid: str):
    """解析后用 LLM 补充元数据（标题/作者/年份/来源），尽力而为。"""
    info = engine_info()
    ok, _ = engine_ready(info)
    if not ok or info["provider"] == "mock":
        return
    paper = row("SELECT * FROM papers WHERE id=?", (pid,))
    if not paper:
        return
    blocks = rows("SELECT en FROM blocks WHERE paper_id=? AND type IN ('text', 'heading') "
                  "ORDER BY order_idx LIMIT 8", (pid,))
    sample = "\n".join(b["en"] for b in blocks)[:1800]
    if not sample.strip():
        return
    msgs = [
        {"role": "system", "content": "你是文献元数据提取助手。从给定文本中提取论文信息，"
                                      "只输出 JSON：{\"title\":\"\",\"authors\":[],\"year\":\"\",\"venue\":\"\"}，"
                                      "不确定的字段留空，不要输出其他内容。"},
        {"role": "user", "content": sample},
    ]
    try:
        raw = await chat(info, msgs, max_tokens=300)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return
    title = str(data.get("title") or "").strip()
    authors = "、".join(data.get("authors") or []) if isinstance(data.get("authors"), list) else ""
    if title and len(title) < 300 and not paper.get("title_locked"):
        ex("UPDATE papers SET title=? WHERE id=?", (title, pid))
    if authors and not paper["authors"]:
        ex("UPDATE papers SET authors=? WHERE id=?", (authors[:500], pid))
    if not paper["year"] and str(data.get("year") or "").strip():
        ex("UPDATE papers SET year=? WHERE id=?", (str(data["year"])[:10], pid))
    if not paper["venue"] and str(data.get("venue") or "").strip():
        ex("UPDATE papers SET venue=? WHERE id=?", (str(data["venue"])[:200], pid))


async def extract_terms(pid: str):
    """整篇翻译完成后，自动沉淀术语到术语表（尽力而为）。"""
    info = engine_info()
    pairs = rows("SELECT en, zh FROM blocks WHERE paper_id=? AND type IN ('text','heading') "
                 "AND zh!='' ORDER BY order_idx LIMIT 25", (pid,))
    if not pairs:
        return
    sample = "\n---\n".join(f"{p['en']}\n{p['zh']}" for p in pairs)[:6000]
    msgs = [
        {"role": "system", "content": "你是术语提取助手。从英中对照的论文段落中提取关键学术术语对，"
                                      "最多 15 条，只输出 JSON 数组，元素为 [\"英文术语\",\"中文译名\"]，"
                                      "不要输出其他内容。只提取确实重要的领域术语。"},
        {"role": "user", "content": sample},
    ]
    raw = await chat(info, msgs, max_tokens=600)
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return
    data = json.loads(m.group(0))
    for item in data:
        if isinstance(item, list) and len(item) >= 2 and item[0] and item[1]:
            ex("INSERT OR IGNORE INTO glossary(term_en, term_zh, source) VALUES(?,?, 'auto')",
               (str(item[0])[:120], str(item[1])[:120]))


def image_data_url(pid: str, rel_path: str) -> str:
    """导出 markdown 时把图片内嵌为 data url（自包含文件）。"""
    p = PAPERS_DIR / pid / rel_path
    try:
        data = p.read_bytes()
        if len(data) > 2 * 1024 * 1024:
            return None
        ext = p.suffix.lstrip(".").lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:  # noqa: BLE001
        return None
