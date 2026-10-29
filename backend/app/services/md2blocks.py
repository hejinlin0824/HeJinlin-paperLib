"""MinerU 输出（content_list.json，回退 markdown）→ 统一块结构。"""
import json
import re
import shutil
from pathlib import Path


def _copy_asset(src_name: str, assets_src: Path, work_dir: Path):
    """把 MinerU 解出的图片复制到论文 assets 目录，返回相对路径。"""
    if not src_name or not assets_src:
        return None
    src = assets_src / Path(src_name).name
    if not src.exists():
        # 兜底：按文件名搜索
        hits = [p for p in assets_src.rglob(Path(src_name).name)]
        if not hits:
            return None
        src = hits[0]
    assets = work_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    dst = assets / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return f"assets/{src.name}"


def _cap_text(v):
    if isinstance(v, list):
        return " ".join(str(x) for x in v if isinstance(x, str)).strip()
    return v if isinstance(v, str) else ""


SUP_WRAPPED = re.compile(r"^\s*<sup>\s*\d+\s*</sup>", re.I)
HTML_SUP = re.compile(r"<sup>(.*?)</sup>", re.I | re.S)
HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def _clean_text(t: str) -> str:
    """清洗 MinerU 文本：剥替换字符/软连字符；<sup>n</sup> 内联标记转 [n]；剥其余 HTML 标签。"""
    t = (t or "").replace("\ufffd", "").replace("\xad", "")
    t = HTML_SUP.sub(r"[\1]", t)
    return HTML_TAG.sub("", t)


def _is_footnote(text: str) -> bool:
    """MinerU 把页底脚注当正文返回：以 <sup>数字</sup> 开头的块。"""
    return bool(SUP_WRAPPED.match(text or ""))


# 页脚 running footer 文本模式（MinerU 无坐标信息，靠模式+重复度识别）
PAGE_FOOTER = re.compile(r"^(\d+[:：]\d+|[A-Z]\.\s*[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+et al\.?|\d{1,4})$")


def _drop_running_footers(blocks: list) -> list:
    """删页脚块：ACM 页码（263:3）、作者缩写（M. Zhang et al.）、纯数字页码；
    以及归一化后重复 ≥3 次的短块（各模板 running head/footer，含长页眉）。"""
    from collections import Counter

    def key(t):
        return re.sub(r"\d+", "#", (t or "").strip())

    cnt = Counter(key(b["en"]) for b in blocks
                  if b["type"] == "text" and 0 < len((b["en"] or "").strip()) <= 90)
    drop = {k for k, c in cnt.items() if c >= 3}
    return [b for b in blocks
            if not (b["type"] == "text" and (
                PAGE_FOOTER.match((b["en"] or "").strip())
                or key(b["en"]) in drop))]


# 段内内联小节标题："1.1.1 Challenges. 首先……" / "1.1.3 Article Organization. 本文……"
INLINE_HEAD = re.compile(
    r"^(\d{1,2}(?:\.\d{1,2}){1,3})\s+([A-Z\u4e00-\u9fff][^.。]{2,60}?)[.。]\s*(.{30,})$", re.S)


def _split_inline_headings(blocks: list) -> list:
    """把与正文粘连的小节标题拆成独立 heading 块（MinerU 常见形态）。"""
    out = []
    for b in blocks:
        if b["type"] != "text":
            out.append(b)
            continue
        en = (b["en"] or "").strip()
        m = INLINE_HEAD.match(en)
        if m:
            level = len(m.group(1).split(".")) + 1
            out.append({"type": "heading", "en": f"{m.group(1)} {m.group(2)}",
                        "meta": {**b.get("meta", {}), "level": level}})
            out.append({"type": "text", "en": m.group(3).strip(),
                        "meta": dict(b.get("meta", {}))})
            continue
        out.append(b)
    return out


LIST_ITEM = re.compile(r"^[—–•\-\*]\s+\S|^\d{1,2}[.)]\s+\S")


def _merge_list_items(blocks: list) -> list:
    """连续的分条块（— / • / 1.）合并为一个 list 块，避免每条一个段落工具。"""
    out = []
    for b in blocks:
        en = (b["en"] or "").strip()
        if b["type"] == "text" and LIST_ITEM.match(en) and len(en) < 600:
            if out and out[-1]["type"] == "list":
                out[-1]["en"] += "\n" + en
                continue
            out.append({"type": "list", "en": en, "meta": dict(b.get("meta", {}))})
        else:
            out.append(b)
    return out


def _merge_references(blocks: list) -> list:
    """MinerU 的参考文献条目是散的 text 块：从 References 标题起聚合为单个 references 块。"""
    for i, b in enumerate(blocks):
        if b["type"] == "heading" and re.match(r"^(references?|bibliography)\b", b["en"] or "", re.I):
            entries, j = [], i + 1
            while j < len(blocks):
                nb = blocks[j]
                if nb["type"] == "text":
                    entries.append(nb["en"])
                    j += 1
                elif nb["type"] == "heading" and re.match(r"^\[?\d", nb["en"] or ""):
                    entries.append(nb["en"])
                    j += 1
                else:
                    break
            if entries:
                ref_block = {"type": "references", "en": "\n".join(entries),
                             "meta": {"entries": [e for e in entries if e.strip()]}}
                return blocks[:i] + [ref_block] + blocks[j:]
    return blocks


