"""内置 PDF 解析器（PyMuPDF）：零配置、即时可用、速度快。

能力与限制：
- 段落/标题/图注识别、双栏排序、图片提取、参考文献切分；
- 复杂数学公式只能保留原始字符（无法还原 LaTeX）、矢量图无法提取为图片块；
  公式密集的论文建议在设置页改用 MinerU 引擎（可获得 LaTeX 公式）。
"""
import re
from pathlib import Path
from statistics import median

import fitz

REF_START = re.compile(r"^(references?|bibliography|参考文献)\s*[:.]?\s*$", re.I)
REF_START_INLINE = re.compile(r"^(references?|bibliography)\s*[\n:]", re.I)
CAPTION_RE = re.compile(r"^(figure|fig\.?|图|table|表)\s*\d*", re.I)
NUM_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s+\S")
SENT_END = tuple(".?!:。？！：")
# 脚注开头：上标数字紧跟大写字母（如 "9Aider"）或星号/剑号标记（如 "*Corresponding"）
FOOTNOTE_START = re.compile(r"^(\d{1,2}[A-Z(]|[∗*†‡§]\s?[A-Za-z(])")
# 数学字体（TeX 计算机现代字体族等）：公式段的识别特征
MATH_FONT = re.compile(r"(cmmi|cmsy|cmex|cmr|msam|msbm|eufm|mathematica|mathitalic|"
                       r"mathnormal|mathsymbol|latinmodern.*math|lmmath)", re.I)

# 模板噪声块（ACM/IEEE/Elsevier 首页元数据与页眉）
NOISE_BLOCK = re.compile(
    r"^(\d+\s*[:：]\s*\d+\s+[A-Z]|CCS Concepts|ACM Reference Format|"
    r"©|\(c\)\s*\d{4}|Copyright|This work is licensed|Permissions?\b|"
    r"Creative Commons|CC[ -]BY|Attribution\s*(4\.0|NonCommercial|ShareAlike)|"
    r"https?://(dx\.)?doi\.org|doi\.org/|https?://doi\.|"
    r"ACM\s+\d{4}-\d{4}|ACM Comput\. Surv|ACM Trans\.|Proc\.\s*of|Proceedings of the|"
    r"Vol(ume)?\.?\s*\d+.{0,60}(No\.|Number|Article)|Article\s*(No\.?\s*)?\d{1,4}\.?\s*$|"
    r".{0,250}(ACM (Comput\. Surv|Trans\.)\s*\d+,\s*\d+|Article\s*\d+\s*\(\w+\s+\d{4}\),\s*\d+\s*pages?)|"
    r"Publication date|出版日期|版权归|版权所有|"
    r"Authors?['’]?\s*(Contact|address)|通讯作者|作者联系方式|"
    r".{0,160}(research|work|studies?) (is|are)? ?supported( in part| in whole| partly| partially)? by|"
    r".{0,40}used Version \d|"
    r"Manuscript (submitted|received)|"
    r"^[A-Z][a-z]+ \d{1,2}, \d{4}$)", re.I)

# 宽松编号标题（兼容 "1Introduction" / "2.1. Background." 等 ACM/LNCS 形态）
NUM_HEAD_LOOSE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[\s.]*([A-Z\u4e00-\u9fff])")


def _math_ratio(b):
    """block 内数学字体字符占比（识别独立公式段）。"""
    spans = [s for ln in b.get("lines", []) for s in ln.get("spans", [])]
    total = sum(len(s.get("text", "")) for s in spans)
    if not total:
        return 0.0
    math_chars = sum(len(s.get("text", "")) for s in spans
                     if MATH_FONT.search(s.get("font", "")))
    return math_chars / total


def _block_text(b):
    lines = []
    for ln in b.get("lines", []):
        # 跳过竖排/旋转文本（arXiv 侧边水印等）
        d = ln.get("dir") or (1, 0)
        if abs(d[0]) < 0.98:
            continue
        t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        if t:
            lines.append(t)
    t = "\n".join(lines).replace("\ufffd", "").replace("\xad", "")
    # 偶发 HTML 标签：上标转 [n]，其余剥壳
    t = re.sub(r"<sup>(.*?)</sup>", r"[\1]", t, flags=re.I | re.S)
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", t)
    return t.strip()


