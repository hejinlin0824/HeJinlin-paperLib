# -*- coding: utf-8 -*-
"""导入 StarDict 版 ECDICT 到 library.db 的 stardict 表（词/音标/释义）。

筛选：规范小写词形 + 有实质释义；体积控制在 ~100MB。
"""
import re
import sqlite3
import struct
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
D = BASE / "data" / "_ecdict" / "stardict-ecdict-2.4.2"
NAME = "stardict-ecdict-2.4.2"
DB = BASE / "data" / "library.db"

WORD_OK = re.compile(r"^[a-z][a-z'-]{1,29}$")


def main():
    idx = open(D / f"{NAME}.idx", "rb").read()
    dictf = open(D / f"{NAME}.dict", "rb")
    db = sqlite3.connect(DB)
    db.execute("DROP TABLE IF EXISTS stardict")
    db.execute("CREATE TABLE stardict(word TEXT PRIMARY KEY, phonetic TEXT DEFAULT '', "
               "translation TEXT DEFAULT '')")
    n = total = 0
    buf = []
    i = 0
    t0 = time.time()
    L = len(idx)
    while i < L:
        j = idx.index(b"\x00", i)
        word = idx[i:j].decode("utf-8", "ignore").lower().strip()
        off, size = struct.unpack(">II", idx[j + 1:j + 9])
        i = j + 9
        total += 1
        if not WORD_OK.match(word) or size < 6 or size > 2000:
            continue
        dictf.seek(off)
        text = dictf.read(size).decode("utf-8", "ignore")
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        phonetic = ""
        if lines and lines[0].startswith("*"):
            phonetic = lines[0][1:].strip()
            lines = lines[1:]
        trans = "\n".join(lines)[:600]
        if not trans:
            continue
        buf.append((word, phonetic, trans))
        n += 1
        if len(buf) >= 20000:
            db.executemany("INSERT OR REPLACE INTO stardict VALUES(?,?,?)", buf)
            buf = []
            print(f"  {total} 读到 / {n} 入库 / {time.time()-t0:.0f}s", flush=True)
    if buf:
        db.executemany("INSERT OR REPLACE INTO stardict VALUES(?,?,?)", buf)
    db.commit()
    print(f"完成：{n} 词入库（扫过 {total} 条目），耗时 {time.time()-t0:.0f}s")
    r = db.execute("SELECT COUNT(*) FROM stardict WHERE word='harness'").fetchone()
    print("harness 存在:", bool(r[0]))


if __name__ == "__main__":
    main()
