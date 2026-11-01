import sqlite3
from pathlib import Path

from tools.check_library import inspect_library


def test_empty_new_install_is_allowed(tmp_path):
    report = inspect_library(tmp_path)
    assert report["safe_to_start"] is True
    assert report["paper_records"] is None


def test_missing_database_with_existing_pdf_blocks_startup(tmp_path):
    papers = tmp_path / "data" / "papers" / "paper-1"
    papers.mkdir(parents=True)
    (papers / "original.pdf").write_bytes(b"%PDF-1.4")
    report = inspect_library(tmp_path)
    assert report["safe_to_start"] is False
    assert report["pdf_files"] == 1


def test_empty_database_with_paper_files_blocks_startup(tmp_path):
    data = tmp_path / "data"
    papers = data / "papers" / "paper-1"
    papers.mkdir(parents=True)
    (papers / "original.pdf").write_bytes(b"%PDF-1.4")
    with sqlite3.connect(data / "library.db") as conn:
        conn.execute("CREATE TABLE papers(id TEXT PRIMARY KEY)")
    report = inspect_library(tmp_path)
    assert report["safe_to_start"] is False
    assert report["paper_records"] == 0


def test_existing_library_database_is_allowed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "library.db") as conn:
        conn.execute("CREATE TABLE papers(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO papers(id) VALUES('paper-1')")
    report = inspect_library(tmp_path)
    assert report["safe_to_start"] is True
    assert report["paper_records"] == 1