def _rows_to_md(rows: list) -> str:
    ncol = max(len(r) for r in rows)
    rows = [list(r) + [""] * (ncol - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * ncol]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _find_tables(page, pno: int):
    """返回 [(md, bbox), ...]；PyMuPDF 表格识别。"""
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in getattr(found, "tables", []) or []:
        try:
            cells = []
            for r in t.extract():
                row = []
                for cell in r:
                    if cell is None:
                        row.append("")
                        continue
                    cell = str(cell).replace("\n", " ").strip()
                    cell = re.sub(r"(?:\s*,\s*){2,}", ", ", cell)  # 合并提取噪声 ", , ,"
                    cell = re.sub(r"\s{2,}", " ", cell)
                    row.append(cell)
                cells.append(row)
            rows = cells
        except Exception:  # noqa: BLE001
            continue
        rows = [r for r in rows if any(x for x in r)]
        ncol = max((len(r) for r in rows), default=0)
        if len(rows) < 2 or ncol < 2 or len(rows) * ncol < 6:
            continue
        out.append((_rows_to_md(rows), tuple(t.bbox)))
    return out


def _overlap_ratio(a, b) -> float:
    """矩形 a 与 b 的交集面积 / a 的面积。"""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return inter / area


def _page_items(doc):
    """按阅读顺序返回全部条目（文本/图片/表格），处理双栏与页眉页脚。"""
    items = []
    seen_xrefs = set()
    img_count = 0
    page1_text = ""
    page_h = {}
    for pno, page in enumerate(doc, start=1):
        W, H = page.rect.width, page.rect.height
        page_h[pno] = H
        W, H = page.rect.width, page.rect.height
        d = page.get_text("dict")
        if pno == 1:
            page1_text = page.get_text("text")[:3000]
        tables = _find_tables(page, pno)
        text_items = []
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            text = _block_text(b)
            if not text:
                continue
            # 丢弃纯页码
            if re.fullmatch(r"\d{1,4}", text):
                continue
            # 丢弃页眉页脚（页面顶/底 6% 区域内的短文本）
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if (y0 < H * 0.055 or y1 > H * 0.955) and len(text) < 90:
                continue
            spans = [s for ln in b.get("lines", []) for s in ln.get("spans", [])]
            size = max((s["size"] for s in spans), default=10.0)
            bold = any("Bold" in s.get("font", "") or (s.get("flags", 0) & 16) for s in spans)
            # 数学字体占比高的块 → 公式块（原文保留，不混入正文、不参与翻译）
            if _math_ratio(b) > 0.4:
                text_items.append({"kind": "formula", "text": text, "size": round(size, 1),
                                   "bold": bold, "bbox": b["bbox"], "page": pno})
                continue
            # 首行是否整行粗体（ACM 等模板的章节标题与正文同字号，靠粗体+编号识别）
            first_line = text.split("\n", 1)[0]
            l0 = next(ln for ln in b.get("lines", [])
                      if "".join(s.get("text", "") for s in ln.get("spans", [])).strip())
            l0_bold = bool(l0) and all(
                ("Bold" in s.get("font", "")) or (s.get("flags", 0) & 16)
                or bool(re.search(r"[-_ ]B$|[-_]BItalic$|OB$|Semib|Heav", s.get("font", "")))
                for s in l0.get("spans", [])) and any(
                s.get("text", "").strip() for s in l0.get("spans", []))
            text_items.append({"kind": "text", "text": text, "size": round(size, 1),
                               "bold": bold, "bbox": b["bbox"], "page": pno,
                               "line0": first_line, "line0_bold": l0_bold})
        # 剔除落入表格区域的文本（避免表格被拆成碎文本段）
        if tables:
            text_items = [it for it in text_items
                          if not any(_overlap_ratio(it["bbox"], tb) > 0.4 for _, tb in tables)]
        for md, tb in tables:
            text_items.append({"kind": "table", "text": md, "size": 0.0,
                               "bold": False, "bbox": tb, "page": pno})
        # 提取内嵌位图
        assets = None  # 由 parse() 传入目录时再保存；这里先收集
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen_xrefs:
                continue
            w, h = img[2], img[3]
            if w < 80 or h < 80:  # 滤掉 logo / 小图标
                seen_xrefs.add(xref)
                continue
            seen_xrefs.add(xref)
            rects = page.get_image_rects(xref)
            bbox = (rects[0].x0, rects[0].y0, rects[0].x1, rects[0].y1) if rects else (0, 0, W, H)
            text_items.append({"kind": "image", "xref": xref, "bbox": bbox,
                               "page": pno, "w": w, "h": h})
        # 双栏检测与排序
        left = [t for t in text_items if t["bbox"][0] < W * 0.45]
        right = [t for t in text_items if t["bbox"][0] >= W * 0.45]
        two_col = (len(left) >= 2 and len(right) >= 2
                   and min(t["bbox"][2] for t in left) <= W * 0.55)
        if two_col:
            ordered = sorted(left, key=lambda t: t["bbox"][1]) + \
                      sorted(right, key=lambda t: t["bbox"][1])
        else:
            ordered = sorted(text_items, key=lambda t: t["bbox"][1])
        items.extend(ordered)
    return items, page1_text, page_h


def _drop_noise(items):
    """剔除模板噪声块：版权/基金/CCS/DOI/期刊页眉等。"""
    return [it for it in items
            if it.get("kind") != "text" or not NOISE_BLOCK.match(" ".join(it["text"].split()))]


def _split_headings(items):
    """首行标题拆分：ACM/LNCS 等模板的章节标题与正文同字号、编号紧贴（“1Introduction”），
    且常与后续正文同块。识别成功则拆出独立 heading 项，剩余文本继续按正文处理。"""
    out = []
    for it in items:
        if it.get("kind") != "text":
            out.append(it)
            continue
        text = it["text"]
        lines = text.split("\n")
        l0 = lines[0].strip()
        rest_lines = lines[1:]
        # ACM 双行标题形态："1" 单独一行 + 标题文字在下一行 → 合并判断
        if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,3}\.?", l0) and rest_lines \
                and rest_lines[0].strip() and len(l0 + rest_lines[0]) < 90 \
                and not re.match(r"^\d", rest_lines[0].strip()):
            l0 = (l0 + " " + rest_lines[0].strip()).strip()
            rest_lines = rest_lines[1:]
        # 含大量数学斜体字符的行是公式，不是标题
        if len(re.findall(r"[\U0001D400-\U0001D7FF]", l0)) >= 2:
            out.append(it)
            continue
        is_head = False
        m = NUM_HEAD_LOOSE.match(l0)
        if m and len(l0) <= 90 and not re.search(r"[.!?]\s+[a-z]", l0):
            is_head = True
        elif (it.get("line0_bold") and 2 < len(l0) <= 80
              and not re.search(r"[,;:]", l0) and not l0.endswith((".", ",", ";"))):
            is_head = True
        if not is_head:
            out.append(it)
            continue
        # 规范化编号与文字之间的空格："1Introduction" → "1 Introduction"
        nm = re.match(r"\d{1,2}(?:\.\d{1,2}){0,3}", l0)
        if nm:
            norm = re.sub(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s*", r"\1 ", l0)
            depth = len(nm.group(0).split("."))
        else:  # 无编号的粗体小标题
            norm = l0
            depth = 3
        out.append({"kind": "heading", "text": norm, "size": it["size"],
                    "bold": True, "bbox": it["bbox"], "page": it["page"],
                    "level": min(depth, 4)})
        rest = "\n".join(rest_lines).strip()
        if rest:
            out.append({**it, "text": rest})
    return out


# ---------- 论文头部结构化（标题/作者/单位 vs 正文），两引擎共用 ----------

AUTHOR_HINT = re.compile(
    r"(University|Institute|College|Laborator|Department|School of|Computing|"
    r"Computer Science|Computer Engineering|@[\w.-]+\.[a-z]{2,}|Inc\.|Corp\.|Ltd\.|"
    r"Google|Microsoft|Meta|Apple|Amazon|ByteDance|Alibaba|Tencent|OpenAI|Anthropic)", re.I)
NAME_ROW = re.compile(r"^[A-Z][A-Za-z'\- .]{2,40}(?:,| and |;|\s{2,}|\n)")


def _authorish(en: str) -> bool:
    t = (en or "").strip()
    if not t or len(t) > 220:
        return False
    if "@" in t:
        return True
    return bool(AUTHOR_HINT.search(t) or NAME_ROW.match(t))


def _noiseish(en: str) -> bool:
    return bool(NOISE_BLOCK.match(" ".join((en or "").split())))


def extract_front_matter(blocks: list) -> tuple:
    """头部结构化：返回 (新块列表, 作者原文)。

    规则：首个 heading=论文标题（阅读器已单独渲染，删除）；其后连续的
    作者/单位/邮箱块聚合为一个 type='authors' 块（居中卡片展示，不翻译）；
    头部区域的版权/DOI/卷期等噪声删除；遇到摘要或首个正文章节标题即终止。
    """
    if not blocks:
        return blocks, ""
    end = None
    for i in range(1, min(len(blocks), 15)):
        b = blocks[i]
        en = (b.get("en") or "").strip()
        if b.get("type") == "heading" and (
                re.match(r"^(abstract|摘\s*要)", en, re.I) or NUM_HEAD_LOOSE.match(en)):
            end = i
            break
        if b.get("type") == "text":
            if _noiseish(en) or _authorish(en):
                continue  # 噪声/作者块：仍属头部
            end = i  # 首个正文块 = 摘要/正文开始
            break
    if end is None:
        end = min(len(blocks), 15)
    head, rest = blocks[1:end], blocks[end:]  # blocks[0] = 论文标题，删除
    authors, kept = [], []
    for b in head:
        en = (b.get("en") or "").strip()
        if b.get("type") == "text" and _authorish(en):
            authors.append(en)
        elif _noiseish(en) and b.get("type") == "text":
            continue
        else:
            kept.append(b)
    out = list(rest)
    if authors:
        out = [{"type": "authors", "en": "\n".join(authors)[:2000], "zh": "",
                "meta": {"page": 1}, "order_idx": 0}] + kept + out
    else:
        out = kept + out
    for i, b in enumerate(out):
        b["order_idx"] = i
    return out, ("\n".join(authors) if authors else "")


def _filter_footnotes(items, body_size, page_h):
    """剔除页脚脚注（小字号、页面底部、以脚注标记开头的文本块）。

    arXiv/ACM 等模板的脚注是页面底部的小字，以上标数字+大写字母（"9Aider
    documentation: …"）或星号（"*Corresponding"）开头。混进正文会打断段落与翻译。
    """
    from collections import defaultdict

    by_page = defaultdict(list)
    for it in items:
        by_page[it["page"]].append(it)
    drop = set()
    for pno, its in by_page.items():
        H = page_h.get(pno, 1000)
        small_bottom = [
            it for it in its
            if it["kind"] == "text"
            and it["bbox"][1] > H * 0.55
            and 0 < it["size"] <= body_size * 0.93
            and not CAPTION_RE.match(it["text"])
            and not REF_START.match(it["text"])
        ]
        starts = [it for it in small_bottom if FOOTNOTE_START.match(it["text"])]
        if not starts:
            continue
        zone_y = min(it["bbox"][1] for it in starts)
        for it in small_bottom:
            if it["bbox"][1] >= zone_y - 2:
                drop.add(id(it))
    return [it for it in items if id(it) not in drop]


def _merge_paragraphs(items):
    """合并被切断的段落：同字号且 ① 垂直紧邻，或 ② 前块无句末标点且本块以小写开头
    （脚注剔除后、双栏换栏处、跨页处的断句接回）。"""
    merged = []
    for it in items:
        if (merged and it["kind"] == "text" and merged[-1]["kind"] == "text"
                and abs(it["size"] - merged[-1]["size"]) < 0.6
                and not merged[-1]["text"].rstrip().endswith(SENT_END)
                and not NUM_HEADING.match(it["text"])
                and not CAPTION_RE.match(it["text"])):
            near = (it["page"] == merged[-1]["page"]
                    and it["bbox"][1] - merged[-1]["bbox"][3] < it["size"] * 2.0)
            cont = it["text"][:1].islower() and len(merged[-1]["text"]) > 60
            if near or cont:
                merged[-1]["text"] += " " + it["text"]
                merged[-1]["bbox"] = (merged[-1]["bbox"][0], merged[-1]["bbox"][1],
                                      max(merged[-1]["bbox"][2], it["bbox"][2]), it["bbox"][3])
                merged[-1]["page"] = it["page"]
                continue
        merged.append(dict(it))
    return merged


def _drop_running_heads(items):
    """去掉跨页重复出现的页眉（页码归一化后比对）与孤立编号/页码块。"""
    from collections import Counter

    def key(t):
        # 页码/卷期号归一化为 #，使仅页码不同的页眉块可聚合计数
        return re.sub(r"\d+[:：]\s*\d+|\b\d{1,4}\b", "#", t)[:60]

    cnt = Counter(key(it["text"]) for it in items
                  if it["kind"] == "text" and len(it["text"]) < 90)
    drop = {k for k, c in cnt.items() if c >= 3}
    return [it for it in items
            if not (it["kind"] == "text" and (
                key(it["text"]) in drop
                # 孤立编号/页码块（"1"、"2"、公式编号残留）
                or re.fullmatch(r"[\d\s:：;,.]+", it["text"].strip())))]


def _classify_and_build(items, body_size):
    blocks = []
    refs = None
    for it in items:
        if it["kind"] == "image":
            blocks.append({"type": "image", "en": "", "meta": {"page": it["page"]}})
            continue
        if it["kind"] == "heading":
            if refs is None and (REF_START.match(it["text"]) or REF_START_INLINE.match(it["text"])):
                refs = [it["text"]]
                continue
            if refs is not None:
                refs.append(it["text"])
                continue
            blocks.append({"type": "heading", "en": it["text"],
                           "meta": {"page": it["page"], "level": it.get("level", 2)}})
            continue
        if it["kind"] == "formula":
            blocks.append({"type": "formula", "en": it["text"],
                           "meta": {"page": it["page"], "raw": True}})
            continue
        if it["kind"] == "table":
            blocks.append({"type": "table", "en": it["text"],
                           "meta": {"page": it["page"], "md": it["text"]}})
            continue
        text = it["text"]
        size, bold = it["size"], it["bold"]
        # 参考文献区开始
        if refs is None and (REF_START.match(text) or REF_START_INLINE.match(text)):
            refs = [text]
            continue
        if refs is not None:
            refs.append(text)
            continue
        m = {"page": it["page"]}
        cap = bool(CAPTION_RE.match(text)) and len(text) < 400
        if cap:
            m["caption"] = True
            blocks.append({"type": "text", "en": text, "meta": m})
            continue
        level = 0
        nm = NUM_HEADING.match(text)
        if (size >= body_size * 1.22 and len(text) <= 300) or \
           (bold and size >= body_size * 1.08 and len(text) <= 120):
            if len(re.findall(r"[\U0001D400-\U0001D7FF]", text)) >= 2:
                blocks.append({"type": "text", "en": text, "meta": m})
                continue
            level = 1 if size >= body_size * 1.6 else (2 if size >= body_size * 1.35 else 3)
        elif nm and len(text) <= 90 and len(text.split()) <= 14 and not text.rstrip().endswith(SENT_END):
            level = min(len(nm.group(1).split(".")) + 1, 4)
        if level:
            m["level"] = level
            blocks.append({"type": "heading", "en": text, "meta": m})
        else:
            blocks.append({"type": "text", "en": text, "meta": m})
    # 参考文献整块
    if refs:
        ref_text = "\n".join(refs)
        ref_text = re.sub(r"^(references?|bibliography)\s*[:.]?\s*\n?", "", ref_text,
                          flags=re.I)
        entries = re.split(r"\n?\s*\[(\d+)\]", ref_text)
        if len(entries) > 2:
            out = []
            for i in range(1, len(entries), 2):
                out.append(f"[{entries[i]}] {entries[i + 1].strip()}")
            entries = out
        else:
            entries = [l.strip() for l in ref_text.split("\n") if l.strip()]
        blocks.append({"type": "references", "en": ref_text,
                       "meta": {"entries": entries[:200]}})
    return blocks


def _attach_images(blocks, doc, work_dir: Path, items):
    """把图片落盘并把紧随其后的 Figure 图注挂到图片块上。"""
    assets = work_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    n = 0
    by_page = {}
    for it in items:
        if it["kind"] == "image":
            by_page.setdefault(it["page"], []).append(it)
    for b in blocks:
        if b["type"] != "image":
            continue
        page_items = by_page.get(b["meta"]["page"], [])
        if not page_items:
            continue
        it = page_items.pop(0)
        try:
            pix = fitz.Pixmap(doc, it["xref"])
            if pix.n - pix.alpha > 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            n += 1
            fname = f"img_p{it['page']}_{n}.png"
            pix.save(str(assets / fname))
            b["meta"]["path"] = f"assets/{fname}"
            b["meta"]["size"] = [it["w"], it["h"]]
        except Exception:
            b["meta"]["path"] = None
    # 挂图注：图片块后紧跟的 Figure 图注文本块
    out = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b["type"] == "image" and b["meta"].get("path"):
            j = i + 1
            while j < len(blocks):
                nb = blocks[j]
                if (nb["type"] == "text" and nb["meta"].get("caption")
                        and re.match(r"^(figure|fig\.?)\s*\d*", nb["en"], re.I)
                        and nb["meta"].get("page") == b["meta"]["page"]):
                    b["en"] = nb["en"]
                    j += 1
                elif nb["type"] == "image" and not nb["meta"].get("path"):
                    j += 1  # 提取失败的图片占位，跳过
                else:
                    break
            out.append(b)
            i = j
        else:
            if not (b["type"] == "image" and not b["meta"].get("path")):
                out.append(b)  # 提取失败的图片块直接丢弃
            i += 1
    return out


def parse(pdf_path: str, work_dir: Path) -> dict:
    doc = fitz.open(pdf_path)
    items, page1_text, page_h = _page_items(doc)
    sizes = [it["size"] for it in items if it["kind"] == "text" and len(it["text"]) > 50]
    body_size = median(sizes) if sizes else 10.0
    items = _drop_noise(items)
    items = _filter_footnotes(_drop_running_heads(items), body_size, page_h)
    items = _split_headings(items)
    items = _merge_paragraphs(items)
    blocks = _classify_and_build(items, body_size)
    blocks = _attach_images(blocks, doc, work_dir, items)
    # 标题 = 首页最大字号文本（排除水印类文本）
    WATERMARK = re.compile(
        r"^(arxiv\s*:|doi\s*:|ieee\b|acm\b|proceedings|copyright|©|licensed|"
        r"vol\.?\s*\d|journal|transactions|received|accepted|published)", re.I)
    title = ""
    p1 = [it for it in items if it["page"] == 1 and it["kind"] == "text"
          and not WATERMARK.match(it["text"])]
    if p1:
        cand = max(p1, key=lambda it: (it["size"], len(it["text"])))
        # 跨行标题取全块（ACM 标题常分两行），换行归一为空格
        title = " ".join(cand["text"].split())[:300]
    year = ""
    m = re.search(r"\b(19|20)\d{2}\b", page1_text)
    if m:
        year = m.group(0)
    for idx, b in enumerate(blocks):
        b["order_idx"] = idx
    return {"blocks": blocks,
            "meta": {"title": title, "year": year, "pages": doc.page_count,
                     "first_page_text": page1_text}}