def from_content_list(data: list, assets_src: Path, work_dir: Path):
    blocks = []
    for it in data:
        t = it.get("type", "text")
        page = (it.get("page_idx") or 0) + 1
        if t == "image":
            blocks.append({"type": "image",
                           "en": _cap_text(it.get("img_caption")),
                           "meta": {"page": page,
                                    "path": _copy_asset(it.get("img_path", ""), assets_src, work_dir)}})
        elif t == "table":
            blocks.append({"type": "table",
                           "en": _cap_text(it.get("table_caption")),
                           "meta": {"page": page, "html": it.get("table_body") or ""}})
        elif t == "equation":
            # MinerU 的公式文本常带 $$ 包裹，剥掉后存纯 LaTeX
            latex = re.sub(r"^\$\$+|\$\$+$", "", (it.get("text") or "").strip()).strip()
            blocks.append({"type": "formula", "en": latex,
                           "meta": {"page": page, "latex": latex}})
        else:  # text
            raw = (it.get("text") or "").strip()
            if _is_footnote(raw):
                continue  # 页底脚注混入正文 → 丢弃
            text = _clean_text(raw)
            if not text:
                continue
            m = {"page": page}
            if it.get("text_level"):
                blocks.append({"type": "heading", "en": text, "meta": {**m, "level": it["text_level"]}})
            else:
                blocks.append({"type": "text", "en": text, "meta": m})
    blocks = _drop_running_footers(blocks)
    blocks = _split_inline_headings(blocks)
    blocks = _merge_list_items(blocks)
    return _merge_references(blocks)


MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def from_markdown(md: str, assets_src: Path, work_dir: Path):
    """无 content_list.json 时，从 markdown 解析出块（公式/图片信息可能缺失）。"""
    blocks = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        hm = MD_HEADING.match(s)
        if hm:
            blocks.append({"type": "heading", "en": hm.group(2).strip(),
                           "meta": {"level": len(hm.group(1))}})
            i += 1
            continue
        if s.startswith("$$"):
            buf = [s[2:]]
            if s.endswith("$$") and len(s) > 3:
                buf = [s[2:-2]]
                latex = buf[0].strip()
            else:
                i += 1
                while i < len(lines) and not lines[i].strip().endswith("$$"):
                    buf.append(lines[i])
                    i += 1
                if i < len(lines):
                    buf.append(lines[i].strip()[:-2])
                    i += 1
                latex = "\n".join(buf).strip()
            blocks.append({"type": "formula", "en": latex, "meta": {"latex": latex}})
            continue
        im = MD_IMAGE.search(s)
        if im and len(s) < 500:
            blocks.append({"type": "image", "en": "",
                           "meta": {"path": _copy_asset(im.group(1), assets_src, work_dir)}})
            i += 1
            continue
        if MD_TABLE_ROW.match(line):
            tbl = []
            while i < len(lines) and MD_TABLE_ROW.match(lines[i]):
                tbl.append(lines[i].strip())
                i += 1
            blocks.append({"type": "table", "en": "", "meta": {"md": "\n".join(tbl)}})
            continue
        # 普通段落：收集到空行
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not MD_HEADING.match(lines[i].strip()) \
                and not lines[i].strip().startswith("$$") and not MD_IMAGE.search(lines[i].strip()) \
                and not MD_TABLE_ROW.match(lines[i]):
            para.append(lines[i])
            i += 1
        blocks.append({"type": "text", "en": " ".join(p.strip() for p in para), "meta": {}})
    return blocks


def from_mineru_dir(extract_dir: Path, work_dir: Path):
    """在 MinerU 输出目录中找 content_list.json / markdown / images，转为统一块。"""
    assets_src = None
    for p in extract_dir.rglob("*"):
        if p.is_dir() and p.name in ("images", "imgs", "images_origin"):
            assets_src = p
            break
    cl = sorted(extract_dir.rglob("*content_list.json"))
    if cl:
        try:
            data = json.loads(cl[0].read_text(encoding="utf-8"))
            blocks = from_content_list(data, assets_src, work_dir)
            if blocks:
                for idx, b in enumerate(blocks):
                    b["order_idx"] = idx
                return {"blocks": blocks, "meta": {}}
        except Exception:
            pass
    mds = [p for p in extract_dir.rglob("*.md") if p.name != "index.md"]
    if not mds:
        raise RuntimeError("MinerU 输出中未找到 markdown 或 content_list.json")
    md = mds[0].read_text(encoding="utf-8")
    blocks = from_markdown(md, assets_src, work_dir)
    for idx, b in enumerate(blocks):
        b["order_idx"] = idx
    return {"blocks": blocks, "meta": {}}
