"""Test isolation: every pytest case gets a disposable SQLite database."""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def isolate_library_database(tmp_path, monkeypatch):
    from app import db as app_db

    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "library-test.db")
    app_db.init_db()
