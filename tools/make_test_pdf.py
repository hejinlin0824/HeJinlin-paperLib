"""生成一篇用于测试的英文论文 PDF（3页：标题/摘要/章节/图片/参考文献）。"""
import sys
from pathlib import Path

import fitz

OUT = Path(__file__).resolve().parent.parent / "data" / "test_paper.pdf"


def make_image(path: Path):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 180, 110))
    for x in range(180):
        for y in range(110):
            pix.set_pixel(x, y, (40 + x % 200, 90 + y % 150, 160))
    pix.save(str(path))


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = OUT.parent / "test_fig.png"
    make_image(img)
    doc = fitz.open()
    W, H = 612, 792

    def para(page, text, y, size=10, font="helv", x=72, max_w=470, leading=None):
        """简易自动换行。"""
        leading = leading or size * 1.5
        words, line = text.split(), ""
        for w in words:
            t = (line + " " + w).strip()
            if fitz.get_text_length(t, fontname=font, fontsize=size) > max_w and line:
                page.insert_text((x, y), line, fontname=font, fontsize=size)
                y += leading
                line = w
            else:
                line = t
        if line:
            page.insert_text((x, y), line, fontname=font, fontsize=size)
            y += leading
        return y

    # ---- Page 1 ----
    p = doc.new_page(width=W, height=H)
    y = 96
    y = para(p, "PaperLib: A Lightweight Personal Paper Reading Workbench",
             y, size=16, font="hebo", leading=22)
    y += 6
    p.insert_text((72, y), "Jinlin He and ZCode Agent", fontname="helv", fontsize=11)
    y += 34
    p.insert_text((72, y), "Abstract", fontname="hebo", fontsize=12)
    y += 22
    y = para(p, "Researchers read many papers every week, but existing translation tools "
                "produce stiff machine translation and provide no way to take structured notes. "
                "We present PaperLib, a local-first reading workbench that parses uploaded PDFs "
                "into structured blocks, translates them with modern language models, and "
                "preserves a one-to-one layout correspondence between the original and translated "
                "text. Annotations, highlights and reading logs are stored locally in 2026.",
             y, size=10)
    y += 10
    p.insert_text((72, y), "1  Introduction", fontname="hebo", fontsize=12)
    y += 22
    y = para(p, "Reading academic papers is a core activity of research [1]. However, most "
                "reading tools either focus on reference management or on crude web-page "
                "translation. None of them combine flexible AI translation with a structured "
                "reading log, which motivates our work. In this paper we describe a system that "
                "keeps figures, formulas and references aligned across languages [2].", y)
    y = para(p, "Our contribution is threefold. First, a block-level bilingual reader. Second, "
                "a glossary-consistent translation pipeline. Third, a research log that records "
                "the research problem, the proposed solution and possible innovation directions "
                "for every paper the user reads.", y)

    # ---- Page 2 ----
    p = doc.new_page(width=W, height=H)
    y = 100
    p.insert_text((72, y), "2  Methods", fontname="hebo", fontsize=12)
    y += 22
    y = para(p, "The pipeline has three stages: parsing, translation and annotation. Parsing "
                "converts a PDF into an ordered list of blocks such as headings, paragraphs, "
                "figures, formulas and references. Figure 1 shows an overview of the pipeline. "
                "Each block carries a stable identifier so that annotations survive re-translation.",
              y)
    y += 8
    p.insert_image(fitz.Rect(140, y, 440, y + 180), filename=str(img))
    y += 200
    p.insert_text((72, y), "Figure 1: Overview of the parsing and translation pipeline.",
                  fontname="helv", fontsize=9)
    y += 30
    y = para(p, "The training objective combines a reconstruction loss and a ranking loss, "
                "L = L_rec + lambda * L_rank, where lambda is a hyper-parameter tuned on the "
                "validation set. We follow the standard evaluation protocol of prior work [1] "
                "and report both BLEU and COMET scores in the experiments section.", y)

    # ---- Page 3 ----
    p = doc.new_page(width=W, height=H)
    y = 100
    p.insert_text((72, y), "3  Conclusion", fontname="hebo", fontsize=12)
    y += 22
    y = para(p, "We presented PaperLib, a local-first workbench for reading translated papers "
                "with structured notes. The system keeps a one-to-one correspondence between "
                "source and translated blocks, anchors annotations to stable block identifiers, "
                "and accumulates a personal knowledge base as the user reads. Future work "
                "includes local model support and retrieval augmented question answering.", y)
    y += 20
    p.insert_text((72, y), "References", fontname="hebo", fontsize=12)
    y += 20
    refs = [
        "[1] J. Smith and K. Lee. Efficient reading of scientific documents. Journal of "
        "Academic Tools, 12(3):45-58, 2025.",
        "[2] A. Wang, B. Chen, and C. Diaz. Neural machine translation for low-resource "
        "languages. In Proceedings of ACL, pages 1122-1135, 2024.",
    ]
    for r in refs:
        y = para(p, r, y, size=9)

    doc.save(str(OUT))
    print("saved:", OUT)


if __name__ == "__main__":
    main()
