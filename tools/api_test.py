# -*- coding: utf-8 -*-
"""API 冒烟测试（中文正文，避免 shell 编码问题）。

这是手动运行的集成冒烟脚本，不是 pytest 测试模块：
    python tools/api_test.py <pid_file>
"""
import argparse
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8300"


def main(pid_file: str) -> int:
    with Path(pid_file).open(encoding="utf-8") as stream:
        pid = next(
            (line.strip().split("=", 1)[1] for line in stream if line.startswith("PID=")),
            None,
        )
    if not pid:
        raise ValueError(f"未能从 {pid_file!r} 中找到 PID=...")

    print("pid:", pid)
    with httpx.Client(base_url=BASE, timeout=30) as client:
        # 1. 研究总表写入 + 读取
        response = client.put(f"/api/log/{pid}", json={
            "research_problem": "如何高效阅读翻译论文",
            "solution": "块级双语对齐 + 本地工作台",
            "innovation": "批注锚定块ID，重译不丢",
            "my_thoughts": "很适合科研日常",
            "takeaway": "读论文效率翻倍",
            "status": "读完",
        })
        print("PUT log:", response.status_code, response.json().get("status"))

        rows = client.get("/api/log").json()
        row = rows[0]
        print(f"log rows={len(rows)} status={row['log_status']} open_q={row['open_questions']} "
              f"total_q={row['total_questions']} problem={row['research_problem'][:12]}")

        # 2. 导出 CSV / MD / 论文MD
        csv_text = client.get("/api/log/export?format=csv").text
        print("csv head:", csv_text[:60].replace("\n", "|"))
        md_text = client.get("/api/log/export?format=md").text
        print("md has takeaway:", "读论文效率翻倍" in md_text)
        paper_md = client.get(f"/api/papers/{pid}/export.md").text
        print("paper md len:", len(paper_md), "| has 图注:", "Overview of the parsing" in paper_md)

        # 3. 问题追踪：先从阅读器创建问题批注（联动建问题），再回答 + 解决
        blocks = client.get(f"/api/papers/{pid}/blocks").json()
        block_id = [item for item in blocks if item["type"] == "text"][0]["id"]
        client.post(f"/api/papers/{pid}/annotations", json={
            "kind": "question", "block_id": block_id, "surface": "zh",
            "anchor_text": "", "content": "为什么用块级对齐？",
        })
        questions = client.get(f"/api/questions?paper_id={pid}").json()
        question = questions[0]
        print("question:", question["content"], "| status:", question["status"])
        client.patch(f"/api/questions/{question['id']}", json={
            "answer": "因为段落ID稳定", "status": "resolved",
        })
        questions = client.get(f"/api/questions?paper_id={pid}").json()
        print("after resolve:", questions[0]["status"], "| answer:", questions[0]["answer"])

        # 4. 术语表
        client.post("/api/glossary", json={
            "term_en": "attention", "term_zh": "注意力机制", "note": "",
        })
        glossary = client.get("/api/glossary").json()
        print("glossary:", [
            (item["term_en"], item["term_zh"], item["source"]) for item in glossary
        ][:3])

        # 5. 单段重译 + 编辑译文
        blocks = client.get(f"/api/papers/{pid}/blocks").json()
        block = [item for item in blocks if item["type"] == "text"][0]
        translated = client.post(
            f"/api/papers/{pid}/blocks/{block['id']}/retranslate"
        ).json()
        print("retranslate:", translated["zh"][:30])
        edited = client.patch(
            f"/api/papers/{pid}/blocks/{block['id']}",
            json={"zh": "人工修正后的译文"},
        ).json()
        print("edit zh:", edited["zh"])

        # 6. 阅读进度 + 仪表盘 + 备份
        client.put(f"/api/papers/{pid}/progress", json={"block_idx": 5, "ratio": 0.42})
        dashboard = client.get("/api/papers/dashboard").json()
        print("dashboard:", dashboard["stats"], "| recent:", len(dashboard["recent"]),
              "| open_q:", len(dashboard["open_questions"]))
        backup = client.get("/api/backup.zip")
        print("backup:", backup.status_code, len(backup.content), "bytes")

        # 7. 引擎测试
        translate_test = client.post("/api/settings/test-translate").json()
        print("test-translate(mock):", translate_test["ok"], translate_test["message"][:20])
        parse_test = client.post(
            "/api/settings/test-parse", json={"engine": "builtin"}
        ).json()
        print("test-parse:", parse_test["ok"], parse_test["message"][:20])

        # 8. 前端静态页
        response = client.get("/")
        print("SPA /:", response.status_code, "len:", len(response.text))
        response = client.get("/reader/xyz")
        print("SPA fallback /reader/xyz:", response.status_code,
              "is html:", response.text.lstrip().startswith("<!DOCTYPE"))

    print("\nALL TESTS DONE")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PaperLib API 手动冒烟测试")
    parser.add_argument("pid_file", help="包含 PID=... 的文本文件")
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.pid_file))
