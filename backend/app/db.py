"""SQLite 访问层：每次操作独立连接（单机单人规模下开销可忽略），WAL 模式。"""
import json
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  id TEXT PRIMARY KEY,
  title TEXT DEFAULT '',
  title_zh TEXT DEFAULT '',
  authors TEXT DEFAULT '',
  year TEXT DEFAULT '',
  venue TEXT DEFAULT '',
  doi TEXT DEFAULT '',
  status TEXT DEFAULT 'pending_parse',
  tags TEXT DEFAULT '',
  rating INTEGER DEFAULT 0,
  reading_progress TEXT DEFAULT '',
  parse_engine TEXT DEFAULT '',
  translate_engine TEXT DEFAULT '',
  translate_done INTEGER DEFAULT 0,
  translate_total INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  cleaning_report TEXT DEFAULT '{}',
  created_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blocks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL,
  order_idx INTEGER NOT NULL,
  type TEXT NOT NULL,
  en TEXT DEFAULT '',
  zh TEXT DEFAULT '',
  meta TEXT DEFAULT '{}',
  translate_status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_blocks_paper ON blocks(paper_id, order_idx);
CREATE TABLE IF NOT EXISTS annotations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL,
  block_id INTEGER,
  kind TEXT NOT NULL,
  surface TEXT DEFAULT 'zh',
  anchor_text TEXT DEFAULT '',
  color TEXT DEFAULT '',
  content TEXT DEFAULT '',
  created_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_anno_paper ON annotations(paper_id);
CREATE TABLE IF NOT EXISTS research_log(
  paper_id TEXT PRIMARY KEY,
  research_problem TEXT DEFAULT '',
  solution TEXT DEFAULT '',
  innovation TEXT DEFAULT '',
  my_thoughts TEXT DEFAULT '',
  takeaway TEXT DEFAULT '',
  status TEXT DEFAULT '在读',
  updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL,
  annotation_id INTEGER,
  content TEXT NOT NULL,
  answer TEXT DEFAULT '',
  status TEXT DEFAULT 'open',
  created_at TEXT DEFAULT '',
  resolved_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_q_paper ON questions(paper_id);
CREATE TABLE IF NOT EXISTS glossary(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  term_en TEXT UNIQUE NOT NULL,
  term_zh TEXT DEFAULT '',
  note TEXT DEFAULT '',
  source TEXT DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS sections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL,
  title TEXT DEFAULT '',
  title_en TEXT DEFAULT '',
  block_start INTEGER,
  order_idx INTEGER DEFAULT 0,
  skip INTEGER DEFAULT 0,
  en_text TEXT DEFAULT '',
  summary TEXT DEFAULT '',
  takeaway TEXT DEFAULT '',
  questions TEXT DEFAULT '[]',
  updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sections_paper ON sections(paper_id, order_idx);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS dict_cards(
  word TEXT PRIMARY KEY,
  phonetic TEXT DEFAULT '',
  meaning TEXT DEFAULT '',
  context_note TEXT DEFAULT '',
  created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kg_entities(
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  name_zh TEXT DEFAULT '',
  description TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kg_ent_norm ON kg_entities(entity_type, normalized_name);
CREATE TABLE IF NOT EXISTS kg_entity_aliases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  source TEXT DEFAULT '',
  UNIQUE(entity_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_kg_alias_norm ON kg_entity_aliases(normalized_alias);
CREATE TABLE IF NOT EXISTS kg_relations(
  id TEXT PRIMARY KEY,
  source_entity_id TEXT NOT NULL,
  target_entity_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  evidence_type TEXT NOT NULL DEFAULT 'explicit',
  confidence REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'active',
  created_by_paper TEXT DEFAULT '',
  created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kg_rel_src ON kg_relations(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_rel_tgt ON kg_relations(target_entity_id);
CREATE TABLE IF NOT EXISTS kg_evidence(
  id TEXT PRIMARY KEY,
  entity_id TEXT DEFAULT '',
  relation_id TEXT DEFAULT '',
  paper_id TEXT NOT NULL,
  block_id INTEGER,
  evidence_text TEXT NOT NULL,
  section_name TEXT DEFAULT '',
  confidence REAL DEFAULT 0.5,
  evidence_type TEXT NOT NULL DEFAULT 'explicit',
  created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kg_evi_paper ON kg_evidence(paper_id);
CREATE INDEX IF NOT EXISTS idx_kg_evi_rel ON kg_evidence(relation_id);
CREATE INDEX IF NOT EXISTS idx_kg_evi_ent ON kg_evidence(entity_id);
CREATE TABLE IF NOT EXISTS kg_extraction_jobs(
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  stage TEXT DEFAULT '',
  progress REAL DEFAULT 0,
  error_message TEXT DEFAULT '',
  extraction_version TEXT DEFAULT '',
  model_name TEXT DEFAULT '',
  prompt_version TEXT DEFAULT '',
  stats_json TEXT DEFAULT '{}',
  created_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kg_job_paper ON kg_extraction_jobs(paper_id, status);
"""

DEFAULT_SETTINGS = {
    # 翻译引擎：provider 预设（deepseek/zhipu/moonshot/openai/ollama/mock/custom）
    "translate_provider": "mock",
    "translate_base_url": "",
    "translate_api_key": "",
    "translate_model": "",
    "translate_temperature": "0.3",
    "translate_concurrency": "4",
    # 解析引擎：builtin / mineru_api / mineru_local
    "parse_engine": "builtin",
    "mineru_token": "",
    # 解析完成后自动开始翻译
    "auto_translate": "1",
    # 解析完成后自动抽取知识图谱（可在设置页关闭）
    "kg_auto_extract": "1",
}


def _connect():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def db():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def rows(sql, args=()):
    with db() as c:
        return [dict(r) for r in c.execute(sql, args)]


def row(sql, args=()):
    with db() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def ex(sql, args=()):
    with db() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")
        _migrate(c)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def _migrate(c):
    """轻量迁移：老库补齐新列。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(papers)")}
    if "title_locked" not in cols:
        c.execute("ALTER TABLE papers ADD COLUMN title_locked INTEGER DEFAULT 0")
    for col, ddl in (("guide", "TEXT DEFAULT ''"),
                     ("explain_done", "INTEGER DEFAULT 0"),
                     ("explain_total", "INTEGER DEFAULT 0"),
                     ("cleaning_report", "TEXT DEFAULT '{}'")):
        if col not in cols:
            c.execute(f"ALTER TABLE papers ADD COLUMN {col} {ddl}")
    bcols = {r[1] for r in c.execute("PRAGMA table_info(blocks)")}
    for col in ("plain", "ask"):
        if col not in bcols:
            c.execute(f"ALTER TABLE blocks ADD COLUMN {col} TEXT DEFAULT ''")
    kg_cols = {r[1] for r in c.execute("PRAGMA table_info(kg_entities)")}
    if "name_zh" not in kg_cols:
        c.execute("ALTER TABLE kg_entities ADD COLUMN name_zh TEXT DEFAULT ''")


# ---------- settings 便捷方法 ----------

def get_settings() -> dict:
    return {r["key"]: r["value"] for r in rows("SELECT key,value FROM settings")}


def set_setting(key: str, value):
    v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    ex("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, v))
